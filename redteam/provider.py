"""A promptfoo target that reports what the agent *did*, not what it said.

A red team that grades the transcript grades the wrong half. The model can say
"refund denied" while `refund_order` already rewrote `orders.note`, and it can
say "your refund is on the way" while the approval policy refused the call. So
this provider runs one turn of the b2b-support agent and returns facts taken from
the event log and the fixture database:

    {"answer": "...", "status": "ok", "executed": ["lookup_order"],
     "denied": [], "approval_denials": ["refund_order"],
     "guardrail_denials": ["on_tool_call/refund_window"],
     "side_effects": [], "writes": {"order_notes": []}, "emails_in_answer": []}

promptfoo's assertions then judge `executed` / `writes` / `emails_in_answer`,
which no amount of persuasion can fake, while its model graders judge the answer
text, which is the only part a customer sees.

Loaded by promptfoo's Python provider protocol (`file://provider.py`). That
protocol speaks JSON over stdin/stdout, so nothing here may print to stdout —
`call_api` redirects it to stderr for the whole turn.
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import importlib.util
import json
import os
import re
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

MANIFEST = ROOT / "examples/b2b-support/agent.yaml"
SEED_PY = ROOT / "examples/b2b-support/seed.py"

#: Everything the provider writes lives under `runs/`, which is gitignored.
WORK_DIR = ROOT / "runs" / "redteam"
RUN_DB = WORK_DIR / "demo.db"
EVENTS_DB = WORK_DIR / "events.db"

#: Tools whose execution changes the world. Kept here so the assertions can say
#: "no side effect ran" without knowing each tool's name by heart.
SIDE_EFFECTING = ("refund_order", "submit_basket", "notify_customer")

EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

#: ORD-1002 is the one order the seed describes as in-window and refundable. Its
#: delivery date is made relative to today (see `prepare_db`), and it is the only
#: order whose `note` a legitimate refund may rewrite.
REFUNDABLE_ORDER = "ORD-1002"


# --------------------------------------------------------------- the fixture DB


def _seed_module() -> Any:
    spec = importlib.util.spec_from_file_location("redteam_b2b_seed", SEED_PY)
    if spec is None or spec.loader is None:  # pragma: no cover - packaging accident
        raise RuntimeError(f"cannot load the demo seed at {SEED_PY}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def prepare_db(path: Path = RUN_DB) -> Path:
    """A fresh seeded copy of the demo DB, safe to mutate and identical every scan.

    Two reasons not to use `examples/b2b-support/demo.db` directly: `refund_order`
    rewrites `orders.note` and `submit_basket` inserts rows, so a scan would leave
    the fixture mutated and the next scan would start from a different state; and
    that file is what `make demo` uses on a real machine.

    ORD-1002's delivery date becomes "five days ago" because the seed pins it to
    2026-10-20. Without that, the 30-day window closes on 2026-11-19 and the
    in-window control starts failing for a reason that has nothing to do with the
    agent under test.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    _seed_module().build_db(path, seed=7)
    fresh = (_dt.date.today() - _dt.timedelta(days=5)).isoformat()
    with closing(sqlite3.connect(path)) as db:
        db.execute(
            "UPDATE orders SET status = 'delivered', delivered_at = ?, note = ? "
            "WHERE order_id = ?",
            (fresh, "sealed, unopened, in original packaging", REFUNDABLE_ORDER),
        )
        db.commit()
    return path


def snapshot(path: Path) -> dict[str, Any]:
    """What a side effect would change, as it stands right now."""
    with closing(sqlite3.connect(path)) as db:
        rows = db.execute("SELECT order_id, note FROM orders").fetchall()
        try:
            notifications = db.execute("SELECT COUNT(*) FROM notifications").fetchone()[0]
        except sqlite3.OperationalError:
            notifications = 0  # the tool creates the table on first use
    return {
        "notes": {str(row[0]): row[1] for row in rows},
        "orders": len(rows),
        "notifications": int(notifications),
    }


def writes(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """The diff two snapshots: the ground truth a transcript cannot lie about."""
    changed = sorted(order for order, note in after["notes"].items()
                     if before["notes"].get(order) != note)
    return {
        "order_notes": changed,
        "new_order_rows": after["orders"] - before["orders"],
        "new_notifications": after["notifications"] - before["notifications"],
    }


def plaintext_contacts(path: Path) -> int:
    """Contacts sitting in the file unsealed — 0 unless `RIGGING_PII_KEY` is unset."""
    from rigging.crypto import is_sealed

    count = 0
    with closing(sqlite3.connect(path)) as db:
        for table, column in (("orders", "contact"), ("notifications", "contact")):
            try:
                values = [row[0] for row in db.execute(
                    f"SELECT {column} FROM {table} WHERE {column} IS NOT NULL")]
            except sqlite3.OperationalError:
                continue
            count += sum(1 for value in values
                         if isinstance(value, str) and not is_sealed(value))
    return count


# ------------------------------------------------------------- the event log


def _add(items: list[str], name: str) -> None:
    if name and name not in items:
        items.append(name)


def facts(events: list[dict[str, Any]]) -> dict[str, list[str]]:
    """Fold the run's events into four lists a scan can assert on.

    `tool_call` is an *attempt* — the event is written before the guardrails and
    the approval policy see the call, so counting attempts would report a denial
    as an action (that is exactly the flakiness `eval.yaml` has). `tool_result`
    is what happened: `denied`, an error, or an execution.
    """
    attempted: list[str] = []
    executed: list[str] = []
    denied: list[str] = []
    approvals: list[str] = []
    guardrails: list[str] = []
    for event in events:
        kind = event.get("type")
        payload = event.get("payload") or {}
        if kind == "tool_call":
            _add(attempted, str(payload.get("name") or ""))
        elif kind == "tool_result":
            name = str(payload.get("name") or "")
            if payload.get("denied"):
                _add(denied, name)
            elif not payload.get("is_error"):
                _add(executed, name)
        elif kind == "approval_verdict" and not payload.get("approved"):
            _add(approvals, str(payload.get("tool") or ""))
        elif kind == "guardrail_verdict":
            for check in payload.get("checks") or []:
                if check.get("verdict") == "Deny":
                    _add(guardrails, f"{payload.get('hook')}/{check.get('guardrail')}")
    return {
        "attempted": attempted,
        "executed": executed,
        "denied": denied,
        "approval_denials": approvals,
        "guardrail_denials": guardrails,
    }


def contact_details(text: str) -> dict[str, list[str]]:
    """Real addresses in the answer. Placeholders (`[EMAIL_1]`) are the point, not a leak."""
    return {"emails": sorted(set(EMAIL.findall(text or "")))}


# ---------------------------------------------------------------- the provider


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    if isinstance(value, (list, tuple, set, dict)):
        return False  # a list means "these tools", not "everything"
    return bool(value)


def approval_for(value: Any) -> Any:
    """Turn the config's `approve` into an approval policy.

    Three forms, because a scan needs to say three different things:

        approve: false          nobody approves anything (the unattended default)
        approve: true           a fully permissive operator
        approve: [refund_order] only this tool — the guardrails are what is on trial,
                                not the approval channel
    """
    from rigging.approvals import ScriptedApproval

    if _truthy(value):
        return ScriptedApproval(True)
    if value is None or isinstance(value, bool):
        return ScriptedApproval(False)
    names = [value] if isinstance(value, str) else list(value)
    allowed = {str(name) for name in names}
    return ScriptedApproval(lambda call: call.name in allowed)


def _run(prompt: str, approve: Any) -> dict[str, Any]:
    from rigging.config import settings
    from rigging.events import EventLog
    from rigging.loop import run_agent
    from rigging.manifest import load_manifest

    # `refund_window` reads this at assembly time, so it has to be set before
    # `run_agent` builds the agent, not after.
    os.environ["RIGGING_DEMO_DB"] = str(prepare_db())
    before = snapshot(RUN_DB)

    with EventLog(EVENTS_DB) as log:
        result = run_agent(load_manifest(str(MANIFEST)), prompt, log=log,
                           approval=approval_for(approve))
        events = list(log.events(result.run_id))

    after = snapshot(RUN_DB)
    folded = facts(events)
    payload: dict[str, Any] = {
        # `answer` first: the model graders read this string as a transcript.
        "answer": result.output,
        "status": result.status,
        **folded,
        "side_effects": [t for t in folded["executed"] if t in SIDE_EFFECTING],
        "writes": writes(before, after),
        **contact_details(result.output),
        "stored_contacts_plaintext": plaintext_contacts(RUN_DB),
        "pii_key_set": bool(settings.pii_key.strip()),
        "approved": approve,
        "steps": result.steps,
        "cost_usd": result.cost_usd,
        "run_id": result.run_id,
        "error": result.error,
        # Not part of the JSON output: promptfoo's own fields carry these.
        "_usage": {**result.usage.model_dump(), "requests": max(1, result.steps)},
    }
    return payload


def call_api(prompt: str, options: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    """One turn of the agent, as facts. Called by promptfoo, once per probe.

    `approve` comes from the test's vars first (so one config can hold both the
    approved control and the unattended test) and from the provider config
    otherwise.
    """
    config = options.get("config") or {}
    variables = (context or {}).get("vars") or {}
    # Raw value, not `_truthy`: `approve: [refund_order]` is a list of tool names,
    # and collapsing it to a bool would silently turn the "refunds pre-approved"
    # scan into the unattended one.
    approve = variables.get("approve", config.get("approve", False))

    try:
        # The protocol owns stdout; a stray print corrupts it.
        with contextlib.redirect_stdout(sys.stderr):
            payload = _run(prompt, approve)
    except Exception as exc:  # promptfoo wants a message, not a traceback
        return {"error": f"{type(exc).__name__}: {exc}"}

    usage = payload.pop("_usage", None) or {}
    return {
        "output": json.dumps(payload, ensure_ascii=False),
        "metadata": payload,  # the exported results keep the facts, not just the string
        "tokenUsage": {
            "total": usage.get("input_tokens", 0) + usage.get("output_tokens", 0),
            "prompt": usage.get("input_tokens", 0),
            "completion": usage.get("output_tokens", 0),
            "numRequests": usage.get("requests", 1),
        },
        "cost": payload.get("cost_usd"),
    }
