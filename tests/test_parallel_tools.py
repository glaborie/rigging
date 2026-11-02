"""Parallel tool execution is opt-in (ADR-0022): approvals serial, log ordered.

Offline: local tools that sleep, so wall-clock overlap is observable without a
network or a model.
"""

from __future__ import annotations

import time
from typing import ClassVar

import yaml

from rigging.approvals import ScriptedApproval
from rigging.events import EventLog
from rigging.loop import run_agent
from rigging.manifest import load_manifest
from rigging.protocols import Allow, Deny
from rigging.registry import Registry
from rigging.testing import FakeClient
from rigging.types import ToolCall, ToolResult


def _spec(tmp_path, **extra):
    data = {"name": "t", "model": "test", "system_prompt": "be terse",
            "limits": {"max_steps": 4}}
    data.update(extra)
    path = tmp_path / "a.yaml"
    path.write_text(yaml.safe_dump(data))
    return load_manifest(str(path))


def _registry(started: list[float] | None = None) -> Registry:
    reg = Registry()

    def slow_a(delay: float = 0.25) -> str:
        """Slow tool A."""
        time.sleep(delay)
        if started is not None:
            started.append(time.monotonic())
        return "a done"

    def slow_b(delay: float = 0.25) -> str:
        """Slow tool B."""
        time.sleep(delay)
        if started is not None:
            started.append(time.monotonic())
        return "b done"

    def refund(amount: int) -> str:
        """Side-effecting tool."""
        return f"refunded {amount}"

    def explode() -> str:
        """Always raises."""
        raise RuntimeError("kaboom")

    for fn, name, side in ((slow_a, "slow_a", False), (slow_b, "slow_b", False),
                           (refund, "refund", True), (explode, "explode", False)):
        fn.__rigging_tool__ = {"name": name, "description": name, "side_effecting": side}
        reg.add("tool", name, fn)
    return reg


class _RaisingTool:
    name = "raises"
    side_effecting = False
    description = "raises from run()"
    json_schema: ClassVar[dict] = {"type": "function", "function": {
        "name": "raises", "description": "raises",
        "parameters": {"type": "object", "properties": {}}}}

    def run(self, call: ToolCall) -> ToolResult:
        raise ValueError("third-party tool exploded")


def _two_calls(first: str = "slow_a", second: str = "slow_b") -> list[dict]:
    return [
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "1", "name": first, "arguments": {}},
            {"id": "2", "name": second, "arguments": {}},
        ]},
        {"role": "assistant", "content": "done"},
    ]


def test_parallel_tools_overlap(tmp_path):
    spec = _spec(tmp_path, tools=["slow_a", "slow_b"], parallel_tools=True)
    started: list[float] = []
    result = run_agent(spec, "go", log=EventLog(tmp_path / "p.db"),
                       client=FakeClient(_two_calls()), registry=_registry(started))

    assert result.status == "ok"
    assert len(started) == 2
    # Wall clock includes SQLite commits per event; the property is the overlap.
    assert started[1] - started[0] < 0.05, f"tools did not overlap: {started}"


def test_without_the_flag_tools_run_in_sequence(tmp_path):
    spec = _spec(tmp_path, tools=["slow_a", "slow_b"])
    started: list[float] = []
    result = run_agent(spec, "go", log=EventLog(tmp_path / "s.db"),
                       client=FakeClient(_two_calls()), registry=_registry(started))

    assert result.status == "ok"
    assert started[1] - started[0] >= 0.2, "sequential tools must not overlap"


def test_messages_and_events_keep_the_declared_order(tmp_path):
    spec = _spec(tmp_path, tools=["slow_b", "slow_a"], parallel_tools=True)
    log = EventLog(tmp_path / "o.db")
    result = run_agent(spec, "go", log=log, client=FakeClient(_two_calls("slow_b", "slow_a")),
                       registry=_registry())

    tool_messages = [m for m in (result.history or []) if m.role == "tool"]
    assert [m.name for m in tool_messages] == ["slow_b", "slow_a"]
    assert [m.tool_call_id for m in tool_messages] == ["1", "2"]

    events = list(log.events(result.run_id))
    assert [e["payload"]["name"] for e in events if e["type"] == "tool_result"] == \
        ["slow_b", "slow_a"]
    seqs = [e["seq"] for e in events]
    assert seqs == list(range(1, len(seqs) + 1)), "the log must stay contiguous"
    assert any(e["type"] == "tool_batch" for e in events)


def test_approval_happens_before_any_tool_starts(tmp_path):
    spec = _spec(tmp_path, tools=["slow_a", "refund"], parallel_tools=True)
    started: list[float] = []
    asked: list[float] = []

    def approve(call: ToolCall) -> bool:
        asked.append(time.monotonic())
        return True

    run_agent(spec, "go", log=EventLog(tmp_path / "a.db"),
              client=FakeClient(_two_calls("slow_a", "refund")),
              registry=_registry(started), approval=ScriptedApproval(approve))

    assert asked, "the side-effecting call must be approved"
    assert started, "the pure call must still run"
    assert asked[0] <= min(started), "approval must precede execution"


def test_auto_deny_blocks_only_the_consequential_call(tmp_path):
    spec = _spec(tmp_path, tools=["slow_a", "refund"], parallel_tools=True)
    log = EventLog(tmp_path / "d.db")
    result = run_agent(spec, "go", log=log,
                       client=FakeClient(_two_calls("slow_a", "refund")),
                       registry=_registry())

    results = {e["payload"]["name"]: e["payload"]
               for e in log.events(result.run_id) if e["type"] == "tool_result"}
    assert results["refund"]["denied"] is True
    assert results["slow_a"]["is_error"] is False


def test_a_raising_tool_becomes_an_error_result(tmp_path):
    spec = _spec(tmp_path, tools=["slow_a", "raises"], parallel_tools=True)
    reg = _registry()
    reg.add("tool", "raises", _RaisingTool())
    log = EventLog(tmp_path / "e.db")
    result = run_agent(spec, "go", log=log, client=FakeClient(_two_calls("slow_a", "raises")),
                       registry=reg)

    assert result.status == "ok"
    payloads = {e["payload"]["name"]: e["payload"]
                for e in log.events(result.run_id) if e["type"] == "tool_result"}
    assert payloads["raises"]["is_error"] is True
    assert "ValueError" in payloads["raises"]["content"]
    assert payloads["slow_a"]["is_error"] is False


def test_a_guardrail_deny_does_not_stop_the_other_call(tmp_path):
    class DenySecond:
        name = "deny_second"
        hooks = ("on_tool_call",)

        def on_input(self, messages): return Allow()
        def on_tool_call(self, call, tool):
            return Deny("no") if call.name == "slow_b" else Allow()
        def on_tool_result(self, call, content): return Allow()
        def on_output(self, text, messages): return Allow()

    reg = _registry()
    reg.add("guardrail", "deny_second", DenySecond())
    spec = _spec(tmp_path, tools=["slow_a", "slow_b"], parallel_tools=True,
                 guardrails={"on_tool_call": ["deny_second"]})
    log = EventLog(tmp_path / "g.db")
    result = run_agent(spec, "go", log=log, client=FakeClient(_two_calls()), registry=reg)

    payloads = {e["payload"]["name"]: e["payload"]
                for e in log.events(result.run_id) if e["type"] == "tool_result"}
    assert payloads["slow_b"]["denied"] is True
    assert payloads["slow_a"]["is_error"] is False
