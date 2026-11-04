"""Reversible PII sentinels (ADR-0025).

The contract: the model and the log see `[EMAIL_1]`, the tool sees the real
address, and the two are the same object for the length of one run.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from rigging.errors import ManifestError
from rigging.events import EventLog
from rigging.guardrails.pii import (
    DEFAULT_SENTINEL_STORE,
    PIIPseudonymiseGuardrail,
    SentinelMap,
    SentinelStore,
)
from rigging.loop import assemble, run_agent
from rigging.manifest import AgentSpec, GuardrailRef, Guardrails, Limits
from rigging.protocols import Allow, Deny, Transform
from rigging.registry import Registry
from rigging.testing import FakeClient
from rigging.types import Message, ModelResponse, ToolCall, Usage

EMAIL = "joel@example.com"


def _resp(content: str = "", calls: list[ToolCall] | None = None) -> ModelResponse:
    return ModelResponse(
        message=Message(role="assistant", content=content, tool_calls=calls or []),
        usage=Usage(input_tokens=1, output_tokens=1), finish_reason="stop", model="fake",
    )


def _spec(guardrails: Guardrails, tools: list[str] | None = None) -> AgentSpec:
    return AgentSpec(name="t", model="fake", tools=tools or [], limits=Limits(max_steps=3),
                     guardrails=guardrails)


@pytest.fixture(autouse=True)
def _clean_store() -> Any:
    """The process store is shared; isolate every test from the last one."""
    DEFAULT_SENTINEL_STORE.clear()
    yield
    DEFAULT_SENTINEL_STORE.clear()


# ------------------------------------------------------------------ the map

def test_the_same_value_gets_the_same_sentinel() -> None:
    sentinels = SentinelMap()
    assert sentinels.sentinelise(f"write to {EMAIL}") == "write to [EMAIL_1]"
    assert sentinels.sentinelise(f"again {EMAIL}") == "again [EMAIL_1]"


def test_distinct_values_get_distinct_sentinels() -> None:
    assert SentinelMap().sentinelise("a@b.co c@d.co") == "[EMAIL_1] [EMAIL_2]"


def test_round_trip_restores_the_value() -> None:
    sentinels = SentinelMap()
    assert sentinels.deanonymise(sentinels.sentinelise(f"mail {EMAIL}")) == f"mail {EMAIL}"


def test_an_unknown_sentinel_is_left_alone() -> None:
    """A model that invents `[EMAIL_9]` sends a literal, it does not crash."""
    assert SentinelMap().deanonymise("call [EMAIL_9]") == "call [EMAIL_9]"


def test_sentinels_are_markdown_safe() -> None:
    """`<EMAIL_1>` would be swallowed as an HTML tag by the web view."""
    assert "<" not in SentinelMap().sentinelise(EMAIL)


# ------------------------------------------------------------- the guardrail

def test_tool_arguments_are_restored_recursively() -> None:
    guardrail = PIIPseudonymiseGuardrail()
    guardrail.on_input([Message(role="user", content=EMAIL)])
    verdict = guardrail.on_tool_call(
        ToolCall(id="1", name="notify", arguments={"to": "[EMAIL_1]", "cc": ["[EMAIL_1]"]}),
        object(),
    )
    assert isinstance(verdict, Transform)
    assert verdict.value.arguments == {"to": EMAIL, "cc": [EMAIL]}


def test_a_placeholder_from_an_earlier_turn_is_denied() -> None:
    """The map is per run, so turn 2 cannot resolve turn 1's sentinel (ADR-0025)."""
    guardrail = PIIPseudonymiseGuardrail()
    verdict = guardrail.on_tool_call(
        ToolCall(id="1", name="notify", arguments={"to": "[EMAIL_1]"}), object())
    assert isinstance(verdict, Deny)
    assert "[EMAIL_1]" in verdict.reason


def test_tool_results_reuse_the_input_sentinel() -> None:
    guardrail = PIIPseudonymiseGuardrail()
    guardrail.on_input([Message(role="user", content=EMAIL)])
    verdict = guardrail.on_tool_result(
        ToolCall(id="1", name="lookup", arguments={}), f"customer {EMAIL}")
    assert isinstance(verdict, Transform)
    assert verdict.value == "customer [EMAIL_1]"


def test_output_keeps_the_sentinel_by_default() -> None:
    guardrail = PIIPseudonymiseGuardrail()
    guardrail.on_input([Message(role="user", content=EMAIL)])
    assert isinstance(guardrail.on_output("emailed [EMAIL_1]", []), Allow)


def test_restore_output_puts_the_value_back() -> None:
    guardrail = PIIPseudonymiseGuardrail(restore_output=True)
    guardrail.on_input([Message(role="user", content=EMAIL)])
    verdict = guardrail.on_output("emailed [EMAIL_1]", [])
    assert isinstance(verdict, Transform)
    assert verdict.value == f"emailed {EMAIL}"


# ------------------------------------------------------------ assembly rules

def test_one_instance_is_shared_across_hooks() -> None:
    """Four instances would mean four maps and four lifetimes (ADR-0025)."""
    registry = Registry()
    registry.add("guardrail", PIIPseudonymiseGuardrail.name, PIIPseudonymiseGuardrail)
    spec = _spec(Guardrails(
        on_input=[GuardrailRef(name="pii_pseudonymise")],
        on_tool_call=[GuardrailRef(name="pii_pseudonymise")],
        on_output=[GuardrailRef(name="pii_pseudonymise")],
    ))
    agent = assemble(spec, registry=registry)
    assert agent.guardrails["on_input"][0] is agent.guardrails["on_tool_call"][0]
    assert agent.guardrails["on_input"][0] is agent.guardrails["on_output"][0]


def test_a_guardrail_configured_twice_differently_is_an_error() -> None:
    registry = Registry()
    registry.add("guardrail", PIIPseudonymiseGuardrail.name, PIIPseudonymiseGuardrail)
    spec = _spec(Guardrails(
        on_input=[GuardrailRef(name="pii_pseudonymise", config={"restore_output": True})],
        on_output=[GuardrailRef(name="pii_pseudonymise", config={"restore_output": False})],
    ))
    with pytest.raises(ManifestError, match="configured differently"):
        assemble(spec, registry=registry)


# ---------------------------------------------------------------- end to end

def test_the_model_and_the_log_never_see_the_value(tmp_path: Path) -> None:
    """The money test: the tool gets the address, the database does not."""
    seen: dict[str, Any] = {}

    def notify(to: str) -> str:
        """Notify a customer."""
        seen["to"] = to
        return f"sent to {to}"

    notify.__rigging_tool__ = {  # type: ignore[attr-defined]
        "name": "notify", "description": "Notify.", "side_effecting": False,
    }

    registry = Registry()
    registry.add("guardrail", PIIPseudonymiseGuardrail.name, PIIPseudonymiseGuardrail)
    registry.add("tool", "notify", notify)
    spec = _spec(Guardrails(
        on_input=[GuardrailRef(name="pii_pseudonymise")],
        on_tool_call=[GuardrailRef(name="pii_pseudonymise")],
        on_tool_result=[GuardrailRef(name="pii_pseudonymise")],
    ), tools=["notify"])

    client = FakeClient([
        _resp(calls=[ToolCall(id="1", name="notify", arguments={"to": "[EMAIL_1]"})]),
        _resp(content="done"),
    ])
    db = tmp_path / "r.db"
    with EventLog(db) as log:
        result = run_agent(spec, f"email {EMAIL}", registry=registry, client=client, log=log)
        events = list(log.events(result.run_id))

    assert result.status == "ok"
    assert seen["to"] == EMAIL, "the tool must receive the real address"
    assert EMAIL.encode() not in db.read_bytes(), "the log is a security boundary"

    received = next(e for e in events if e["type"] == "input_received")
    assert received["payload"]["content"] == "email [EMAIL_1]"
    call = next(e for e in events if e["type"] == "tool_call")
    assert call["payload"]["arguments"] == {"to": "[EMAIL_1]"}
    tool_result = next(e for e in events if e["type"] == "tool_result")
    assert tool_result["payload"]["content"] == "sent to [EMAIL_1]"


def test_an_unresolvable_placeholder_never_reaches_the_tool(tmp_path: Path) -> None:
    """What actually happened in the demo: the model echoed a sentinel from an
    earlier turn, the map had never issued it, and the literal got written to the
    database. A tool call that cannot be resolved is now a denial."""
    called: list[str] = []

    def notify(to: str) -> str:
        """Notify."""
        called.append(to)
        return "sent"

    notify.__rigging_tool__ = {  # type: ignore[attr-defined]
        "name": "notify", "description": "Notify.", "side_effecting": False,
    }

    registry = Registry()
    registry.add("guardrail", PIIPseudonymiseGuardrail.name, PIIPseudonymiseGuardrail)
    registry.add("tool", "notify", notify)
    spec = _spec(Guardrails(on_tool_call=[GuardrailRef(name="pii_pseudonymise")]),
                 tools=["notify"])
    client = FakeClient([
        _resp(calls=[ToolCall(id="1", name="notify", arguments={"to": "[EMAIL_1]"})]),
        _resp(content="What address should I use?"),
    ])
    with EventLog(tmp_path / "r.db") as log:
        result = run_agent(spec, "confirmé", registry=registry, client=client, log=log)

    assert result.status == "ok"
    assert called == [], "the tool must not run with a literal placeholder"
    tool_msg = next(m for m in client.requests[1].messages if m.role == "tool")
    assert "[EMAIL_1]" in tool_msg.content
    assert "never issued" in tool_msg.content


# ------------------------------------------------------------- the store

def test_the_store_is_per_session() -> None:
    store = SentinelStore()
    assert store.get("a") is store.get("a")
    assert store.get("a") is not store.get("b")


def test_the_store_expires_an_idle_session() -> None:
    store = SentinelStore(ttl_s=10)
    store.get("a", now=0.0)
    store.get("b", now=20.0)
    assert "a" not in store


def test_the_store_drops_the_oldest_session_at_the_cap() -> None:
    store = SentinelStore(max_sessions=2)
    store.get("a", now=0.0)
    store.get("b", now=1.0)
    store.get("c", now=2.0)
    assert "a" not in store
    assert "c" in store


def test_forget_drops_a_session() -> None:
    store = SentinelStore()
    store.get("a")
    store.forget("a")
    assert "a" not in store


# ------------------------------------------------------- across turns

def _notify_registry(called: list[str]) -> Registry:
    def notify(to: str) -> str:
        """Notify."""
        called.append(to)
        return "sent"

    notify.__rigging_tool__ = {  # type: ignore[attr-defined]
        "name": "notify", "description": "Notify.", "side_effecting": False,
    }
    registry = Registry()
    registry.add("guardrail", PIIPseudonymiseGuardrail.name, PIIPseudonymiseGuardrail)
    registry.add("tool", "notify", notify)
    return registry


def test_a_placeholder_survives_into_the_next_turn(tmp_path: Path) -> None:
    """The failure that motivated ADR-0026: the address in turn 1, "confirmé" in turn 2."""
    called: list[str] = []
    registry = _notify_registry(called)
    spec = _spec(Guardrails(
        on_input=[GuardrailRef(name="pii_pseudonymise")],
        on_tool_call=[GuardrailRef(name="pii_pseudonymise")],
        on_tool_result=[GuardrailRef(name="pii_pseudonymise")],
    ), tools=["notify"])

    with EventLog(tmp_path / "r.db") as log:
        run_agent(spec, f"my email is {EMAIL}", registry=registry, log=log,
                  session_id="s_turn", client=FakeClient([_resp(content="noted")]))
        client = FakeClient([
            _resp(calls=[ToolCall(id="1", name="notify", arguments={"to": "[EMAIL_1]"})]),
            _resp(content="done"),
        ])
        result = run_agent(spec, "confirmé", registry=registry, log=log,
                           session_id="s_turn", client=client)

    assert result.status == "ok"
    assert called == [EMAIL], "turn 2 must resolve the sentinel turn 1 issued"


def test_a_placeholder_from_another_session_is_denied(tmp_path: Path) -> None:
    called: list[str] = []
    registry = _notify_registry(called)
    spec = _spec(Guardrails(
        on_input=[GuardrailRef(name="pii_pseudonymise")],
        on_tool_call=[GuardrailRef(name="pii_pseudonymise")],
    ), tools=["notify"])

    with EventLog(tmp_path / "r.db") as log:
        run_agent(spec, f"my email is {EMAIL}", registry=registry, log=log,
                  session_id="s_a", client=FakeClient([_resp(content="noted")]))
        client = FakeClient([
            _resp(calls=[ToolCall(id="1", name="notify", arguments={"to": "[EMAIL_1]"})]),
            _resp(content="which address?"),
        ])
        run_agent(spec, "notify the customer", registry=registry, log=log,
                  session_id="s_b", client=client)

    assert called == []
    tool_msg = next(m for m in client.requests[1].messages if m.role == "tool")
    assert "never issued" in tool_msg.content
