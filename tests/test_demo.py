"""The b2b-support demo pack: deterministic seed, order tools, retriever wiring."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

import rigging.tools.demo  # noqa: F401  (registration side-effect)
from rigging.registry import DEFAULT_REGISTRY
from rigging.testing import FakeClient
from rigging.tools.schema import FunctionTool


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
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"baskets", "basket_items"} <= tables


def test_lookup_and_pricing(demo_db):
    out = _call("lookup_order", {"order_id": "ORD-1001"})
    assert "ORD-1001" in out and ("delivered" in out or "shipped" in out)
    miss = _call("lookup_order", {"order_id": "ORD-9999"})
    assert miss.startswith("error:")
    pricing = _call("check_pricing", {"sku": "SW-24P-GBE", "qty": 250})
    assert '"tier_note"' in pricing  # 85% tier applies at 250 units


def test_search_catalogue_by_name_and_sku(demo_db):
    by_name = json.loads(_call("search_catalogue", {"query": "switch"}))
    assert by_name["returned"] >= 2
    assert all("switch" in p["name"].lower() for p in by_name["products"])

    exact = json.loads(_call("search_catalogue", {"query": "SW-24P-GBE"}))
    assert exact["products"][0]["sku"] == "SW-24P-GBE"  # an exact SKU ranks first

    plural = json.loads(_call("search_catalogue", {"query": "switches"}))
    assert {p["sku"] for p in plural["products"]} >= {"SW-8P-GBE", "SW-24P-GBE"}

    limited = json.loads(_call("search_catalogue", {"query": "", "limit": 3}))
    assert limited["returned"] == 3

    miss = json.loads(_call("search_catalogue", {"query": "definitely-not-a-product"}))
    assert miss["returned"] == 0 and "hint" in miss

    literal = json.loads(_call("search_catalogue", {"query": "%"}))
    assert literal["returned"] == 0  # LIKE wildcards are escaped, not expanded


def test_basket_lifecycle(demo_db):
    first = json.loads(_call("add_to_basket", {"sku": "SFP-10G-LR", "qty": 4}))
    basket = first["basket_id"]
    assert basket.startswith("b_")
    assert first["count"] == 1
    assert first["added"]["unit_price"] == 129.0
    assert first["subtotal"] == 516.0

    second = json.loads(_call("add_to_basket",
                              {"sku": "SW-24P-GBE", "qty": 2, "basket_id": basket}))
    assert second["count"] == 2

    # Adding again increments, and the tier is priced at the new line quantity.
    again = json.loads(_call("add_to_basket",
                             {"sku": "SFP-10G-LR", "qty": 6, "basket_id": basket}))
    line = next(i for i in again["items"] if i["sku"] == "SFP-10G-LR")
    assert line["qty"] == 10 and line["unit_price"] == 122.55

    updated = json.loads(_call("update_basket_item",
                               {"basket_id": basket, "sku": "SFP-10G-LR", "qty": 4}))
    line = next(i for i in updated["items"] if i["sku"] == "SFP-10G-LR")
    assert line["qty"] == 4 and line["unit_price"] == 129.0

    removed = json.loads(_call("update_basket_item",
                               {"basket_id": basket, "sku": "SW-24P-GBE", "qty": 0}))
    assert removed["count"] == 1
    assert [i["sku"] for i in removed["items"]] == ["SFP-10G-LR"]

    view = json.loads(_call("view_basket", {"basket_id": basket}))
    assert view["subtotal"] == 516.0

    cleared = json.loads(_call("clear_basket", {"basket_id": basket}))
    assert cleared["count"] == 0 and cleared["subtotal"] == 0


def test_basket_flags_policy_and_validates(demo_db):
    below = json.loads(_call("add_to_basket", {"sku": "SFP-10G-LR", "qty": 2}))
    assert below["added"]["below_moq"] is True

    over = json.loads(_call("add_to_basket", {"sku": "SW-24P-GBE", "qty": 100}))
    assert over["added"]["exceeds_stock"] is True
    assert below["basket_id"] != over["basket_id"]  # empty basket_id starts a new basket

    assert _call("add_to_basket", {"sku": "NOPE", "qty": 1}).startswith("error: unknown SKU")
    assert _call("add_to_basket", {"sku": "SFP-10G-LR", "qty": 0}).startswith("error:")
    assert _call("update_basket_item",
                 {"basket_id": below["basket_id"], "sku": "SW-24P-GBE",
                  "qty": 1}).startswith("error:")
    assert _call("update_basket_item",
                 {"basket_id": below["basket_id"], "sku": "SFP-10G-LR",
                  "qty": -1}).startswith("error:")


def test_basket_tables_are_created_on_an_old_seed(demo_db):
    """Existing demo.db files predate the basket tables; the tool creates them."""
    db = sqlite3.connect(demo_db)
    db.execute("DROP TABLE basket_items")
    db.execute("DROP TABLE baskets")
    db.commit()
    db.close()
    out = json.loads(_call("add_to_basket", {"sku": "SW-8P-GBE", "qty": 2}))
    assert out["count"] == 1


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
        "tools": ["search_policies", "lookup_order", "search_catalogue", "check_pricing",
                  "add_to_basket", "view_basket", "update_basket_item", "clear_basket",
                  "refund_order", "escalate_to_human"],
        "retriever": {"name": "fts5", "options": {}},
        "limits": {"max_steps": 3},
    }))
    spec = load_manifest(str(d))
    agent = assemble(spec, client=FakeClient([]))
    names = {t.name for t in agent.tools}
    assert {"search_policies", "lookup_order", "search_catalogue", "check_pricing",
            "add_to_basket", "view_basket", "update_basket_item", "clear_basket",
            "refund_order", "escalate_to_human"} <= names
    escalate = next(t for t in agent.tools if t.name == "escalate_to_human")
    facts = escalate.json_schema["function"]["parameters"]["properties"]["facts"]
    assert facts["type"] == "object"  # `dict | None` keeps its type in the schema


def test_search_policies_finds_refund_window(demo_db):
    from rigging.retriever.fts5 import FTS5Retriever
    from rigging.tools.builtin import make_search_tool

    t = make_search_tool(FTS5Retriever(path=str(demo_db)))
    out = t.run(type("C", (), {"id": "9", "name": "search_policies",
                               "arguments": {"query": "refund window 30 days"}})()).content  # type: ignore[arg-type]
    assert "REF-001" in out or "30 calendar days" in out


def test_escalation_yields_structured_handoff(tmp_path, demo_db):
    import yaml

    from rigging.events import EventLog
    from rigging.loop import run_agent
    from rigging.manifest import load_manifest
    d = tmp_path / "esc.yaml"
    d.write_text(yaml.safe_dump({"name": "esc", "model": "test", "system_prompt": "be terse",
                                 "tools": ["escalate_to_human"], "limits": {"max_steps": 3}}))
    spec = load_manifest(str(d))
    client = FakeClient([
        {"role": "assistant", "content": "",
         "tool_calls": [{"id": "1", "name": "escalate_to_human",
                          "arguments": {"reason": "out of policy", "summary": "needs a human",
                                        "facts": {"order_id": "ORD-1002", "amount": 112.5}}}]},
        {"role": "assistant", "content": "I have escalated this to a human colleague."},
    ])
    log = EventLog(tmp_path / "e.db")
    result = run_agent(spec, "this is out of policy", client=client, log=log)
    assert result.handoff is not None
    assert result.handoff.reason == "out of policy"
    assert result.handoff.facts == {"order_id": "ORD-1002", "amount": 112.5}
    assert result.handoff.transcript_run_id == result.run_id
    events = list(log.events(result.run_id))
    event = next(e for e in events if e["type"] == "escalation")
    assert event["payload"]["facts"] == {"order_id": "ORD-1002", "amount": 112.5}


def test_replay_and_live_have_same_handoff_contract():  # documents the contract
    from rigging.types import Handoff

    h = Handoff(reason="r", summary="s", transcript_run_id="r_x")
    assert h.transcript_run_id == "r_x"
