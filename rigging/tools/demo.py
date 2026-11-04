"""Order-desk tools for the b2b-support demo (v0.3).

Registered on the default registry when a manifest asks for them; backed by the
seeded SQLite fixture DB (`examples/b2b-support/demo.db`, built by seed.py).
`refund_order` and `submit_basket` are side-effecting: the consequential-action
guardrail and the approval policy gate them before these functions ever run
(ADR-0004, ADR-0007).
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from rigging.registry import DEFAULT_REGISTRY, tool
from rigging.types import RunContext


def _db() -> sqlite3.Connection:
    env = os.environ.get("RIGGING_DEMO_DB", "")
    path = Path(env) if env else Path(__file__).parents[2] / "examples/b2b-support/demo.db"
    if not path.exists():
        raise RuntimeError(
            f"demo database not found at {path}. Seed it first: "
            "uv run python examples/b2b-support/seed.py")
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    return db


def _money(x: float) -> float:
    return round(x, 2)


def _tier(list_price: float, qty: int) -> float:
    if qty >= 200:
        return 0.85
    if qty >= 50:
        return 0.90
    if qty >= 10:
        return 0.95
    return 1.0


BASKET_SCHEMA = """
CREATE TABLE IF NOT EXISTS session_baskets (
  session_id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS session_basket_items (
  session_id TEXT NOT NULL,
  sku        TEXT NOT NULL,
  qty        INTEGER NOT NULL,
  added_at   TEXT NOT NULL,
  PRIMARY KEY (session_id, sku)
);
"""


def _basket_db() -> sqlite3.Connection:
    """The demo DB plus the session-basket tables, created lazily in old databases.

    Baskets used to be keyed by a generated id the model had to thread through
    every call; they are now one per session (ADR-0024). A pre-existing
    `baskets` table is renamed, not dropped: those rows are scratch state, but
    there is no reason to destroy someone's half-built basket to make room.
    """
    db = _db()
    tables = {row["name"] for row in db.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'")}
    if "baskets" in tables and "session_baskets" not in tables:
        db.execute("ALTER TABLE baskets RENAME TO baskets_legacy")
    if "basket_items" in tables and "session_basket_items" not in tables:
        db.execute("ALTER TABLE basket_items RENAME TO basket_items_legacy")
    db.executescript(BASKET_SCHEMA)
    db.commit()
    return db


def _session(ctx: RunContext | None) -> str:
    if ctx is None:
        raise RuntimeError("basket tools need a run context: the loop supplies the session id")
    return ctx.session_id


def _ensure_multiline_orders(db: sqlite3.Connection) -> None:
    """Bring an older seeded database up to the current orders schema.

    An order can have several lines; old seeds keyed it by `order_id` alone, and
    pre-ADR-0025 seeds have no `contact`. Both upgrades are idempotent and keep
    every row: `lookup_order` already returns a list, so only the key changes.
    """
    row = db.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'orders'"
    ).fetchone()
    if row is None:
        return
    columns = {r["name"] for r in db.execute("PRAGMA table_info(orders)")}
    if "PRIMARY KEY (order_id, sku)" not in row["sql"]:
        contact = "contact" if "contact" in columns else "NULL"
        db.executescript(f"""
        ALTER TABLE orders RENAME TO orders_single_line;
        CREATE TABLE orders(order_id TEXT, sku TEXT, qty INTEGER,
                            unit_price REAL, status TEXT, delivered_at TEXT, note TEXT,
                            contact TEXT,
                            PRIMARY KEY (order_id, sku));
        INSERT INTO orders(order_id, sku, qty, unit_price, status, delivered_at, note, contact)
          SELECT order_id, sku, qty, unit_price, status, delivered_at, note, {contact}
          FROM orders_single_line;
        DROP TABLE orders_single_line;
        """)
        db.commit()
        columns.add("contact")
    if "contact" not in columns:
        db.execute("ALTER TABLE orders ADD COLUMN contact TEXT")
        db.commit()


def _now() -> str:
    return _dt.datetime.now(_dt.UTC).isoformat(timespec="seconds")


def _line(db: sqlite3.Connection, sku: str, qty: int) -> dict[str, Any]:
    product = db.execute("SELECT * FROM products WHERE sku = ?", (sku,)).fetchone()
    if product is None:  # the catalogue is fixed; a stale basket is a bug, not input
        return {"sku": sku, "qty": qty, "missing": True}
    unit = _money(_tier(product["list_price"], qty) * product["list_price"])
    return {
        "sku": sku, "name": product["name"], "qty": qty,
        "unit_price": unit, "line_total": _money(unit * qty),
        "list_price": product["list_price"], "moq": product["moq"],
        "below_moq": qty < product["moq"], "stock": product["stock"],
        "exceeds_stock": qty > product["stock"], "lead_days": product["lead_days"],
    }


def _view(db: sqlite3.Connection, session_id: str) -> dict[str, Any]:
    rows = db.execute(
        "SELECT sku, qty FROM session_basket_items WHERE session_id = ? ORDER BY sku",
        (session_id,),
    ).fetchall()
    items = [_line(db, row["sku"], row["qty"]) for row in rows]
    return {
        "basket": session_id, "items": items, "count": len(items),
        "subtotal": _money(sum(float(item.get("line_total", 0.0)) for item in items)),
    }


@tool()
def add_to_basket(sku: str, qty: int, ctx: RunContext) -> str:
    """Add qty of a SKU to this session's basket. The basket is per session; no id needed."""
    if qty <= 0:
        return f"error: qty must be positive (got {qty})"
    session_id = _session(ctx)
    with closing(_basket_db()) as db:
        if db.execute("SELECT 1 FROM products WHERE sku = ?", (sku,)).fetchone() is None:
            return f"error: unknown SKU {sku!r}"
        db.execute("INSERT OR IGNORE INTO session_baskets(session_id, created_at) VALUES (?,?)",
                   (session_id, _now()))
        db.execute(
            "INSERT INTO session_basket_items(session_id, sku, qty, added_at) VALUES (?,?,?,?)"
            " ON CONFLICT(session_id, sku) DO UPDATE SET qty = qty + excluded.qty",
            (session_id, sku, qty, _now()),
        )
        db.commit()
        total = db.execute(
            "SELECT qty FROM session_basket_items WHERE session_id = ? AND sku = ?",
            (session_id, sku)).fetchone()["qty"]
        return json.dumps({"added": _line(db, sku, total), **_view(db, session_id)}, indent=1)


@tool()
def view_basket(ctx: RunContext) -> str:
    """Show this session's basket: lines, tier unit prices, MOQ/stock flags and the subtotal."""
    with closing(_basket_db()) as db:
        return json.dumps(_view(db, _session(ctx)), indent=1)


@tool()
def update_basket_item(sku: str, qty: int, ctx: RunContext) -> str:
    """Set a line in this session's basket to qty; qty=0 removes it. Use add_to_basket for new lines."""
    if qty < 0:
        return f"error: qty must be zero or positive (got {qty})"
    session_id = _session(ctx)
    with closing(_basket_db()) as db:
        if qty == 0:
            removed = db.execute(
                "DELETE FROM session_basket_items WHERE session_id = ? AND sku = ?",
                (session_id, sku)).rowcount
            db.commit()
            if not removed:
                return f"error: {sku!r} is not in this session's basket"
            return json.dumps({"removed": sku, **_view(db, session_id)}, indent=1)
        if db.execute("SELECT 1 FROM products WHERE sku = ?", (sku,)).fetchone() is None:
            return f"error: unknown SKU {sku!r}"
        updated = db.execute(
            "UPDATE session_basket_items SET qty = ? WHERE session_id = ? AND sku = ?",
            (qty, session_id, sku)).rowcount
        db.commit()
        if not updated:
            return (f"error: {sku!r} is not in this session's basket; "
                    "use add_to_basket for a new line")
        return json.dumps({"updated": _line(db, sku, qty), **_view(db, session_id)}, indent=1)


@tool()
def clear_basket(ctx: RunContext) -> str:
    """Remove every line from this session's basket."""
    session_id = _session(ctx)
    with closing(_basket_db()) as db:
        removed = db.execute(
            "DELETE FROM session_basket_items WHERE session_id = ?", (session_id,)).rowcount
        db.commit()
        return json.dumps({"removed": removed, **_view(db, session_id)}, indent=1)


def _next_order_id(db: sqlite3.Connection) -> str:
    numbers = [
        int(row["order_id"][4:])
        for row in db.execute("SELECT order_id FROM orders")
        if row["order_id"].startswith("ORD-") and row["order_id"][4:].isdigit()
    ]
    return f"ORD-{max(numbers, default=1000) + 1}"


@tool(side_effecting=True)
def submit_basket(ctx: RunContext, contact: str = "") -> str:
    """Convert this session's basket into an order. Requires approval. Every line must
    meet its MOQ and fit stock; the order is recorded as `processing`, unpaid and unshipped.

    `contact` is the customer's email for this order. Pass it exactly as it appeared
    in the conversation: a placeholder like [EMAIL_1] is restored before this runs,
    so the order gets the real address and the model never sees it (ADR-0025)."""
    session_id = _session(ctx)
    with closing(_basket_db()) as db:
        _ensure_multiline_orders(db)
        basket = _view(db, session_id)
        if not basket["items"]:
            return "error: this session's basket is empty — add lines with add_to_basket"
        missing = [item["sku"] for item in basket["items"] if item.get("missing")]
        if missing:
            return (f"error: {', '.join(missing)} is no longer in the catalogue — "
                    "remove the line with update_basket_item before submitting")
        problems = [
            f"{item['sku']}: qty {item['qty']} is below MOQ {item['moq']}"
            if item.get("below_moq") else
            f"{item['sku']}: qty {item['qty']} exceeds stock {item['stock']}"
            if item.get("exceeds_stock") else ""
            for item in basket["items"]
        ]
        problems = [problem for problem in problems if problem]
        if problems:
            return ("error: cannot submit this basket — " + "; ".join(problems)
                    + ". Fix the lines with update_basket_item, then submit again.")

        order_id = _next_order_id(db)
        try:
            db.executemany(
                "INSERT INTO orders(order_id, sku, qty, unit_price, status, delivered_at, note,"
                " contact) VALUES (?,?,?,?,?,?,?,?)",
                [(order_id, item["sku"], item["qty"], item["unit_price"],
                  "processing", None, f"from basket {session_id}", contact or None)
                 for item in basket["items"]],
            )
            db.execute("DELETE FROM session_basket_items WHERE session_id = ?", (session_id,))
            db.execute("DELETE FROM session_baskets WHERE session_id = ?", (session_id,))
            db.commit()
        except sqlite3.Error as exc:
            db.rollback()
            return f"error: could not record the order ({exc}); the basket is unchanged"

        return json.dumps({
            "order_id": order_id, "status": "processing", "currency": "USD",
            "basket": session_id, "lines": basket["items"], "total": basket["subtotal"],
            "lead_days": max(item["lead_days"] for item in basket["items"]),
            "note": "Order recorded. No payment was taken and nothing has shipped.",
        }, indent=1)


@tool()
def lookup_order(order_id: str) -> str:
    """Look up a customer order by id (e.g. ORD-1001): items, status, delivery."""
    with closing(_db()) as db:
        rows = db.execute("SELECT * FROM orders WHERE order_id = ?", (order_id,)).fetchall()
    if not rows:
        return f"error: no order {order_id!r} in the system"
    return json.dumps([dict(r) for r in rows], indent=1)


def _like_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


@tool()
def search_catalogue(query: str = "", limit: int = 10) -> str:
    """Search the product catalogue by SKU or name; an empty query browses the first items."""
    limit = max(1, min(int(limit), 50))
    query = query.strip()
    # "switches" should find "8-port gigabit switch"; customers rarely type the
    # exact catalogue noun. Try the query and its common singular forms.
    variants = sorted({query, query.rstrip("s"), query.rstrip("es")} - {""}) or [""]
    clauses = " OR ".join(["sku LIKE ? ESCAPE '\\' OR name LIKE ? ESCAPE '\\'"] * len(variants))
    params: list[object] = []
    for variant in variants:
        pattern = f"%{_like_escape(variant)}%"
        params.extend((pattern, pattern))
    params.extend((query, limit))
    with closing(_db()) as db:
        rows = db.execute(
            "SELECT sku, name, list_price, moq, lead_days, stock FROM products"
            f" WHERE {clauses}"
            " ORDER BY CASE WHEN lower(sku)=lower(?) THEN 0 ELSE 1 END, sku"
            " LIMIT ?",
            params,
        ).fetchall()
    products = [dict(r) for r in rows]
    payload: dict[str, object] = {
        "query": query, "returned": len(products), "products": products,
    }
    if not products:
        payload["hint"] = "No match. Try a shorter product name or a SKU prefix."
    return json.dumps(payload, indent=1)


@tool()
def check_pricing(sku: str, qty: int) -> str:
    """Price a quantity of a SKU: unit price at the volume tier, MOQ, lead time, stock."""
    with closing(_db()) as db:
        row = db.execute("SELECT * FROM products WHERE sku = ?", (sku,)).fetchone()
    if not row:
        return f"error: unknown SKU {sku!r}"
    unit = _tier(row["list_price"], qty) * row["list_price"]
    return json.dumps({
        "sku": sku, "qty": qty, "unit_price": round(unit, 2),
        "total": round(unit * qty, 2), "moq": row["moq"], "lead_days": row["lead_days"],
        "stock": row["stock"],
        "tier_note": "policy PRI-001: tiers are per line item, cannot be combined",
    }, indent=1)


@tool(side_effecting=True)
def refund_order(order_id: str, amount: float, reason: str) -> str:
    """Issue a refund for an order. Requires approval; state the amount explicitly."""
    with closing(_db()) as db:
        row = db.execute("SELECT * FROM orders WHERE order_id = ?", (order_id,)).fetchone()
        if not row:
            return f"error: no order {order_id!r} in the system"
        # The demo DB is immutable: "refunding" records the intent, not money movement.
        db.execute("UPDATE orders SET note = ? WHERE order_id = ?",
                   (f"refund {_money(amount)} USD: {reason}", order_id))
        db.commit()
    return (f"refund approved and recorded for {order_id}: {_money(amount)} USD ({reason}). "
            "Per policy ACC-001 it appears as a credit note on the next invoice.")


@tool(side_effecting=True)
def notify_customer(contact: str, message: str) -> str:
    """Email a customer about their order. Requires approval.

    `contact` is the address `lookup_order` returned. The pseudonymisation
    guardrail restores it from its placeholder before this runs, so the model
    never sees the address and the tool still gets the real one (ADR-0025).
    """
    if not contact or "@" not in contact:
        return f"error: {contact!r} is not an email address"
    with closing(_db()) as db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS notifications("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, contact TEXT NOT NULL,"
            "message TEXT NOT NULL, sent_at TEXT NOT NULL)")
        db.execute("INSERT INTO notifications(contact, message, sent_at) VALUES (?,?,?)",
                   (contact, message, _now()))
        db.commit()
    return f"email queued to {contact}: {message}"


_ = DEFAULT_REGISTRY  # imported for registration side-effects; see loop.assemble
