"""Guardrail + invariant tests: the rules the engine must own."""

from __future__ import annotations

import datetime as dt
import sqlite3
from pathlib import Path

import yaml

from rigging.guardrails.refund_window import RefundWindowGuardrail
from rigging.protocols import Allow, Deny
from rigging.types import ToolCall


def _db(tmp_path, delivered: str | None) -> Path:
    p = tmp_path / "demo.db"
    db = sqlite3.connect(p)
    db.execute("CREATE TABLE orders(order_id TEXT PRIMARY KEY, delivered_at TEXT)")
    db.execute("INSERT INTO orders VALUES ('ORD-1', ?)", (delivered,))
    db.commit()
    db.close()
    return p


def _call(amount: float = 10.0, order: str = "ORD-1") -> ToolCall:
    return ToolCall(id="1", name="refund_order", arguments={"order_id": order, "amount": amount})


def test_window_allows_recent_delivery(tmp_path):
    g = RefundWindowGuardrail(db_path=str(_db(tmp_path, dt.date.today().isoformat())))
    assert isinstance(g.on_tool_call(_call(), object()), Allow)


def test_window_denies_old_delivery(tmp_path):
    old = (dt.date.today() - dt.timedelta(days=300)).isoformat()
    g = RefundWindowGuardrail(db_path=str(_db(tmp_path, old)))
    verdict = g.on_tool_call(_call(), object())
    assert isinstance(verdict, Deny) and "outside the 30-day refund window" in verdict.reason


def test_window_denies_missing_delivery_date(tmp_path):
    g = RefundWindowGuardrail(db_path=str(_db(tmp_path, None)))
    verdict = g.on_tool_call(_call(), object())
    assert isinstance(verdict, Deny) and "no delivery date" in verdict.reason


def test_window_ignores_other_tools(tmp_path):
    g = RefundWindowGuardrail(db_path=str(_db(tmp_path, "2020-01-01")))
    assert isinstance(g.on_tool_call(ToolCall(id="2", name="lookup_order", arguments={}), object()), Allow)


def test_invariant_no_unapproved_side_effect(tmp_path):
    from rigging.eval import _check_invariant

    ok_events = [
        {"type": "approval_requested", "payload": {"tool": "refund_order"}},
        {"type": "approval_verdict", "payload": {"tool": "refund_order", "approved": True}},
        {"type": "tool_result", "payload": {"name": "refund_order", "denied": False, "is_error": False}},
    ]
    assert _check_invariant("no_unapproved_side_effect", ok_events) is None

    bad_events = [
        {"type": "approval_requested", "payload": {"tool": "refund_order"}},
        {"type": "approval_verdict", "payload": {"tool": "refund_order", "approved": False}},
        {"type": "tool_result", "payload": {"name": "refund_order", "denied": False, "is_error": False}},
    ]
    assert "without a recorded approval" in (_check_invariant("no_unapproved_side_effect", bad_events) or "")


def test_demo_manifest_wires_the_window_guardrail(tmp_path):
    from rigging.manifest import load_manifest

    root = Path(__file__).parents[1]
    spec = load_manifest(str(root / "examples/b2b-support/agent.yaml"))
    names = [g.name for g in spec.guardrails.on_tool_call]
    assert names == ["consequential_action", "refund_window"]
