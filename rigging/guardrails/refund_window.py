"""`refund_window` — a policy guardrail found by the eval suite, not by imagination.

The first golden eval run showed the b2b agent refunding an order delivered 300
days earlier: the 30-day rule lived only in the prompt and in a policy document.
A rule you cannot enforce is a suggestion. This guardrail checks the tool call
against the fixture DB before `refund_order` runs, so the engine — not the
model's mood — owns the window.
"""

from __future__ import annotations

import datetime as _dt
import os
import sqlite3
from pathlib import Path
from typing import Any

from rigging.protocols import Allow, Deny, Verdict
from rigging.types import ToolCall


class RefundWindowGuardrail:
    name = "refund_window"

    def __init__(self, *, tool: str = "refund_order", max_days: int = 30,
                 db_path: str | None = None, order_arg: str = "order_id") -> None:
        self.tool, self.max_days, self.order_arg = tool, max_days, order_arg
        self.db_path = db_path or os.environ.get("RIGGING_DEMO_DB", "")
        self._db_path_default = Path(__file__).parents[2] / "examples/b2b-support/demo.db"

    def _delivered_at(self, order_id: str) -> str | None:
        path = Path(self.db_path) if self.db_path else self._db_path_default
        if not path.exists():
            return None
        db = sqlite3.connect(path)
        row = db.execute("SELECT delivered_at FROM orders WHERE order_id = ?", (order_id,)).fetchone()
        db.close()
        return row[0] if row else None

    def on_input(self, messages: Any) -> Verdict:
        return Allow()

    def on_tool_call(self, call: ToolCall, tool: object) -> Verdict:
        if call.name != self.tool:
            return Allow()
        order_id = str(call.arguments.get(self.order_arg, ""))
        delivered = self._delivered_at(order_id)
        if not delivered:
            return Deny(f"{order_id}: no delivery date on record — a refund needs a delivered "
                        "order; escalate for a manual review")
        try:
            when = _dt.date.fromisoformat(delivered)
        except ValueError:
            return Deny(f"{order_id}: unparseable delivery date {delivered!r} — escalate")
        age = (_dt.date.today() - when).days
        if age > self.max_days:
            return Deny(f"{order_id} was delivered {age} days ago, outside the {self.max_days}-day "
                        "refund window (REF-001); a written commercial exception is required — escalate")
        return Allow()

    def on_tool_result(self, call: ToolCall, content: str) -> Verdict:
        return Allow()

    def on_output(self, text: str, messages: Any) -> Verdict:
        return Allow()


from rigging.registry import DEFAULT_REGISTRY  # noqa: E402

DEFAULT_REGISTRY.add("guardrail", RefundWindowGuardrail.name, RefundWindowGuardrail, replace=True)
