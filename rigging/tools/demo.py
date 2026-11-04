"""Order-desk tools for the b2b-support demo (v0.3).

Registered on the default registry when a manifest asks for them; backed by the
seeded SQLite fixture DB (`examples/b2b-support/demo.db`, built by seed.py).
`refund_order` is side-effecting: the consequential-action guardrail and the
approval policy gate it before this function ever runs (ADR-0004, ADR-0007).
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import sqlite3
import uuid
from contextlib import closing
from pathlib import Path
from typing import Any

from rigging.registry import DEFAULT_REGISTRY, tool


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
CREATE TABLE IF NOT EXISTS baskets (
  basket_id  TEXT PRIMARY KEY,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS basket_items (
  basket_id TEXT NOT NULL,
  sku       TEXT NOT NULL,
  qty       INTEGER NOT NULL,
  added_at  TEXT NOT NULL,
  PRIMARY KEY (basket_id, sku)
);
"""


def _basket_db() -> sqlite3.Connection:
    """The demo DB plus the basket tables. Old seeded databases gain them here."""
    db = _db()
    db.executescript(BASKET_SCHEMA)
    db.commit()
    return db


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


def _view(db: sqlite3.Connection, basket_id: str) -> dict[str, Any]:
    rows = db.execute(
        "SELECT sku, qty FROM basket_items WHERE basket_id = ? ORDER BY sku", (basket_id,)
    ).fetchall()
    items = [_line(db, row["sku"], row["qty"]) for row in rows]
    return {
        "basket_id": basket_id, "items": items, "count": len(items),
        "subtotal": _money(sum(float(item.get("line_total", 0.0)) for item in items)),
    }


@tool()
def add_to_basket(sku: str, qty: int, basket_id: str = "") -> str:
    """Add qty of a SKU to a basket. Omit basket_id to start one; reuse the id it returns."""
    if qty <= 0:
        return f"error: qty must be positive (got {qty})"
    with closing(_basket_db()) as db:
        if db.execute("SELECT 1 FROM products WHERE sku = ?", (sku,)).fetchone() is None:
            return f"error: unknown SKU {sku!r}"
        basket_id = basket_id.strip() or f"b_{uuid.uuid4().hex[:8]}"
        db.execute("INSERT OR IGNORE INTO baskets(basket_id, created_at) VALUES (?,?)",
                   (basket_id, _now()))
        db.execute(
            "INSERT INTO basket_items(basket_id, sku, qty, added_at) VALUES (?,?,?,?)"
            " ON CONFLICT(basket_id, sku) DO UPDATE SET qty = qty + excluded.qty",
            (basket_id, sku, qty, _now()),
        )
        db.commit()
        total = db.execute("SELECT qty FROM basket_items WHERE basket_id = ? AND sku = ?",
                           (basket_id, sku)).fetchone()["qty"]
        return json.dumps({"added": _line(db, sku, total), **_view(db, basket_id)}, indent=1)


@tool()
def view_basket(basket_id: str) -> str:
    """Show a basket's lines with tier unit prices, MOQ/stock flags and the subtotal."""
    with closing(_basket_db()) as db:
        return json.dumps(_view(db, basket_id.strip()), indent=1)


@tool()
def update_basket_item(basket_id: str, sku: str, qty: int) -> str:
    """Set a basket line to qty; qty=0 removes the line. Use add_to_basket for new lines."""
    if qty < 0:
        return f"error: qty must be zero or positive (got {qty})"
    with closing(_basket_db()) as db:
        if qty == 0:
            removed = db.execute("DELETE FROM basket_items WHERE basket_id = ? AND sku = ?",
                                 (basket_id, sku)).rowcount
            db.commit()
            if not removed:
                return f"error: {sku!r} is not in basket {basket_id!r}"
            return json.dumps({"removed": sku, **_view(db, basket_id)}, indent=1)
        if db.execute("SELECT 1 FROM products WHERE sku = ?", (sku,)).fetchone() is None:
            return f"error: unknown SKU {sku!r}"
        updated = db.execute("UPDATE basket_items SET qty = ? WHERE basket_id = ? AND sku = ?",
                             (qty, basket_id, sku)).rowcount
        db.commit()
        if not updated:
            return (f"error: {sku!r} is not in basket {basket_id!r}; "
                    "use add_to_basket for a new line")
        return json.dumps({"updated": _line(db, sku, qty), **_view(db, basket_id)}, indent=1)


@tool()
def clear_basket(basket_id: str) -> str:
    """Remove every line from a basket. The basket id stays valid for a new build."""
    with closing(_basket_db()) as db:
        removed = db.execute("DELETE FROM basket_items WHERE basket_id = ?",
                             (basket_id.strip(),)).rowcount
        db.commit()
        return json.dumps({"removed": removed, **_view(db, basket_id.strip())}, indent=1)


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


_ = DEFAULT_REGISTRY  # imported for registration side-effects; see loop.assemble
