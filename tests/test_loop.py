"""Loop invariants: tools never crash the run, side effects need approval, every
step is an event. Uses a fake client — no network, no model, deterministic.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from rigging import loop as loop_module
from rigging.approvals import AutoDenyApproval, ScriptedApproval
from rigging.errors import ManifestError, TransportError
from rigging.events import EventLog
from rigging.loop import assemble, run_agent
from rigging.manifest import AgentSpec, GuardrailRef, Guardrails, Limits
from rigging.protocols import Allow, Deny, Transform
from rigging.registry import Registry
from rigging.testing import FakeClient
from rigging.types import Message, ModelDelta, ModelRequest, ModelResponse, ToolCall, Usage


def _resp(content: str = "", calls: list[ToolCall] | None = None) -> ModelResponse:
    return ModelResponse(
        message=Message(role="assistant", content=content, tool_calls=calls or []),
        usage=Usage(input_tokens=10, output_tokens=5), finish_reason="stop", model="fake",
    )


def _spec(**kw: Any) -> AgentSpec:
    base = dict(name="t", model="fake", tools=[], limits=Limits(max_steps=3))
    base.update(kw)
    return AgentSpec(**base)


def test_settings_retry_budget_reaches_the_client(monkeypatch: Any) -> None:
    """RIGGING_MAX_RETRIES was dead config: the client kept its default 3."""
    import rigging.client.openai_compat as client_mod
    from rigging import loop

    captured: dict[str, Any] = {}

    class Capturing:
        def __init__(self, base_url: str, api_key: str = "not-needed", **kw: Any) -> None:
            captured.update(kw)

    monkeypatch.setattr(client_mod, "OpenAICompatClient", Capturing)
    monkeypatch.setattr(loop.settings, "max_retries", 7)

    loop.assemble(_spec(), registry=Registry())

    assert captured["max_retries"] == 7
    assert captured["timeout"] == loop.settings.request_timeout_s


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
        logged_usage = log.usage(result.run_id)
    assert result.status == "ok"
    assert result.output == "here is what I know"
    assert client.requests[-1].tools == []  # final answer call has tools disabled
    # The final-answer call is a real completion: its tokens belong to the run,
    # and the result must agree with the log's own projection.
    assert result.usage == logged_usage


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
        events = list(log.events(result.run_id))
        status = log.runs()[0]["status"]
    assert result.status == "denied"
    # A denied run still ends: the row is not left running, and the exit is an
    # event like every other one.
    assert status == "denied"
    finished = [e["payload"]["status"] for e in events if e["type"] == "run_finished"]
    assert finished == ["denied"]


def test_tool_result_guardrail_rewrite_is_logged(tmp_path: Path) -> None:
    """Hard rule 2: a guardrail that rewrites a tool result is a step in the log.

    prompt_injection is a Transform, so without this event the eval could not tell
    "the model ignored the poisoned note" from "the guardrail flagged it".
    """

    class Tagger:
        name = "tagger"
        hooks: tuple[str, ...] = ("on_tool_result",)

        def on_input(self, messages): return Allow()
        def on_tool_call(self, call, tool): return Allow()
        def on_tool_result(self, call, content): return Transform(f"[flagged]\n{content}", "flagged")
        def on_output(self, text, messages): return Allow()

    reg = _registry()
    reg.add("guardrail", "tagger", Tagger())
    spec = _spec(tools=["echo"],
                 guardrails=Guardrails(on_tool_result=[GuardrailRef(name="tagger")]))
    client = FakeClient([
        _resp("", calls=[ToolCall(id="1", name="echo", arguments={"text": "x"})]),
        _resp("done"),
    ])
    with EventLog(tmp_path / "r.db") as log:
        result = run_agent(spec, "go", registry=reg, client=client, log=log)
        events = list(log.events(result.run_id))

    verdicts = [e for e in events if e["type"] == "guardrail_verdict"
                and e["payload"].get("hook") == "on_tool_result"]
    assert verdicts, "the on_tool_result guardrail left no event"
    assert verdicts[0]["payload"]["checks"][0]["verdict"] == "Transform"
    tool_result = next(e for e in events if e["type"] == "tool_result")
    assert tool_result["payload"]["content"].startswith("[flagged]")


def test_output_guardrail_transform_reaches_the_result(tmp_path: Path) -> None:
    """ADR-0004: `on_output` may transform, not only deny.

    This is what makes `pii_redaction` mean anything on the output side; the
    transform used to be dropped by `_apply` and returned as `Allow`.
    """

    class Redactor:
        name = "redactor"
        hooks: tuple[str, ...] = ("on_output",)

        def on_input(self, messages: Any) -> Any:
            return Allow()

        def on_tool_call(self, call: Any, tool: Any) -> Any:
            return Allow()

        def on_tool_result(self, call: Any, content: str) -> Any:
            return Allow()

        def on_output(self, text: str, messages: Any) -> Any:
            return Transform(text.replace("a@b.co", "[email]"), "pii")

    reg = _registry()
    reg.add("guardrail", "redactor", Redactor)
    spec = _spec(guardrails=Guardrails(on_output=[GuardrailRef(name="redactor")]))
    with EventLog(tmp_path / "r.db") as log:
        result = run_agent(spec, "go", registry=reg,
                           client=FakeClient([_resp("mail me at a@b.co")]), log=log)
    assert result.output == "mail me at [email]"


def test_tool_call_guardrail_transform_rewrites_arguments(tmp_path: Path) -> None:
    """ADR-0004/architecture §3: `on_tool_call` may transform the args.

    Before the fix the transform was dropped and the tool ran with the model's
    original arguments.
    """

    class Rewriter:
        name = "rewriter"
        hooks: tuple[str, ...] = ("on_tool_call",)

        def on_input(self, messages: Any) -> Any:
            return Allow()

        def on_tool_call(self, call: Any, tool: Any) -> Any:
            return Transform({**call.arguments, "text": "rewritten"}, "rewrite")

        def on_tool_result(self, call: Any, content: str) -> Any:
            return Allow()

        def on_output(self, text: str, messages: Any) -> Any:
            return Allow()

    reg = _registry()
    reg.add("guardrail", "rewriter", Rewriter)
    spec = _spec(tools=["echo"],
                 guardrails=Guardrails(on_tool_call=[GuardrailRef(name="rewriter")]))
    client = FakeClient([
        _resp("", calls=[ToolCall(id="1", name="echo", arguments={"text": "original"})]),
        _resp("done"),
    ])
    with EventLog(tmp_path / "r.db") as log:
        result = run_agent(spec, "go", registry=reg, client=client, log=log)
        verdict = next(e for e in log.events(result.run_id)
                       if e["type"] == "guardrail_verdict"
                       and e["payload"].get("hook") == "on_tool_call")
    tool_msg = next(m for m in client.requests[1].messages if m.role == "tool")
    assert tool_msg.content == "rewritten"
    assert verdict["payload"]["checks"][-1]["arguments"] == {"text": "rewritten"}


def test_chained_input_transforms_keep_the_message_shape(tmp_path: Path) -> None:
    """Each guardrail in a chain must receive `Sequence[Message]`.

    `_apply` used to replace the message with the transformed *string*, so the
    second guardrail iterated characters and crashed the run.
    """

    class Suffix:
        hooks: tuple[str, ...] = ("on_input",)

        def __init__(self, name: str, suffix: str) -> None:
            self.name, self.suffix = name, suffix

        def on_input(self, messages: Any) -> Any:
            return Transform(messages[0].content + self.suffix, "suffix")

        def on_tool_call(self, call: Any, tool: Any) -> Any:
            return Allow()

        def on_tool_result(self, call: Any, content: str) -> Any:
            return Allow()

        def on_output(self, text: str, messages: Any) -> Any:
            return Allow()

    reg = _registry()
    reg.add("guardrail", "a", Suffix("a", " a"))
    reg.add("guardrail", "b", Suffix("b", " b"))

    spec = _spec(guardrails=Guardrails(
        on_input=[GuardrailRef(name="a"), GuardrailRef(name="b")]))
    with EventLog(tmp_path / "r.db") as log:
        result = run_agent(spec, "go", registry=reg,
                           client=FakeClient([_resp("done")]), log=log)
        received = next(e for e in log.events(result.run_id)
                        if e["type"] == "input_received")
    assert received["payload"]["content"] == "go a b"


def test_transport_error_ends_the_run_instead_of_escaping(tmp_path: Path) -> None:
    """ADR-0010: every run ends in a structured, inspectable failure.

    A gateway error used to propagate out of `run_agent`, leaving the run row
    `running` forever and `RunResult.status == "error"` unreachable.
    """

    class Broken:
        name = "broken"

        def complete(self, request: ModelRequest) -> ModelResponse:
            raise TransportError(503, "gateway down", attempts=3)

        def stream(self, request: ModelRequest, on_error: Any = None) -> Any:
            raise NotImplementedError

    with EventLog(tmp_path / "r.db") as log:
        result = run_agent(_spec(), "go", registry=_registry(), client=Broken(), log=log)
        events = list(log.events(result.run_id))
        row = log.runs()[0]
    assert result.status == "error"
    assert "503" in (result.error or "")
    assert row["status"] == "error"
    assert [e["type"] for e in events].count("run_finished") == 1
    assert any(e["type"] == "error" and e["payload"]["stage"] == "model_request"
               for e in events)


def test_the_loop_closes_a_log_it_created(tmp_path: Path, monkeypatch: Any) -> None:
    """`owns_log` was computed after `log = log or ...`, so it was always False."""

    class Counting(EventLog):
        closes = 0

        def close(self) -> None:
            type(self).closes += 1
            super().close()

    monkeypatch.setattr(loop_module, "EventLog", Counting)
    monkeypatch.setattr(loop_module.settings, "db_path", str(tmp_path / "owned.db"))
    result = loop_module.run_agent(_spec(), "go", registry=_registry(),
                                   client=FakeClient([_resp("done")]))
    assert result.status == "ok"
    assert Counting.closes == 1


def test_streaming_calls_reach_the_callback_and_the_result(tmp_path: Path) -> None:
    """The loop streams deltas and still folds them into one ModelResponse."""

    class Streaming:
        name = "streaming"

        def stream(self, request: ModelRequest, on_error: Any = None) -> Any:
            yield ModelDelta(text="he")
            yield ModelDelta(text="llo", finish_reason="stop",
                             usage=Usage(input_tokens=1, output_tokens=2))

        def complete(self, request: ModelRequest) -> ModelResponse:  # pragma: no cover
            raise AssertionError("stream= should use the stream path")

    seen: list[ModelDelta] = []
    with EventLog(tmp_path / "r.db") as log:
        result = run_agent(_spec(), "go", registry=_registry(), client=Streaming(),
                           log=log, stream=seen.append)
    assert result.output == "hello"
    assert [d.text for d in seen] == ["he", "llo"]
    assert result.usage.input_tokens == 1
    assert result.usage.output_tokens == 2


def test_guardrail_wired_to_an_undeclared_hook_fails_fast() -> None:
    """`.hooks` is a contract, not decoration: assembly refuses a mismatch."""

    class OutputOnly:
        name = "output_only"
        hooks: tuple[str, ...] = ("on_output",)

        def on_input(self, messages: Any) -> Any:
            return Allow()

        def on_tool_call(self, call: Any, tool: Any) -> Any:
            return Allow()

        def on_tool_result(self, call: Any, content: str) -> Any:
            return Allow()

        def on_output(self, text: str, messages: Any) -> Any:
            return Allow()

    reg = _registry()
    reg.add("guardrail", "output_only", OutputOnly)
    spec = _spec(guardrails=Guardrails(on_input=[GuardrailRef(name="output_only")]))
    with pytest.raises(ManifestError, match="declares"):
        assemble(spec, registry=reg, client=FakeClient([]))
