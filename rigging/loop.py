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
    handoff: Any = None  # set when the model calls escalate_to_human
    exporters: list[Any] = field(default_factory=list)

    def tool_map(self) -> dict[str, Any]:
        return {t.name: t for t in self.tools}


def _span(agent: Assembled, name: str, attributes: dict[str, Any]) -> Any:
    """Start a span on every exporter; a broken exporter must never break a run."""
    spans = []
    for exporter in agent.exporters:
        try:
            spans.append((exporter, exporter.start_span(name, attributes)))
        except Exception:  # observability is not allowed to be a failure mode
            continue
    return spans


def _end_span(agent: Assembled, spans: Any, attributes: dict[str, Any]) -> None:
    for exporter, span in spans or []:
        try:
            exporter.end_span(span, attributes)
        except Exception:
            continue


def _sub_env(value: Any) -> Any:
    """${VAR} in manifest option values reads the environment; unset means empty."""
    import os
    import re as _re

    if isinstance(value, str):
        return _re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", lambda m: os.environ.get(m.group(1), ""), value)
    return value


def _coerce_tool(obj: Any) -> Any:
    from rigging.tools.schema import FunctionTool

    return obj if hasattr(obj, "run") else FunctionTool(obj)


def _instantiate_guardrail(obj: Any, config: dict[str, Any]) -> Any:
    return obj(**config) if isinstance(obj, type) else obj


def assemble(spec: AgentSpec, *, registry: Registry | None = None, client: Any = None,
             prices: PriceTable | None = None) -> Assembled:
    if registry is None:
        # The default registry is populated by the built-in capability packs at
        # import; third-party entry points are discovered separately (ADR-0003).
        import rigging.guardrails
        import rigging.retriever
        import rigging.tools.core
        if any(n in ("lookup_order", "check_pricing", "refund_order") for n in spec.tools):
            import rigging.tools.demo  # noqa: F401  (demo pack, registered on demand)
        registry = DEFAULT_REGISTRY

    if client is None:
        from rigging.client.openai_compat import OpenAICompatClient

        client = OpenAICompatClient(settings.base_url, settings.api_key, timeout=settings.request_timeout_s)

    fabricated = {"escalate_to_human", "search_policies", "load_skill"}
    mcp_prefixes = tuple(f"{s.name}__" for s in spec.mcp_servers)
    mcp_names = [n for n in spec.tools if n.startswith(mcp_prefixes)]
    registry_tools = [n for n in spec.tools if n not in fabricated and n not in mcp_names]
    tools: list[Any] = [_coerce_tool(obj) for obj in registry.resolve_all("tool", registry_tools)]

    if spec.mcp_servers:
        from rigging.mcp import collect

        tools.extend(collect(spec.mcp_servers, allow=mcp_names or None))

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

    if spec.retriever is not None:
        from pathlib import Path as _Path

        from rigging.tools.builtin import make_search_tool

        options = {k: _sub_env(v) for k, v in spec.retriever.options.items()}
        if spec.retriever.name == "fts5":
            options.setdefault("path", (_Path(spec.source_path).parent / "demo.db") if spec.source_path else _Path("demo.db"))
        retriever = registry.get("retriever", spec.retriever.name)(**options)
        tools.append(make_search_tool(retriever))

    if "escalate_to_human" in spec.tools:
        from rigging.tools.builtin import make_escalate_tool

        tools.append(make_escalate_tool())

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

    exporters: list[Any] = []
    for ref in spec.observability.exporters:
        name, _, opts_raw = ref.partition(":")
        opts = dict(kv.split("=", 1) for kv in opts_raw.split(",") if "=" in kv) if opts_raw else {}
        from rigging.observability import EXPORTERS

        factory = EXPORTERS.get(name.strip())
        if factory is None:
            raise CapabilityNotFound("exporter", [name], sorted(EXPORTERS))
        exporters.append(factory(**opts))

    return Assembled(spec=spec, client=client, tools=tools, guardrails=guardrails,
                     skills=skills, approval=approval, prices=prices or PriceTable.load(),
                     exporters=exporters)


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
              approval: Any = None, prices: PriceTable | None = None,
              history: list[Message] | None = None) -> RunResult:
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

    messages: list[Message] = list(history or [])
    if not any(m.role == "system" for m in messages) and system:
        messages.insert(0, Message(role="system", content=system))

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
    root_span = _span(agent, "invoke_agent", {
        "gen_ai.operation.name": "invoke_agent",
        "gen_ai.system": agent.client.name if hasattr(agent.client, "name") else "openai",
        "gen_ai.request.model": spec.model,
        "gen_ai.agent.name": spec.name,
        "session.id": session_id,
        "rigging.run_id": run_id,
        "rigging.spec_hash": spec.spec_hash,
    })
    last: ModelResponse | None = None
    exhausted: str | None = None

    for step in range(1, spec.limits.max_steps + 1):
        reason = _check_budget(spec, usage, started, agent.prices, step)
        log.append(run_id, "budget_check", {"step": step, "usage": usage.model_dump(), "blocked": reason})
        if reason:
            exhausted = reason
            break

        call_span = _span(agent, "chat", {
            "gen_ai.operation.name": "chat",
            "gen_ai.request.model": spec.model,
            "gen_ai.request.max_tokens": spec.limits.max_tokens,
            "rigging.step": step,
        })
        request = ModelRequest(model=spec.model, messages=messages, tools=wire)
        log.append(run_id, "model_request", {"model": spec.model, "messages": len(messages), "tools": list(tools)})
        response = agent.client.complete(request) if stream is None else _streaming_call(agent, request, log, run_id, stream)

        usage = usage + response.usage
        last = response
        _end_span(agent, call_span, {
            "gen_ai.response.model": response.model or spec.model,
            "gen_ai.usage.input_tokens": response.usage.input_tokens,
            "gen_ai.usage.output_tokens": response.usage.output_tokens,
            "gen_ai.response.finish_reason": response.finish_reason,
            "rigging.tool_calls": len(response.tool_calls),
        })
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

    _end_span(agent, root_span, {
        "gen_ai.usage.input_tokens": usage.input_tokens,
        "gen_ai.usage.output_tokens": usage.output_tokens,
        "rigging.status": "ok",
        "rigging.steps": step,
        "rigging.handoff": bool(agent.handoff),
    })
    log.append(run_id, "run_finished", {"status": "ok", "content_chars": len(output)})
    log.finish_run(run_id, "ok")
    if owns_log:
        log.close()
    result = RunResult(run_id=run_id, status="ok", output=output, usage=usage,
                       steps=step if not exhausted else spec.limits.max_steps,
                       cost_usd=agent.prices.cost(spec.model, usage), handoff=agent.handoff)
    result.history = messages  # chat REPL only; excluded from --json
    for exporter in agent.exporters:
        try:
            exporter.close()
        except Exception:
            continue
    return result


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
    tool_span = _span(agent, "execute_tool", {
        "gen_ai.operation.name": "execute_tool",
        "gen_ai.tool.name": call.name,
        "rigging.tool.side_effecting": bool(getattr(tools.get(call.name), "side_effecting", False)),
    })
    tool = tools.get(call.name)
    if tool is None:
        result = ToolResult.error(call, f"unknown tool {call.name!r}; available: {', '.join(sorted(tools))}")
    else:
        verdict, notes = _apply("on_tool_call", agent.guardrails["on_tool_call"], call, tool)
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

    _end_span(agent, tool_span, {"rigging.tool.denied": result.denied,
                                 "rigging.tool.is_error": result.is_error})
    if call.name == "escalate_to_human" and not result.is_error:
        _capture_handoff(agent, result.content, log, run_id)

    verdict, notes = _apply("on_tool_result", agent.guardrails["on_tool_result"], call, result.content)
    if isinstance(verdict, Transform):
        result = result.model_copy(update={"content": verdict.value})
    log.append(run_id, "tool_result", {"call_id": result.call_id, "name": result.name,
                                       "content": result.content, "is_error": result.is_error,
                                       "denied": result.denied, "duration_ms": result.duration_ms})
    return Message(role="tool", tool_call_id=result.call_id, name=result.name, content=result.content)


def _capture_handoff(agent: Assembled, content: str, log: EventLog, run_id: str) -> None:
    """`escalate_to_human` returns "HANDOFF {json}"; parse it into a structured handoff."""
    import json as _json

    from rigging.types import Handoff

    try:
        payload = _json.loads(content.removeprefix("HANDOFF ").strip())
        agent.handoff = Handoff(**payload)
        if not agent.handoff.transcript_run_id:
            agent.handoff = agent.handoff.model_copy(update={"transcript_run_id": run_id})
    except Exception as exc:  # a malformed handoff must not crash the run
        log.append(run_id, "error", {"stage": "escalation", "error": str(exc), "raw": content[:200]})
        return
    log.append(run_id, "escalation", {"reason": agent.handoff.reason,
                                      "summary": agent.handoff.summary,
                                      "transcript_run_id": agent.handoff.transcript_run_id})


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
