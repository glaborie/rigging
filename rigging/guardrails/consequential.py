"""Consequential-action gate: side-effecting tools never run on the model's word.

Enforcement lives in the loop (it calls the ApprovalPolicy); this guardrail is the
policy-level deny that runs first, so a manifest can forbid a class of actions
outright — e.g. refunds above a threshold — before anyone is asked.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from rigging.protocols import Allow, Deny, Verdict
from rigging.types import Message, ToolCall


class ConsequentialActionGuardrail:
    name = "consequential_action"
    hooks = ("on_tool_call",)

    def __init__(self, max_amount_usd: float = 500.0, amount_arg: str = "amount") -> None:
        self.max_amount_usd = max_amount_usd
        self.amount_arg = amount_arg

    def on_input(self, messages: Sequence[Message]) -> Verdict:
        return Allow()

    def on_tool_call(self, call: ToolCall, tool: Any) -> Verdict:
        if not getattr(tool, "side_effecting", False):
            return Allow()
        amount: Any = call.arguments.get(self.amount_arg)
        if isinstance(amount, (int, float)) and amount > self.max_amount_usd:
            return Deny(
                f"{call.name} for {amount} exceeds the autonomous limit "
                f"({self.max_amount_usd}); escalate to a human instead."
            )
        return Allow()

    def on_tool_result(self, call: ToolCall, content: str) -> Verdict:
        return Allow()

    def on_output(self, text: str, messages: Sequence[Message]) -> Verdict:
        return Allow()
