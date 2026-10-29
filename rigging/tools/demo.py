"""Order-desk tools for the b2b-support demo (v0.3).

Registered on the default registry when a manifest asks for them; backed by the
seeded SQLite fixture DB (`examples/b2b-support/demo.db`, built by seed.py).
`refund_order` is side-effecting: the consequential-action guardrail and the
approval policy gate it before this function ever runs (ADR-0004, ADR-0012).
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

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


@tool()
def lookup_order(order_id: str) -> str:
    """Look up a customer order by id (e.g. ORD-1001): items, status, delivery."""
    db = _db()
    rows = db.execute("SELECT * FROM orders WHERE order_id = ?", (order_id,)).fetchall()
    if not rows:
        return f"error: no order {order_id!r} in the system"
    return json.dumps([dict(r) for r in rows], indent=1)


@tool()
def check_pricing(sku: str, qty: int) -> str:
    """Price a quantity of a SKU: unit price at the volume tier, MOQ, lead time, stock."""
    db = _db()
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
    db = _db()
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
