"""Laya adapter — advisory only.

Status: v0.3, behind the `[laya]` extra.

Why advisory: Reflection's own model card says Laya is "a fast base to specialise,
not a zero-shot decision engine" (0.362 vs 0.766 accuracy on their typed-decisions
benchmark). Its authority has to be earned by measuring it on
`tests/data/decisions.jsonl` (~40 labelled cases from this domain) through
`harness eval`. Until that table exists, the deterministic guardrails are the gate.
"""

from __future__ import annotations

from collections.abc import Sequence

from rigging.errors import RiggingError
from rigging.protocols import DecisionAnswer

NOT_IMPLEMENTED = "LayaDecisionModel lands in v0.3 behind the [laya] extra."


class LayaDecisionModel:
    name = "laya"

    def __init__(self, checkpoint: str = "convaiinnovations/laya-typed-decisions",
                 threshold: float = 0.7, device: str = "cpu") -> None:
        self.checkpoint, self.threshold, self.device = checkpoint, threshold, device

    def decide(self, state: str, questions: dict[str, Sequence[str]]) -> dict[str, DecisionAnswer]:
        raise RiggingError(NOT_IMPLEMENTED)
