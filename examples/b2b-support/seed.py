"""Seed the demo fixture DB.

Status: v0.3. Target shape: ~200 products (SKU, MOQ, tier prices, lead time,
stock), ~30 orders, ~8 policy documents — deterministic via a fixed seed so every
run of the demo looks the same and the eval suite can assert on real numbers.

    uv run python examples/b2b-support/seed.py --out examples/b2b-support/demo.db

Not implemented yet: this is the contract v0.3 fills in.
"""

from __future__ import annotations

import argparse


def main() -> int:
    parser = argparse.ArgumentParser(description="seed the b2b-support demo database")
    parser.add_argument("--out", default="examples/b2b-support/demo.db")
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args()
    raise SystemExit(
        f"seed.py lands in v0.3 (would write {args.out} with seed={args.seed}). "
        "See docs/milestones.md."
    )


if __name__ == "__main__":
    raise SystemExit(main())
