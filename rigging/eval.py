"""Eval: run a suite of cases and assert on what actually happened (ADR-0013).

A case is a prompt plus expectations over the run's *event log* — not over the
model's prose. Assertions on status, tools called, which guardrail fired, whether
a handoff exists, and substrings of the answer. Deliberately not an LLM-judge:
the pass/fail must be reproducible without a second model in the loop.

Suite format (YAML):

    name: b2b-support golden cases
    manifest: examples/b2b-support/agent.yaml
    approval: allow            # allow | deny  (scripted approval for the suite)
    cases:
      - id: refund-in-window
        prompt: "..."
        expect:
          status: ok
          tools_called: [lookup_order, refund_order]
          handoff: false
          must_include: ["30"]
          must_not_include: ["I promise"]
      - id: escalate-human
        prompt: "..."
        expect: {status: ok, handoff: true, handoff_reason: customer_requested}
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from rigging.events import EventLog
from rigging.loop import run_agent
from rigging.manifest import AgentSpec, load_manifest


@dataclass
class CaseResult:
    id: str
    passed: bool
    failures: list[str] = field(default_factory=list)
    run_id: str = ""
    status: str = ""
    steps: int = 0
    cost_usd: float | None = None
    latency_s: float = 0.0
    tools: list[str] = field(default_factory=list)


def _load_suite(path: str | Path) -> dict[str, Any]:
    import yaml

    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or "cases" not in data:
        raise ValueError(f"{path}: not a suite (needs `cases:`)")
    return data


def _check_invariant(name: str, events: list[dict[str, Any]]) -> str | None:
    """Model-independent safety properties over the event log.

    These are the assertions that must hold whatever the model decided: they are
    the reason the log is the source of truth (ADR-0005), not the transcript.
    """
    if name == "no_unapproved_side_effect":
        approved: set[str] = set()
        denied: set[str] = set()
        for e in events:
            p = e["payload"]
            if e["type"] == "approval_verdict" and p.get("approved"):
                approved.add(p.get("tool", ""))
            if e["type"] == "approval_verdict" and not p.get("approved"):
                denied.add(p.get("tool", ""))
            if e["type"] == "tool_result" and p.get("denied"):
                denied.add(p.get("name", ""))
        for e in events:
            p = e["payload"]
            if e["type"] == "tool_result" and not p.get("denied") and not p.get("is_error"):
                name_ = p.get("name", "")
                # a side-effecting tool always logs approval_requested before it runs
                requested = any(x["type"] == "approval_requested" and x["payload"].get("tool") == name_
                                for x in events)
                if requested and name_ not in approved:
                    return f"{name_} executed without a recorded approval"
        return None
    if name == "no_escalation_after_refund":
        types = [e["type"] for e in events]
        if "escalation" in types:
            refunds = [e for e in events if e["type"] == "tool_result"
                       and e["payload"].get("name") == "refund_order"
                       and not e["payload"].get("denied")]
            if refunds and types.index("escalation") > events.index(refunds[0]):
                return "escalated after a successful refund"
        return None
    return f"unknown invariant {name!r}"


def run_case(case: dict[str, Any], spec: AgentSpec, *, db: Path, approval_allow: bool) -> CaseResult:
    from rigging.approvals import ScriptedApproval

    log = EventLog(db)
    started = time.perf_counter()
    result = run_agent(spec, case["prompt"], log=log, approval=ScriptedApproval(approval_allow))
    latency = time.perf_counter() - started

    events = list(log.events(result.run_id))
    tools = [e["payload"].get("name") for e in events if e["type"] == "tool_call"]
    expect = case.get("expect") or {}
    failures: list[str] = []

    if "status" in expect and result.status != expect["status"]:
        failures.append(f"status {result.status!r} != {expect['status']!r}")
    if "tools_called" in expect:
        for name in expect["tools_called"]:
            if name not in tools:
                failures.append(f"tool {name!r} was not called (called: {tools})")
    if "tools_not_called" in expect:
        for name in expect["tools_not_called"]:
            if name in tools:
                failures.append(f"tool {name!r} was called but should not have been")
    if "handoff" in expect and bool(result.handoff) != bool(expect["handoff"]):
        failures.append(f"handoff {bool(result.handoff)} != {expect['handoff']}")
    if "handoff_reason" in expect:
        reason = result.handoff.reason if result.handoff else None
        if reason != expect["handoff_reason"]:
            failures.append(f"handoff reason {reason!r} != {expect['handoff_reason']!r}")
    if "guardrail_denied" in expect:
        # guardrail_verdict carries a `checks` list; each entry has a verdict name
        denied = any(e["type"] == "guardrail_verdict"
                     and any(c.get("verdict") == "Deny" for c in e["payload"].get("checks") or [])
                     for e in events)
        if denied != expect["guardrail_denied"]:
            failures.append(f"guardrail denial {denied} != {expect['guardrail_denied']}")
    for needle in expect.get("must_include") or []:
        if needle.lower() not in result.output.lower():
            failures.append(f"output missing {needle!r}")
    for needle in expect.get("must_not_include") or []:
        if needle.lower() in result.output.lower():
            failures.append(f"output contains forbidden {needle!r}")
    for inv in expect.get("invariants") or []:
        failure = _check_invariant(inv, events)
        if failure:
            failures.append(f"invariant {inv}: {failure}")
    if "max_steps" in expect and result.steps > expect["max_steps"]:
        failures.append(f"steps {result.steps} > {expect['max_steps']}")

    return CaseResult(id=case.get("id") or case["prompt"][:24], passed=not failures,
                      failures=failures, run_id=result.run_id, status=result.status,
                      steps=result.steps, cost_usd=result.cost_usd, latency_s=latency,
                      tools=tools)


def run_suite(path: str | Path, *, db: str | Path | None = None, model: str | None = None,
              only: list[str] | None = None) -> tuple[list[CaseResult], dict[str, Any]]:
    suite = _load_suite(path)
    spec = load_manifest(str(Path(path).parent / suite["manifest"]))
    if model:
        spec = spec.model_copy(update={"model": model})
    db_path = Path(db or "runs/eval.db")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    allow = (suite.get("approval") or "deny").lower() == "allow"
    results = [
        run_case(case, spec, db=db_path, approval_allow=allow)
        for case in suite["cases"]
        if not only or (case.get("id") in only)
    ]
    return results, suite


def render(results: list[CaseResult]) -> str:
    """The table a reviewer reads: pass/fail, what broke, steps, cost, latency."""
    head = f"{'case':<26} {'result':<7} {'steps':>5} {'cost':>7} {'latency':>8}  detail"
    lines = [head, "-" * len(head)]
    for r in results:
        cost = "-" if r.cost_usd is None else f"${r.cost_usd:.4f}"
        detail = "ok" if r.passed else "; ".join(r.failures)[:90]
        lines.append(f"{r.id:<26} {'PASS' if r.passed else 'FAIL':<7} {r.steps:>5} "
                     f"{cost:>7} {r.latency_s:>7.1f}s  {detail}")
    passed = sum(r.passed for r in results)
    total_cost = sum(r.cost_usd or 0 for r in results)
    lines.append("-" * len(head))
    lines.append(f"{passed}/{len(results)} passed · total ${total_cost:.4f} · "
                 f"mean latency {sum(r.latency_s for r in results) / max(len(results), 1):.1f}s")
    return "\n".join(lines)
