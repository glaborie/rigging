#!/usr/bin/env python3
"""scripts/bench.py: run `harness eval` over models x repeats and publish a results table.

    uv run python scripts/bench.py examples/b2b-support/suite.yaml \
        --models qwen3:8b other-local-model hosted-model --repeats 5

Writes results/raw/<model>-<rep>.txt, results/summary.json and results/table.md.

Parsing: prefers JSON on stdout (a list of {"case","passed","latency_s","cost_usd","steps"}),
falls back to scraping "<case> ... PASS|FAIL" lines from the eval table.
Case naming convention: safety-* cases are invariants (guardrail/approval must hold,
reported separately); everything else is task success.
"""
import argparse
import json
import math
import re
import shlex
import statistics
import subprocess
import sys
import time
from pathlib import Path

DEFAULT_CMD = "uv run harness eval {suite} --model {model}"
LINE_RE = re.compile(r"^\s*(?P<case>[\w.\-]+)\b.*?\b(?P<verdict>PASS|FAIL)\b", re.M | re.I)


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return 0.0, 0.0
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return max(0.0, c - h), min(1.0, c + h)


def parse(stdout: str) -> list[dict]:
    try:
        data = json.loads(stdout)
        if isinstance(data, list):
            return [
                {
                    "case": r["case"],
                    "passed": bool(r["passed"]),
                    "latency_s": r.get("latency_s"),
                    "cost_usd": r.get("cost_usd"),
                    "steps": r.get("steps"),
                }
                for r in data
            ]
    except (json.JSONDecodeError, KeyError, TypeError):
        pass
    return [
        {"case": m["case"], "passed": m["verdict"].upper() == "PASS",
         "latency_s": None, "cost_usd": None, "steps": None}
        for m in LINE_RE.finditer(stdout)
    ]


def median(xs: list) -> float | None:
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else None


def fmt(v, spec: str = ".1f") -> str:
    return "n/a" if v is None else format(v, spec)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("suite")
    ap.add_argument("--models", nargs="+", required=True)
    ap.add_argument("--repeats", type=int, default=5)
    ap.add_argument("--cmd", default=DEFAULT_CMD, help="template with {suite} and {model}")
    ap.add_argument("--safety-prefix", default="safety-")
    ap.add_argument("--timeout", type=int, default=900)
    ap.add_argument("--out", default="results")
    a = ap.parse_args()

    out = Path(a.out)
    (out / "raw").mkdir(parents=True, exist_ok=True)
    summary: dict[str, dict] = {}

    for model in a.models:
        trials: list[dict] = []
        wall: list[float] = []
        crashes = 0
        for rep in range(a.repeats):
            cmd = shlex.split(a.cmd.format(suite=a.suite, model=model))
            t0 = time.monotonic()
            try:
                p = subprocess.run(cmd, capture_output=True, text=True, timeout=a.timeout)
                stdout, rc = p.stdout, p.returncode
            except subprocess.TimeoutExpired as e:
                stdout, rc = (e.stdout or b"").decode() if isinstance(e.stdout, bytes) else (e.stdout or ""), -1
            wall.append(time.monotonic() - t0)
            safe = re.sub(r"[^\w.\-]", "_", model)
            (out / "raw" / f"{safe}-{rep}.txt").write_text(stdout)
            rows = parse(stdout)
            if not rows:  # a run that produced no parsable cases is a crash, not a pass
                crashes += 1
            trials.extend(rows)
            print(f"{model} rep {rep + 1}/{a.repeats}: {len(rows)} cases, rc={rc}", file=sys.stderr)

        def rate(rows):
            k = sum(r["passed"] for r in rows)
            lo, hi = wilson(k, len(rows))
            return {"passed": k, "n": len(rows), "rate": k / len(rows) if rows else None,
                    "ci95": [lo, hi]}

        safety = [r for r in trials if r["case"].startswith(a.safety_prefix)]
        task = [r for r in trials if not r["case"].startswith(a.safety_prefix)]
        flaky = sorted({
            c for c in {r["case"] for r in trials}
            if 0 < sum(r["passed"] for r in trials if r["case"] == c) < sum(r["case"] == c for r in trials)
        })
        summary[model] = {
            "safety": rate(safety), "task": rate(task), "crashed_runs": crashes,
            "latency_s_median": median([r["latency_s"] for r in trials]),
            "cost_usd_median": median([r["cost_usd"] for r in trials]),
            "steps_median": median([r["steps"] for r in trials]),
            "suite_wall_s_median": median(wall), "flaky_cases": flaky,
        }

    (out / "summary.json").write_text(json.dumps(summary, indent=2))

    lines = [
        f"Suite `{a.suite}`, {a.repeats} repeats per model. 95% Wilson intervals.\n",
        "| Model | Safety invariants | Task success | Crashed runs | Median latency (s) | Median steps | Median cost (USD) | Flaky cases |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for m, s in summary.items():
        def cell(r):
            if not r["n"]:
                return "n/a"
            return f"{r['passed']}/{r['n']} ({r['rate']:.0%}, {r['ci95'][0]:.0%}-{r['ci95'][1]:.0%})"
        lines.append(
            f"| `{m}` | {cell(s['safety'])} | {cell(s['task'])} | {s['crashed_runs']} | "
            f"{fmt(s['latency_s_median'])} | {fmt(s['steps_median'], '.0f')} | "
            f"{fmt(s['cost_usd_median'], '.4f')} | {', '.join(s['flaky_cases']) or '-'} |"
        )
    (out / "table.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())