#!/usr/bin/env python3
"""scripts/bench.py: run `harness eval` over models x repeats and publish a results table.

    uv run python scripts/bench.py examples/b2b-support/eval.yaml \
        --models opencode-go/deepseek-v4.1-flash --repeats 5 \
        --cmd "uv run harness eval {suite} --model {model} --profile default"

Writes results/raw/<model>-<rep>.txt, results/summary.json and results/table.md.

Traces: every rep runs with `--export langfuse` (override with `--export NAME`,
disable with `--no-export`) and sets RIGGING_TRACE_TAGS, so the harness tags the
trace `bench,<model>,rep-<n>`. One eval case is one trace.

Parsing: prefers JSON on stdout (the `--json` envelope or a bare list of
{"case","passed","latency_s","cost_usd","steps"}), falls back to scraping
"<case> ... PASS|FAIL" lines from the eval table.
Case naming convention: safety-* cases are invariants (guardrail/approval must hold,
reported separately); everything else is task success.
"""
import argparse
import json
import math
import os
import re
import shlex
import statistics
import subprocess
import sys
import time
from pathlib import Path

DEFAULT_CMD = "uv run harness eval {suite} --model {model}"
DEFAULT_EXPORT = ["langfuse"]
LINE_RE = re.compile(r"^\s*(?P<case>[\w.\-]+)\b.*?\b(?P<verdict>PASS|FAIL)\b", re.M | re.I)


def build_cmd(template: str, suite: str, model: str, exports: list[str]) -> list[str]:
    """The eval command for one rep.

    `--export` is appended rather than baked into the default template so the
    exports stay visible in `--help` and switchable. A template that already
    passes `--export` is left alone: the caller meant that exporter.
    """
    cmd = template.format(suite=suite, model=model)
    if exports and "--export" not in cmd:
        cmd += "".join(f" --export {name}" for name in exports)
    return shlex.split(cmd)


def run_tags(model: str, rep: int) -> str:
    """`langfuse.trace.tags` for one rep: which benchmark, model, and pass.

    Tags are what make a benchmark filterable in the Langfuse UI — every case is
    its own trace, and `gen_ai.request.model` alone cannot tell rep 1 from rep 5.
    """
    return f"bench,{model},rep-{rep + 1}"


def langfuse_keys_present() -> bool:
    """The harness reads .env itself; this check only decides whether to warn."""
    from rigging.config import settings

    return bool((os.environ.get("LANGFUSE_PUBLIC_KEY") or settings.langfuse_public_key)
                and (os.environ.get("LANGFUSE_SECRET_KEY") or settings.langfuse_secret_key))


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
    except json.JSONDecodeError:
        data = None
    # `harness eval --json` prints {"suite": ..., "results": [...]}; a bare list
    # is still accepted so the parser does not depend on one envelope.
    if isinstance(data, dict):
        data = data.get("results")
    if isinstance(data, list):
        rows = []
        for r in data:
            case = (r.get("id") or r.get("case")) if isinstance(r, dict) else None
            if not case or "passed" not in r:
                return _scrape(stdout)
            rows.append({
                "case": case,
                "passed": bool(r["passed"]),
                "latency_s": r.get("latency_s"),
                "cost_usd": r.get("cost_usd"),
                "steps": r.get("steps"),
            })
        return rows
    return _scrape(stdout)


def _scrape(stdout: str) -> list[dict]:
    return [
        {"case": m["case"], "passed": m["verdict"].upper() == "PASS",
         "latency_s": None, "cost_usd": None, "steps": None}
        for m in LINE_RE.finditer(stdout)
    ]


def _txt(x) -> str:
    return x.decode(errors="replace") if isinstance(x, bytes) else (x or "")


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
    ap.add_argument("--safety-cases", nargs="*", default=[], help="case names that are invariants")
    ap.add_argument("--timeout", type=int, default=900)
    ap.add_argument("--out", default="results")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--export", action="append", metavar="NAME",
                   help="exporter for every eval run, repeatable (default: langfuse)")
    g.add_argument("--no-export", action="store_true",
                   help="no exporters: no traces, nothing to configure")
    a = ap.parse_args()

    exports = [] if a.no_export else (a.export or DEFAULT_EXPORT)
    if "langfuse" in exports and not langfuse_keys_present():
        print("warning: --export langfuse but LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY "
              "are unset; the harness will skip spans", file=sys.stderr)
    if exports:
        print(f"traces: {', '.join(exports)} · tags bench,<model>,rep-<n>", file=sys.stderr)

    out = Path(a.out)
    (out / "raw").mkdir(parents=True, exist_ok=True)
    summary: dict[str, dict] = {}
    per_case: dict[str, dict[str, list[bool]]] = {}

    for model in a.models:
        trials: list[dict] = []
        wall: list[float] = []
        crashes = 0
        for rep in range(a.repeats):
            cmd = build_cmd(a.cmd, a.suite, model, exports)
            env = {**os.environ, "RIGGING_TRACE_TAGS": run_tags(model, rep)}
            t0 = time.monotonic()
            try:
                p = subprocess.run(cmd, capture_output=True, text=True, timeout=a.timeout,
                                   env=env)
                stdout, stderr, rc = p.stdout, p.stderr, p.returncode
            except subprocess.TimeoutExpired as e:
                stdout, stderr, rc = _txt(e.stdout), _txt(e.stderr) + "\n[timeout]", -1
            wall.append(time.monotonic() - t0)
            safe = re.sub(r"[^\w.\-]", "_", model)
            (out / "raw" / f"{safe}-{rep}.txt").write_text(
                stdout + (f"\n--- stderr ---\n{stderr}" if stderr else ""))
            rows = parse(stdout)
            if not rows:  # a run that produced no parsable cases is a crash, not a pass
                crashes += 1
                tail = [ln for ln in (stderr or stdout).strip().splitlines() if ln.strip()][-3:]
                print("  no cases parsed; last output: " + " | ".join(tail), file=sys.stderr)
            trials.extend(rows)
            print(f"{model} rep {rep + 1}/{a.repeats}: {len(rows)} cases, rc={rc}", file=sys.stderr)

        def rate(rows):
            k = sum(r["passed"] for r in rows)
            lo, hi = wilson(k, len(rows))
            return {"passed": k, "n": len(rows), "rate": k / len(rows) if rows else None,
                    "ci95": [lo, hi]}

        def is_safety(c: str) -> bool:
            return c.startswith(a.safety_prefix) or c in a.safety_cases

        safety = [r for r in trials if is_safety(r["case"])]
        task = [r for r in trials if not is_safety(r["case"])]
        per_case[model] = {}
        for r in trials:
            per_case[model].setdefault(r["case"], []).append(r["passed"])
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
    cases = sorted({c for m in per_case.values() for c in m})
    lines += ["", "Per case (passes/repeats):", "",
              "| Case | " + " | ".join(f"`{m}`" for m in per_case) + " |",
              "|---|" + "---|" * len(per_case)]
    for c in cases:
        cells = []
        for m in per_case:
            r = per_case[m].get(c)
            cells.append(f"{sum(r)}/{len(r)}" if r else "n/a")
        lines.append(f"| {c}{' (invariant)' if is_safety(c) else ''} | " + " | ".join(cells) + " |")
    (out / "table.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())