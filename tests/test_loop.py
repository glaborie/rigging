"""Loop invariants: tools never crash the run, side effects need approval, every
step is an event. Uses a fake client — no network, no model, deterministic.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from rigging.approvals import AutoDenyApproval, ScriptedApproval
from rigging.events import EventLog
from rigging.loop import run_agent
from rigging.manifest import AgentSpec, GuardrailRef, Guardrails, Limits
from rigging.protocols import Allow, Deny
from rigging.registry import Registry
from rigging.types import Message, ModelRequest, ModelResponse, ToolCall, Usage


class FakeClient:
    """Replays scripted responses in order."""

    name = "fake"

    def __init__(self, responses: list[ModelResponse]) -> None:
        self.responses = list(responses)
        self.requests: list[ModelRequest] = []

    def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        return self.responses.pop(0)

    def stream(self, request: ModelRequest, on_error: Any = None):  # pragma: no cover
        raise NotImplementedError


def _resp(content: str = "", calls: list[ToolCall] | None = None) -> ModelResponse:
    return ModelResponse(
        message=Message(role="assistant", content=content, tool_calls=calls or []),
        usage=Usage(input_tokens=10, output_tokens=5), finish_reason="stop", model="fake",
    )


def _spec(**kw: Any) -> AgentSpec:
    base = dict(name="t", model="fake", tools=[], limits=Limits(max_steps=3))
    base.update(kw)
    return AgentSpec(**base)


def _registry() -> Registry:
    reg = Registry()

    def echo(text: str) -> str:
        """Echo text."""
        return text

    def boom(text: str) -> str:
        """Always fails."""
        raise RuntimeError("kaboom")

    def refund(amount: int) -> str:
        """Refund money."""
        return f"refunded {amount}"

    for fn, name, side in ((echo, "echo", False), (boom, "boom", False), (refund, "refund", True)):
        fn.__rigging_tool__ = {"name": name, "description": fn.__doc__, "side_effecting": side}
        reg.add("tool", name, fn)
    return reg


def test_tool_error_becomes_a_message_not_a_crash(tmp_path: Path) -> None:
    client = FakeClient([
        _resp(calls=[ToolCall(id="1", name="boom", arguments={"text": "x"})]),
        _resp("recovered"),
    ])
    with EventLog(tmp_path / "r.db") as log:
        result = run_agent(_spec(tools=["boom"]), "go", registry=_registry(), client=client, log=log)
    assert result.status == "ok"
    assert result.output == "recovered"
    tool_msg = next(m for m in client.requests[1].messages if m.role == "tool")
    assert "kaboom" in tool_msg.content


def test_unknown_tool_is_reported_to_the_model(tmp_path: Path) -> None:
    client = FakeClient([
        _resp(calls=[ToolCall(id="1", name="nope", arguments={})]),
        _resp("sorry"),
    ])
    with EventLog(tmp_path / "r.db") as log:
        run_agent(_spec(), "go", registry=_registry(), client=client, log=log)
    tool_msg = next(m for m in client.requests[1].messages if m.role == "tool")
    assert "unknown tool" in tool_msg.content


def test_side_effecting_tool_denied_by_default(tmp_path: Path) -> None:
    client = FakeClient([
        _resp(calls=[ToolCall(id="1", name="refund", arguments={"amount": 10})]),
        _resp("could not refund"),
    ])
    with EventLog(tmp_path / "r.db") as log:
        run_agent(_spec(tools=["refund"]), "refund me", registry=_registry(),
                  client=client, log=log, approval=AutoDenyApproval())
    tool_msg = next(m for m in client.requests[1].messages if m.role == "tool")
    assert "denied by approval policy" in tool_msg.content


def test_side_effecting_tool_runs_when_approved(tmp_path: Path) -> None:
    client = FakeClient([
        _resp(calls=[ToolCall(id="1", name="refund", arguments={"amount": 10})]),
        _resp("done"),
    ])
    with EventLog(tmp_path / "r.db") as log:
        run_agent(_spec(tools=["refund"]), "refund me", registry=_registry(),
                  client=client, log=log, approval=ScriptedApproval(True))
    tool_msg = next(m for m in client.requests[1].messages if m.role == "tool")
    assert "refunded 10" in tool_msg.content


def test_budget_exhaustion_asks_for_final_answer(tmp_path: Path) -> None:
    loop_forever = [
        _resp(calls=[ToolCall(id=str(i), name="echo", arguments={"text": "again"})]) for i in range(2)
    ]
    client = FakeClient([*loop_forever, _resp("here is what I know")])
    with EventLog(tmp_path / "r.db") as log:
        result = run_agent(_spec(tools=["echo"], limits=Limits(max_steps=2)), "go",
                           registry=_registry(), client=client, log=log)
    assert result.status == "ok"
    assert result.output == "here is what I know"
    assert client.requests[-1].tools == []  # final answer call has tools disabled


def test_every_step_is_an_event(tmp_path: Path) -> None:
    client = FakeClient([
        _resp(calls=[ToolCall(id="1", name="echo", arguments={"text": "x"})]),
        _resp("done"),
    ])
    with EventLog(tmp_path / "r.db") as log:
        result = run_agent(_spec(tools=["echo"]), "go", registry=_registry(), client=client, log=log)
        types = [e["type"] for e in log.events(result.run_id)]
    for required in ("run_started", "input_received", "model_request", "model_response",
                     "tool_call", "tool_result", "run_finished"):
        assert required in types


def test_output_guardrail_can_deny(tmp_path: Path) -> None:
    class NoApologies:
        name = "no_apologies"
        hooks: tuple[str, ...] = ("on_output",)

        def on_input(self, messages): return Allow()
        def on_tool_call(self, call, tool): return Allow()
        def on_tool_result(self, call, content): return Allow()
        def on_output(self, text, messages): return Deny("no")


    reg = _registry()
    reg.add("guardrail", "no_apologies", NoApologies())
    spec = _spec()
    spec.guardrails = Guardrails(on_output=[GuardrailRef(name="no_apologies")])
    client = FakeClient([_resp("anything")])
    with EventLog(tmp_path / "r.db") as log:
        result = run_agent(spec, "go", registry=reg, client=client, log=log)
    assert result.status == "denied"
