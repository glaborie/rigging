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
from rigging.types import RunContext

SESSION = "s_test"


@pytest.fixture()
def demo_db(tmp_path, monkeypatch):
    import sys
    sys.path.insert(0, str(Path(__file__).parents[1] / "examples/b2b-support"))
    import seed

    out = tmp_path / "demo.db"
    seed.build_db(out, seed=7)
    monkeypatch.setenv("RIGGING_DEMO_DB", str(out))
    return out


def _call(name: str, args: dict, *, session: str = SESSION) -> str:
    ft = FunctionTool(DEFAULT_REGISTRY.get("tool", name))
    call = type("C", (), {"id": "1", "name": name, "arguments": args})()
    return ft.run(call, RunContext(session_id=session, run_id="r_test")).content  # type: ignore[arg-type]


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
    assert {"session_baskets", "session_basket_items"} <= tables


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
    assert first["basket"] == SESSION
    assert first["count"] == 1
    assert first["added"]["unit_price"] == 129.0
    assert first["subtotal"] == 516.0

    second = json.loads(_call("add_to_basket", {"sku": "SW-24P-GBE", "qty": 2}))
    assert second["count"] == 2

    # Adding again increments, and the tier is priced at the new line quantity.
    again = json.loads(_call("add_to_basket", {"sku": "SFP-10G-LR", "qty": 6}))
    line = next(i for i in again["items"] if i["sku"] == "SFP-10G-LR")
    assert line["qty"] == 10 and line["unit_price"] == 122.55

    updated = json.loads(_call("update_basket_item", {"sku": "SFP-10G-LR", "qty": 4}))
    line = next(i for i in updated["items"] if i["sku"] == "SFP-10G-LR")
    assert line["qty"] == 4 and line["unit_price"] == 129.0

    removed = json.loads(_call("update_basket_item", {"sku": "SW-24P-GBE", "qty": 0}))
    assert removed["count"] == 1
    assert [i["sku"] for i in removed["items"]] == ["SFP-10G-LR"]

    view = json.loads(_call("view_basket", {}))
    assert view["subtotal"] == 516.0

    cleared = json.loads(_call("clear_basket", {}))
    assert cleared["count"] == 0 and cleared["subtotal"] == 0


def test_baskets_are_per_session(demo_db):
    """The loop's session id keys the basket; no id travels through the model."""
    _call("add_to_basket", {"sku": "SW-8P-GBE", "qty": 10}, session="s_one")
    _call("add_to_basket", {"sku": "SW-24P-GBE", "qty": 2}, session="s_two")

    one = json.loads(_call("view_basket", {}, session="s_one"))
    two = json.loads(_call("view_basket", {}, session="s_two"))
    assert [i["sku"] for i in one["items"]] == ["SW-8P-GBE"]
    assert [i["sku"] for i in two["items"]] == ["SW-24P-GBE"]

    assert json.loads(_call("clear_basket", {}, session="s_one"))["count"] == 0
    assert json.loads(_call("view_basket", {}, session="s_two"))["count"] == 1


def test_basket_tools_need_a_run_context(demo_db):
    """Called outside a run there is no session, and the failure is a tool message."""
    ft = FunctionTool(DEFAULT_REGISTRY.get("tool", "view_basket"))
    call = type("C", (), {"id": "1", "name": "view_basket", "arguments": {}})()
    result = ft.run(call)  # type: ignore[arg-type]
    assert result.is_error and "run context" in result.content


def test_basket_flags_policy_and_validates(demo_db):
    below = json.loads(_call("add_to_basket", {"sku": "SFP-10G-LR", "qty": 2},
                             session="s_below"))
    assert below["added"]["below_moq"] is True

    over = json.loads(_call("add_to_basket", {"sku": "SW-24P-GBE", "qty": 100},
                            session="s_over"))
    assert over["added"]["exceeds_stock"] is True

    assert _call("add_to_basket", {"sku": "NOPE", "qty": 1}).startswith("error: unknown SKU")
    assert _call("add_to_basket", {"sku": "SFP-10G-LR", "qty": 0}).startswith("error:")
    assert _call("update_basket_item", {"sku": "SW-24P-GBE", "qty": 1},
                 session="s_below").startswith("error:")
    assert _call("update_basket_item", {"sku": "SFP-10G-LR", "qty": -1},
                 session="s_below").startswith("error:")


def test_basket_tables_are_created_on_an_old_seed(demo_db):
    """Existing demo.db files predate the session baskets; the tool creates them."""
    db = sqlite3.connect(demo_db)
    db.execute("DROP TABLE session_basket_items")
    db.execute("DROP TABLE session_baskets")
    db.commit()
    db.close()
    out = json.loads(_call("add_to_basket", {"sku": "SW-8P-GBE", "qty": 2}))
    assert out["count"] == 1


def test_legacy_baskets_are_renamed_not_dropped(demo_db):
    """The pre-ADR-0024 table was keyed by basket_id; its rows stay inspectable."""
    db = sqlite3.connect(demo_db)
    db.executescript("""
    DROP TABLE session_basket_items;
    DROP TABLE session_baskets;
    CREATE TABLE baskets(basket_id TEXT PRIMARY KEY, created_at TEXT NOT NULL);
    CREATE TABLE basket_items(basket_id TEXT NOT NULL, sku TEXT NOT NULL,
                              qty INTEGER NOT NULL, added_at TEXT NOT NULL,
                              PRIMARY KEY (basket_id, sku));
    INSERT INTO baskets VALUES ('b_legacy', '2026-11-04T00:00:00+00:00');
    INSERT INTO basket_items VALUES ('b_legacy', 'SW-8P-GBE', 4, '2026-11-04T00:00:00+00:00');
    """)
    db.commit()
    db.close()

    out = json.loads(_call("add_to_basket", {"sku": "SW-8P-GBE", "qty": 1}))
    assert out["count"] == 1, "the legacy basket is not the session's basket"
    db = sqlite3.connect(demo_db)
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"baskets_legacy", "basket_items_legacy"} <= tables
    assert db.execute("SELECT qty FROM basket_items_legacy").fetchone()[0] == 4


def test_submit_basket_creates_a_multiline_order(demo_db):
    first = json.loads(_call("add_to_basket", {"sku": "SW-8P-GBE", "qty": 10}))
    assert first["count"] == 1
    added = json.loads(_call("add_to_basket", {"sku": "SW-24P-GBE", "qty": 10}))
    assert added["count"] == 2

    out = json.loads(_call("submit_basket", {}))
    assert out["order_id"].startswith("ORD-")
    assert out["status"] == "processing"
    assert out["total"] == added["subtotal"] == 3591.0
    assert out["lead_days"] == 15, "a mixed basket quotes the slowest item"
    assert out["basket"] == SESSION

    lines = json.loads(_call("lookup_order", {"order_id": out["order_id"]}))
    assert {line["sku"] for line in lines} == {"SW-8P-GBE", "SW-24P-GBE"}
    assert all(line["status"] == "processing" for line in lines)
    assert {line["note"] for line in lines} == {f"from basket {SESSION}"}

    # the basket is consumed, and submitting it again is an error, not a second order
    assert json.loads(_call("view_basket", {}))["count"] == 0
    assert _call("submit_basket", {}).startswith("error:")


def test_parallel_adds_share_the_session_basket(tmp_path, demo_db):
    """Two `add_to_basket` calls in one model response: one basket, two lines (ADR-0024)."""
    import yaml

    from rigging.events import EventLog
    from rigging.loop import run_agent
    from rigging.manifest import load_manifest

    d = tmp_path / "b2b.yaml"
    d.write_text(yaml.safe_dump({
        "name": "b2b", "model": "test", "system_prompt": "be terse",
        "tools": ["add_to_basket", "view_basket"],
        "parallel_tools": True,
        "limits": {"max_steps": 3},
    }))
    client = FakeClient([
        {"role": "assistant", "content": "",
         "tool_calls": [
             {"id": "1", "name": "add_to_basket",
              "arguments": {"sku": "SW-8P-GBE", "qty": 10}},
             {"id": "2", "name": "add_to_basket",
              "arguments": {"sku": "SW-24P-GBE", "qty": 2}},
         ]},
        {"role": "assistant", "content": "Both lines are in the basket."},
    ])
    with EventLog(tmp_path / "e.db") as log:
        result = run_agent(load_manifest(str(d)), "add both", client=client, log=log)
        session_id = log.runs()[0]["session_id"]

    assert result.status == "ok"
    view = json.loads(_call("view_basket", {}, session=session_id))
    assert {item["sku"] for item in view["items"]} == {"SW-8P-GBE", "SW-24P-GBE"}
    db = sqlite3.connect(demo_db)
    assert db.execute("SELECT count(*) FROM session_baskets WHERE session_id = ?",
                      (session_id,)).fetchone()[0] == 1


def test_submit_basket_refuses_moq_and_stock_violations(demo_db):
    below = json.loads(_call("add_to_basket", {"sku": "SFP-10G-LR", "qty": 1},
                             session="s_below"))
    assert below["count"] == 1
    out = _call("submit_basket", {}, session="s_below")
    assert out.startswith("error:") and "MOQ" in out

    over = json.loads(_call("add_to_basket", {"sku": "KVM-IP-16", "qty": 19},
                            session="s_over"))
    assert over["count"] == 1
    out = _call("submit_basket", {}, session="s_over")
    assert out.startswith("error:") and "exceeds stock" in out

    # a refused submit leaves the basket alone, ready for update_basket_item
    assert json.loads(_call("view_basket", {}, session="s_below"))["count"] == 1


def test_submit_basket_is_side_effecting(demo_db):
    ft = FunctionTool(DEFAULT_REGISTRY.get("tool", "submit_basket"))
    assert getattr(ft, "side_effecting", False) is True


def test_submit_basket_upgrades_a_single_line_orders_table(demo_db):
    """An older seed keyed orders by order_id alone; the tool rebuilds it in place."""
    db = sqlite3.connect(demo_db)
    db.executescript("""
    ALTER TABLE orders RENAME TO orders_single_line;
    CREATE TABLE orders(order_id TEXT PRIMARY KEY, sku TEXT, qty INTEGER,
                        unit_price REAL, status TEXT, delivered_at TEXT, note TEXT);
    INSERT INTO orders SELECT order_id, sku, qty, unit_price, status, delivered_at, note
      FROM orders_single_line;
    DROP TABLE orders_single_line;
    """)
    db.commit()
    db.close()

    basket = json.loads(_call("add_to_basket", {"sku": "SW-8P-GBE", "qty": 2}))
    assert basket["count"] == 1
    out = json.loads(_call("submit_basket", {}))
    assert out["order_id"] == "ORD-1013"
    assert "ORD-1001" in _call("lookup_order", {"order_id": "ORD-1001"})
    assert len(json.loads(_call("lookup_order", {"order_id": out["order_id"]}))) == 1


def test_refund_is_marked_side_effecting(demo_db):
    ft = FunctionTool(DEFAULT_REGISTRY.get("tool", "refund_order"))
    assert getattr(ft, "side_effecting", False) is True
    result = ft.run(type("C", (), {"id": "2", "name": "refund_order",
                                   "arguments": {"order_id": "ORD-1002", "amount": 99.0,
                                                 "reason": "test"}})())
    assert "refund approved" in result.content


def test_lookup_returns_the_contact(demo_db):
    """The contact is what `pii_pseudonymise` turns into [EMAIL_1] (ADR-0025)."""
    rows = json.loads(_call("lookup_order", {"order_id": "ORD-1001"}))
    assert rows[0]["contact"].endswith(".ch")


def test_notify_customer_is_side_effecting(demo_db):
    ft = FunctionTool(DEFAULT_REGISTRY.get("tool", "notify_customer"))
    assert getattr(ft, "side_effecting", False) is True
    out = _call("notify_customer", {"contact": "anna.meier@nordwind-ag.ch",
                                    "message": "your order shipped"})
    assert "anna.meier@nordwind-ag.ch" in out


def test_notify_customer_persists_the_real_address(demo_db):
    """The address the customer gave is data, not a placeholder: the row carries it.

    The eval suite can only see `[EMAIL_1]` (the log is redacted, ADR-0025), so
    this is the layer that proves the value itself was stored — the same split the
    suite uses for refunds: the tool test owns the row, the eval owns the call.
    """
    import sqlite3

    _call("notify_customer", {"contact": "anna.meier@nordwind-ag.ch",
                              "message": "flagged for priority"})
    rows = sqlite3.connect(demo_db).execute(
        "select contact, message from notifications order by id desc limit 1").fetchone()
    assert rows == ("anna.meier@nordwind-ag.ch", "flagged for priority")


def test_a_contact_is_sealed_at_rest_when_a_key_is_set(demo_db, monkeypatch):
    """ADR-0027: the row is ciphertext, and `lookup_order` still hands it back."""
    from rigging.config import settings
    from rigging.crypto import new_key

    monkeypatch.setattr(settings, "pii_key", new_key())
    _call("notify_customer", {"contact": "anna.meier@nordwind-ag.ch", "message": "x"})

    import sqlite3
    stored = sqlite3.connect(demo_db).execute(
        "select contact from notifications order by id desc limit 1").fetchone()[0]
    assert stored.startswith("enc:v1:") and "anna.meier" not in stored
    assert "anna.meier@nordwind-ag.ch" in _call("notify_customer",
                                                {"contact": "anna.meier@nordwind-ag.ch",
                                                 "message": "y"})

    # An order written with a key reads back through the tool, not as ciphertext.
    _call("add_to_basket", {"sku": "SW-8P-GBE", "qty": 2})
    order = json.loads(_call("submit_basket", {"contact": "joe@wh.com"}))
    rows = sqlite3.connect(demo_db).execute(
        "select contact from orders where order_id = ?", (order["order_id"],)).fetchall()
    assert rows and all(r[0].startswith("enc:v1:") for r in rows), "sealed in the file"
    assert json.loads(_call("lookup_order", {"order_id": order["order_id"]}))[0]["contact"] \
        == "joe@wh.com", "and opened for the agent"


def test_a_sealed_contact_without_the_key_is_a_missing_contact_not_a_crash(demo_db, monkeypatch):
    from rigging.config import settings
    from rigging.crypto import new_key

    monkeypatch.setattr(settings, "pii_key", new_key())
    _call("add_to_basket", {"sku": "SW-8P-GBE", "qty": 2})
    order = json.loads(_call("submit_basket", {"contact": "joe@wh.com"}))

    monkeypatch.setattr(settings, "pii_key", new_key())  # a different process, new key
    rows = json.loads(_call("lookup_order", {"order_id": order["order_id"]}))
    assert rows[0]["contact"] is None


def test_notify_customer_rejects_a_non_address(demo_db):
    assert _call("notify_customer", {"contact": "[EMAIL_9]", "message": "x"}).startswith("error:")


def test_submit_basket_records_the_contact(demo_db):
    """An order created from a basket used to have no contact, so there was
    nobody for `notify_customer` to email."""
    _call("add_to_basket", {"sku": "SW-8P-GBE", "qty": 2})
    out = json.loads(_call("submit_basket", {"contact": "joe@wh.com"}))
    rows = json.loads(_call("lookup_order", {"order_id": out["order_id"]}))
    assert rows[0]["contact"] == "joe@wh.com"


def test_manifest_assembles_with_retriever_and_demo_tools(tmp_path, demo_db, monkeypatch):
    import yaml

    from rigging.loop import assemble
    from rigging.manifest import load_manifest

    d = tmp_path / "b2b.yaml"
    d.write_text(yaml.safe_dump({
        "name": "b2b", "model": "test", "system_prompt": "be terse",
        "tools": ["search_policies", "lookup_order", "search_catalogue", "check_pricing",
                  "add_to_basket", "view_basket", "update_basket_item", "clear_basket",
                  "submit_basket", "refund_order", "escalate_to_human"],
        "retriever": {"name": "fts5", "options": {}},
        "limits": {"max_steps": 3},
    }))
    spec = load_manifest(str(d))
    agent = assemble(spec, client=FakeClient([]))
    names = {t.name for t in agent.tools}
    assert {"search_policies", "lookup_order", "search_catalogue", "check_pricing",
            "add_to_basket", "view_basket", "update_basket_item", "clear_basket",
            "submit_basket", "refund_order", "escalate_to_human"} <= names
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
