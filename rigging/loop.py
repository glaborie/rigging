"""The agent loop.

Sequence (docs/architecture.md §3):

    on_input → [budget check] → model → tool calls → on_tool_call → approval
    → tool → on_tool_result → … → final answer → on_output

Three invariants, each covered by a test:
  1. a tool can never crash the run (errors become tool messages);
  2. a side-effecting tool can never run unapproved;
  3. every step is an event.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from rigging.approvals import AutoDenyApproval, CLIPromptApproval, ScriptedApproval
from rigging.config import settings
from rigging.errors import CapabilityNotFound
from rigging.events import EventLog
from rigging.manifest import AgentSpec
from rigging.pricing import PriceTable
from rigging.protocols import Allow, Deny, Transform, Verdict
from rigging.registry import DEFAULT_REGISTRY, Registry
from rigging.skills import FilesystemSkillResolver
from rigging.types import (
    Message,
    ModelDelta,
    ModelRequest,
    ModelResponse,
    RunResult,
    ToolCall,
    ToolResult,
    Usage,
)


@dataclass
class Assembled:
    """A manifest, resolved against a registry. Fail-fast: unknown names raise."""

    spec: AgentSpec
    client: Any
    tools: list[Any] = field(default_factory=list)
    guardrails: dict[str, list[Any]] = field(default_factory=dict)
    skills: Any = None
    approval: Any = None
    prices: PriceTable = field(default_factory=PriceTable.load)

    def tool_map(self) -> dict[str, Any]:
        return {t.name: t for t in self.tools}


def _coerce_tool(obj: Any) -> Any:
    from rigging.tools.schema import FunctionTool

    return obj if hasattr(obj, "run") else FunctionTool(obj)


def _instantiate_guardrail(obj: Any, config: dict[str, Any]) -> Any:
    return obj(**config) if isinstance(obj, type) else obj


def assemble(spec: AgentSpec, *, registry: Registry | None = None, client: Any = None,
             prices: PriceTable | None = None) -> Assembled:
    registry = registry or DEFAULT_REGISTRY

    if client is None:
        from rigging.client.openai_compat import OpenAICompatClient

        client = OpenAICompatClient(settings.base_url, settings.api_key, timeout=settings.request_timeout_s)

    tools: list[Any] = [_coerce_tool(obj) for obj in registry.resolve_all("tool", spec.tools)]

    guardrails: dict[str, list[Any]] = {"on_input": [], "on_tool_call": [], "on_tool_result": [], "on_output": []}
    for hook in guardrails:
        for ref in getattr(spec.guardrails, hook):
            impl = _instantiate_guardrail(registry.get("guardrail", ref.name), ref.config)
            guardrails[hook].append(impl)

    skills = None
    if spec.skills:
        from pathlib import Path

        from rigging.tools.builtin import make_load_skill_tool

        skills_dir = (Path(spec.source_path).parent / spec.skills_dir) if spec.source_path else Path(spec.skills_dir)
        skills = FilesystemSkillResolver(skills_dir)
        known = {m.name for m in skills.discover()}
        missing = [s for s in spec.skills if s not in known]
        if missing:
            raise CapabilityNotFound("skill", missing, sorted(known))
        tools.append(make_load_skill_tool(skills))

    approval = {
        "cli": CLIPromptApproval,
        "scripted": ScriptedApproval,
        "auto_deny": AutoDenyApproval,
    }[spec.approval.policy]()
    if spec.approval.side_effecting_tools:
        for name in spec.approval.side_effecting_tools:
            for t in tools:
                if t.name == name:
                    t.side_effecting = True

    return Assembled(spec=spec, client=client, tools=tools, guardrails=guardrails,
                     skills=skills, approval=approval, prices=prices or PriceTable.load())


# --------------------------------------------------------------------------- chain

def _apply(hook: str, chain: Sequence[Any], *args: Any) -> tuple[Verdict, list[dict[str, Any]]]:
    """Run a guardrail chain. First Deny wins; Transform threads through."""
    verdict: Verdict = Allow()
    notes: list[dict[str, Any]] = []
    current = list(args)
    for guardrail in chain:
        result = getattr(guardrail, hook)(*current)
        notes.append({"guardrail": guardrail.name, "hook": hook, "verdict": type(result).__name__})
        if isinstance(result, Deny):
            return result, notes
        if isinstance(result, Transform):
            current = _apply_transform(hook, current, result.value)
            notes[-1]["transform"] = result.note
    if current != list(args):
        return Transform(current[0] if hook == "on_input" else current[-1]), notes
    return verdict, notes


def _apply_transform(hook: str, current: list[Any], value: Any) -> list[Any]:
    if hook == "on_input":
        return [value, *current[1:]]
    if hook == "on_tool_result":
        return [current[0], value]
    return current


# --------------------------------------------------------------------------- run

def run_agent(spec: AgentSpec, prompt: str, *, registry: Registry | None = None, client: Any = None,
              log: EventLog | None = None, session_id: str | None = None,
              stream: Callable[[ModelDelta], None] | None = None,
              approval: Any = None, prices: PriceTable | None = None) -> RunResult:
    agent = assemble(spec, registry=registry, client=client, prices=prices)
    if approval is not None:
        agent.approval = approval

    log = log or EventLog(settings.db_path)
    owns_log = log is None
    session_id = log.start_session(session_id, title=spec.name, spec_hash=spec.spec_hash)
    run_id = log.start_run(session_id=session_id, spec_hash=spec.spec_hash, model=spec.model)

    system = spec.prompt().strip()
    if agent.skills is not None:
        index = agent.skills.index_block(spec.skills)
        system = f"{system}\n\n{index}".strip()

    messages: list[Message] = []
    if system:
        messages.append(Message(role="system", content=system))

    verdict, notes = _apply("on_input", agent.guardrails["on_input"], [Message(role="user", content=prompt)])
    log.append(run_id, "guardrail_verdict", {"hook": "on_input", "checks": notes})
    if isinstance(verdict, Deny):
        log.finish_run(run_id, "denied")
        return RunResult(run_id=run_id, status="denied", error=verdict.reason)
    user_content = verdict.value if isinstance(verdict, Transform) else prompt
    messages.append(Message(role="user", content=user_content))
    log.append(run_id, "input_received", {"content": user_content})

    tools = agent.tool_map()
    wire = [t.json_schema for t in agent.tools]
    usage = Usage()
    started = time.perf_counter()
    last: ModelResponse | None = None
    exhausted: str | None = None

    for step in range(1, spec.limits.max_steps + 1):
        reason = _check_budget(spec, usage, started, agent.prices, step)
        log.append(run_id, "budget_check", {"step": step, "usage": usage.model_dump(), "blocked": reason})
        if reason:
            exhausted = reason
            break

        request = ModelRequest(model=spec.model, messages=messages, tools=wire)
        log.append(run_id, "model_request", {"model": spec.model, "messages": len(messages), "tools": list(tools)})
        response = agent.client.complete(request) if stream is None else _streaming_call(agent, request, log, run_id, stream)

        usage = usage + response.usage
        last = response
        log.append(run_id, "model_response", {
            "content": response.message.content,
            "reasoning": response.message.reasoning,
            "tool_calls": [c.model_dump() for c in response.tool_calls],
            "usage": response.usage.model_dump(),
            "finish_reason": response.finish_reason,
        })
        messages.append(response.message)

        if not response.tool_calls:
            break

        for call in response.tool_calls:
            messages.append(_execute_call(call, tools, agent, log, run_id))

    else:
        exhausted = "max_steps"

    # ---- final answer
    if exhausted:
        log.append(run_id, "budget_check", {"exhausted": exhausted})
        final = _final_answer(agent, spec, messages, log, run_id, stream)
        if final is None:
            log.finish_run(run_id, "budget_exhausted")
            return RunResult(run_id=run_id, status="budget_exhausted", usage=usage,
                             steps=spec.limits.max_steps, error=exhausted,
                             cost_usd=agent.prices.cost(spec.model, usage))
        last = final

    output = last.message.content if last else ""
    out_verdict, out_notes = _apply("on_output", agent.guardrails["on_output"], output, messages)
    log.append(run_id, "output_verdict", {"checks": out_notes})
    if isinstance(out_verdict, Deny):
        log.finish_run(run_id, "denied")
        return RunResult(run_id=run_id, status="denied", usage=usage, error=out_verdict.reason)
    if isinstance(out_verdict, Transform):
        output = out_verdict.value

    log.append(run_id, "run_finished", {"status": "ok", "content_chars": len(output)})
    log.finish_run(run_id, "ok")
    if owns_log:
        log.close()
    return RunResult(run_id=run_id, status="ok", output=output, usage=usage,
                     steps=step if not exhausted else spec.limits.max_steps,
                     cost_usd=agent.prices.cost(spec.model, usage))


def _check_budget(spec: AgentSpec, usage: Usage, started: float, prices: PriceTable, step: int) -> str | None:
    limits = spec.limits
    if usage.input_tokens + usage.output_tokens > (limits.max_tokens or 10**18):
        return "max_tokens"
    if limits.max_wall_clock_s and (time.perf_counter() - started) > limits.max_wall_clock_s:
        return "max_wall_clock_s"
    if limits.max_cost_usd is not None:
        cost = prices.cost(spec.model, usage)
        if cost is not None and cost > limits.max_cost_usd:
            return "max_cost_usd"
    return None


def _streaming_call(agent: Assembled, request: ModelRequest, log: EventLog, run_id: str,
                    stream: Callable[[ModelDelta], None]) -> ModelResponse:
    text: list[str] = []
    reasoning: list[str] = []
    from rigging.client.openai_compat import OpenAICompatClient

    calls: dict[int, dict[str, Any]] = {}
    usage = Usage()
    finish = None
    for delta in agent.client.stream(request, on_error=lambda t, p: log.append(run_id, t, p)):
        stream(delta)
        text.append(delta.text)
        reasoning.append(delta.reasoning)
        if delta.usage:
            usage = delta.usage
        if delta.tool_call_delta:
            OpenAICompatClient._merge_tool_delta(None, calls, delta.tool_call_delta)  # type: ignore[arg-type]
        finish = delta.finish_reason or finish
    return ModelResponse(
        message=OpenAICompatClient._assemble_message(None, "".join(text), "".join(reasoning), calls),  # type: ignore[arg-type]
        usage=usage, finish_reason=finish, model=request.model,
    )


def _execute_call(call: ToolCall, tools: dict[str, Any], agent: Assembled, log: EventLog, run_id: str) -> Message:
    log.append(run_id, "tool_call", call.model_dump())
    tool = tools.get(call.name)
    if tool is None:
        result = ToolResult.error(call, f"unknown tool {call.name!r}; available: {', '.join(sorted(tools))}")
    else:
        verdict, notes = _apply("on_tool_call", agent.guardrails["on_tool_call"], [call, tool])
        log.append(run_id, "guardrail_verdict", {"hook": "on_tool_call", "tool": call.name, "checks": notes})
        if isinstance(verdict, Deny):
            result = ToolResult.error(call, f"denied by guardrail: {verdict.reason}", denied=True)
        else:
            args = verdict.value if isinstance(verdict, Transform) else call.arguments
            if isinstance(args, dict) and args != call.arguments:
                call = call.model_copy(update={"arguments": args})
            if getattr(tool, "side_effecting", False):
                log.append(run_id, "approval_requested", {"tool": call.name, "arguments": call.arguments})
                approved, note = agent.approval.request(call, tool, "side-effecting tool")
                log.append(run_id, "approval_verdict", {"tool": call.name, "approved": approved, "note": note})
                result = (tool.run(call) if approved
                          else ToolResult.error(call, f"denied by approval policy: {note}", denied=True))
            else:
                result = tool.run(call)

    verdict, notes = _apply("on_tool_result", agent.guardrails["on_tool_result"], [call, result.content])
    if isinstance(verdict, Transform):
        result = result.model_copy(update={"content": verdict.value})
    log.append(run_id, "tool_result", {"call_id": result.call_id, "name": result.name,
                                       "content": result.content, "is_error": result.is_error,
                                       "denied": result.denied, "duration_ms": result.duration_ms})
    return Message(role="tool", tool_call_id=result.call_id, name=result.name, content=result.content)


def _final_answer(agent: Assembled, spec: AgentSpec, messages: list[Message], log: EventLog,
                  run_id: str, stream: Callable[[ModelDelta], None] | None) -> ModelResponse | None:
    """One last call with tools disabled (ADR-0010)."""
    nudge = Message(role="user", content=(
        "You have reached this run's limit. Answer now with what you have, or say plainly "
        "what is missing and that a human will follow up. Do not call tools."))
    request = ModelRequest(model=spec.model, messages=[*messages, nudge], tools=[])
    log.append(run_id, "model_request", {"model": spec.model, "final_answer": True})
    try:
        response: ModelResponse = agent.client.complete(request)
    except Exception as exc:
        log.append(run_id, "error", {"stage": "final_answer", "error": str(exc)})
        return None
    log.append(run_id, "model_response", {"content": response.message.content,
                                          "usage": response.usage.model_dump(), "final_answer": True})
    return response
