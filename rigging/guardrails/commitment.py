"""Out-of-policy commitments: the hard gate.

Deterministic and deliberately blunt: a commitment verb (refund/waive/credit/
discount/guarantee) in the output requires the transcript to contain a policy
passage that permits it. Laya's calibrated probability (ADR-0009) is the advisory
signal that catches paraphrase; this is the floor that always holds.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from rigging.protocols import Allow, Deny, Verdict
from rigging.types import Message, ToolCall

COMMITMENT = re.compile(
    r"\b(i('| a)?ll|we('| a)?ll|you will (get|receive)|i (can|will) (refund|waive|credit|approve)"
    r"|(refund|waive|credit|discount|compensat\w+) (is|has been) (approved|issued|processed))\b",
    re.IGNORECASE,
)
PERMISSION = re.compile(r"\b(policy|entitled|within|eligible|permitted|allowed)\b", re.IGNORECASE)


class CommitmentGuardrail:
    name = "commitment_check"
    hooks = ("on_output",)

    def on_input(self, messages: Sequence[Message]) -> Verdict:
        return Allow()

    def on_tool_call(self, call: ToolCall, tool: object) -> Verdict:
        return Allow()

    def on_tool_result(self, call: ToolCall, content: str) -> Verdict:
        return Allow()

    def on_output(self, text: str, messages: Sequence[Message]) -> Verdict:
        match = COMMITMENT.search(text)
        if not match:
            return Allow()
        corpus = "\n".join(m.content for m in messages if m.role in ("tool", "system"))
        if PERMISSION.search(corpus):
            return Allow()
        return Deny(
            f"output commits to {match.group(0)!r} without a supporting policy passage; "
            "ask the customer for details, or escalate to a human."
        )
