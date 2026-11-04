"""Subagents are child runs in the same event log (ADR-0019).

A parent manifest declares `subagents: [{name, manifest, description}]`; assembly
turns each declaration into a `spawn_<name>(task)` tool that runs the child with
its own manifest and budget, in the parent's session, with `parent_run_id` set.

Three properties are load-bearing:
  * the child appends to the parent's log, so nothing is summarised away;
  * the child inherits the parent's approval *object*, never its own policy;
  * failures come back as tool results — a subagent cannot crash the run.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from rigging.config import settings
from rigging.errors import ManifestError
from rigging.manifest import AgentSpec, SubagentCfg, load_manifest
from rigging.types import RunContext, ToolCall, ToolResult

#: A child's answer is pasted into the parent's context; cap it rather than
#: letting one chatty subagent eat the parent's whole budget.
MAX_SUMMARY_CHARS = 4000


def resolve_child(parent: AgentSpec, ref: SubagentCfg) -> AgentSpec:
    """Load the child manifest relative to the parent's, failing fast on a typo."""
    base = Path(parent.source_path).parent if parent.source_path else Path.cwd()
    path = base / ref.manifest
    if not path.exists():
        raise ManifestError(
            f"subagent {ref.name!r}: manifest not found: {path} (declared in {parent.source_path})"
        )
    return load_manifest(str(path))


class SpawnTool:
    """`spawn_<name>(task)` — a side-effecting tool that runs a child agent.

    A child that ends `denied`, `budget_exhausted` or `error` still produced a
    structured summary, so the result carries it *and* sets `is_error`: the parent
    model learns the delegation failed without the harness pretending it worked.
    """

    def __init__(self, *, ref: SubagentCfg, child: AgentSpec, log: Any, session_id: str,
                 run_id: str, depth: int, registry: Any, client: Any, approval: Any,
                 prices: Any) -> None:
        self.name = f"spawn_{ref.name}"
        self.side_effecting = True  # spends tokens and time: the approval gate applies
        self.description = ref.description or (
            f"Delegate a self-contained task to the {ref.name!r} subagent "
            f"({child.name}) and return its answer."
        )
        self.json_schema = {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "task": {
                            "type": "string",
                            "description": "The whole task, self-contained: the subagent "
                                           "does not see this conversation.",
                        },
                    },
                    "required": ["task"],
                },
            },
        }
        self._child = child
        self._log = log
        self._session_id = session_id
        self._run_id = run_id
        self._depth = depth
        self._registry = registry
        self._client = client
        self._approval = approval
        self._prices = prices

    def run(self, call: ToolCall, ctx: RunContext | None = None) -> ToolResult:
        task = call.arguments.get("task")
        if not isinstance(task, str) or not task.strip():
            return ToolResult.error(call, "spawn needs a non-empty `task` string")
        if self._depth >= settings.subagent_max_depth:
            return ToolResult.error(call, (
                f"max subagent depth reached ({settings.subagent_max_depth}); "
                "finish this task yourself instead of delegating further"
            ))

        from rigging.loop import run_agent  # local: the loop builds these tools

        try:
            result = run_agent(
                self._child, task,
                registry=self._registry,
                # An injected client is the caller's (a test double or a deliberate
                # gateway override). Otherwise the child builds its own from its
                # manifest's model + credential_profile.
                client=self._client,
                log=self._log,
                session_id=self._session_id,
                approval=self._approval,
                prices=self._prices,
                parent_run_id=self._run_id,
                depth=self._depth + 1,
            )
        except Exception as exc:  # a subagent can never crash the parent run
            return ToolResult.error(call, f"subagent failed to start: {type(exc).__name__}: {exc}")

        output = result.output
        if len(output) > MAX_SUMMARY_CHARS:
            output = output[:MAX_SUMMARY_CHARS] + "… (truncated)"
        payload = {
            "run_id": result.run_id,
            "status": result.status,
            "output": output,
            "steps": result.steps,
            "usage": result.usage.model_dump(),
        }
        if result.error:
            payload["error"] = result.error
        return ToolResult(call_id=call.id, name=self.name, content=json.dumps(payload),
                          is_error=result.status != "ok")

    def __repr__(self) -> str:
        return f"<SpawnTool {self.name} side_effecting>"


def make_spawn_tool(*, ref: SubagentCfg, child: AgentSpec,
                    log: Any, session_id: str, run_id: str, depth: int,
                    registry: Any, client: Any, approval: Any, prices: Any) -> Any:
    """Build the tool for one declared subagent (the child is loaded eagerly)."""
    return SpawnTool(ref=ref, child=child, log=log, session_id=session_id, run_id=run_id,
                     depth=depth, registry=registry, client=client, approval=approval,
                     prices=prices)
