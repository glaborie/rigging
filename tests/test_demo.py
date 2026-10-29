"""The b2b-support demo pack: deterministic seed, order tools, retriever wiring."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import rigging.tools.demo  # noqa: F401  (registration side-effect)
from rigging.registry import DEFAULT_REGISTRY
from rigging.testing import FakeClient
from rigging.tools.schema import FunctionTool
from rigging.types import ModelRequest


@pytest.fixture()
def demo_db(tmp_path, monkeypatch):
    import sys
    sys.path.insert(0, str(Path(__file__).parents[1] / "examples/b2b-support"))
    import seed

    out = tmp_path / "demo.db"
    seed.build_db(out, seed=7)
    monkeypatch.setenv("RIGGING_DEMO_DB", str(out))
    return out


def _call(name: str, args: dict) -> str:
    ft = FunctionTool(DEFAULT_REGISTRY.get("tool", name))
    return ft.run(type("C", (), {"id": "1", "name": name, "arguments": args})()).content  # type: ignore[arg-type]


def test_seed_is_deterministic_and_chunked(tmp_path):
    import sys
    sys.path.insert(0, str(Path(__file__).parents[1] / "examples/b2b-support"))
    import seed

    a = seed.build_db(tmp_path / "a.db", seed=7)
    b = seed.build_db(tmp_path / "b.db", seed=7)
    assert a == b and a["chunks"] >= 8
    db = sqlite3.connect(tmp_path / "a.db")
    assert db.execute("SELECT count(*) FROM orders WHERE status='delivered'").fetchone()[0] >= 3


def test_lookup_and_pricing(demo_db):
    out = _call("lookup_order", {"order_id": "ORD-1001"})
    assert "ORD-1001" in out and ("delivered" in out or "shipped" in out)
    miss = _call("lookup_order", {"order_id": "ORD-9999"})
    assert miss.startswith("error:")
    pricing = _call("check_pricing", {"sku": "SW-24P-GBE", "qty": 250})
    assert '"tier_note"' in pricing  # 85% tier applies at 250 units


def test_refund_is_marked_side_effecting(demo_db):
    ft = FunctionTool(DEFAULT_REGISTRY.get("tool", "refund_order"))
    assert getattr(ft, "side_effecting", False) is True
    result = ft.run(type("C", (), {"id": "2", "name": "refund_order",
                                   "arguments": {"order_id": "ORD-1002", "amount": 99.0,
                                                 "reason": "test"}})())
    assert "refund approved" in result.content


def test_manifest_assembles_with_retriever_and_demo_tools(tmp_path, demo_db, monkeypatch):
    import yaml

    from rigging.loop import assemble
    from rigging.manifest import load_manifest

    d = tmp_path / "b2b.yaml"
    d.write_text(yaml.safe_dump({
        "name": "b2b", "model": "test", "system_prompt": "be terse",
        "tools": ["search_policies", "lookup_order", "check_pricing", "refund_order",
                  "escalate_to_human"],
        "retriever": {"name": "fts5", "options": {}},
        "limits": {"max_steps": 3},
    }))
    spec = load_manifest(str(d))
    agent = assemble(spec, client=FakeClient([]))
    names = {t.name for t in agent.tools}
    assert {"search_policies", "lookup_order", "check_pricing", "refund_order",
            "escalate_to_human"} <= names


def test_search_policies_finds_refund_window(demo_db):
    from rigging.retriever.fts5 import FTS5Retriever
    from rigging.tools.builtin import make_search_tool

    t = make_search_tool(FTS5Retriever(path=str(demo_db)))
    out = t.run(type("C", (), {"id": "9", "name": "search_policies",
                               "arguments": {"query": "refund window 30 days"}})()).content  # type: ignore[arg-type]
    assert "REF-001" in out or "30 calendar days" in out


def test_escalation_yields_structured_handoff(tmp_path, demo_db):
    from rigging.events import EventLog
    from rigging.loop import run_agent
    from rigging.manifest import load_manifest

    import yaml
    d = tmp_path / "esc.yaml"
    d.write_text(yaml.safe_dump({"name": "esc", "model": "test", "system_prompt": "be terse",
                                 "tools": ["escalate_to_human"], "limits": {"max_steps": 3}}))
    spec = load_manifest(str(d))
    client = FakeClient([
        {"role": "assistant", "content": "",
         "tool_calls": [{"id": "1", "name": "escalate_to_human",
                          "arguments": {"reason": "out of policy", "summary": "needs a human"}}]},
        {"role": "assistant", "content": "I have escalated this to a human colleague."},
    ])
    log = EventLog(tmp_path / "e.db")
    result = run_agent(spec, "this is out of policy", client=client, log=log)
    assert result.handoff is not None
    assert result.handoff.reason == "out of policy"
    assert result.handoff.transcript_run_id == result.run_id
    events = [e["type"] for e in log.events(result.run_id)]
    assert "escalation" in events


def test_replay_and_live_have_same_handoff_contract():  # documents the contract
    from rigging.types import Handoff

    h = Handoff(reason="r", summary="s", transcript_run_id="r_x")
    assert h.transcript_run_id == "r_x"
