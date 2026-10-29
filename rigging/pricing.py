"""Cost accounting from a static price table and gateway usage (ADR-0011)."""

from __future__ import annotations

from pathlib import Path

import yaml

from rigging.types import Usage


class PriceTable:
    def __init__(self, prices: dict[str, dict[str, float]]) -> None:
        self._prices = prices

    @classmethod
    def load(cls, path: str | Path = "prices.yaml") -> PriceTable:
        p = Path(path)
        if not p.exists():
            bundled = Path(__file__).with_name("prices.yaml")
            p = bundled if bundled.exists() else p
        if not p.exists():
            return cls({})
        return cls(yaml.safe_load(p.read_text(encoding="utf-8")) or {})

    def knows(self, model: str) -> bool:
        return model in self._prices

    def cost(self, model: str, usage: Usage) -> float | None:
        """None means "unknown model": the caller logs `cost_unknown` and skips the limit."""
        price = self._prices.get(model)
        if price is None:
            return None
        return (usage.input_tokens * price.get("input", 0.0)
                + usage.output_tokens * price.get("output", 0.0)) / 1_000_000
