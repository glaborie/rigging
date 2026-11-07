"""The write path: run an agent from the browser (ADR-0023).

The loop stays the only thing that runs an agent. This module starts `run_agent`
in a worker thread, names the run before the thread does so the page can subscribe
to its stream, and blocks a consequential tool on an answer from the page.

Nothing here decides an approval: the loop's `ApprovalPolicy` is still the gate,
`WebApproval` only carries the page's answer back to it, and the loop writes
`approval_requested` / `approval_verdict` exactly as it does for the CLI.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from rigging.events import EventLog
from rigging.loop import run_agent
from rigging.manifest import AgentSpec
from rigging.types import RunResult, ToolCall

#: How long a consequential tool waits for the page before it is denied.
APPROVAL_TIMEOUT_S = 600.0


@dataclass
class _Pending:
    """One question waiting for the browser."""

    tool: str
    reason: str
    arguments: dict[str, Any]
    answered: threading.Event = field(default_factory=threading.Event)
    approved: bool = False
    note: str = ""


class WebApproval:
    """An `ApprovalPolicy` that waits for the page (ADR-0023).

    `run_agent` appends `approval_requested` *before* calling `request()`, so the
    page learns about the prompt from the event stream and answers with a POST.
    The loop then writes `approval_verdict` itself; this object never touches the
    log.
    """

    name = "web"

    def __init__(self, run_id: str, pending: dict[str, _Pending], lock: threading.Lock,
                 timeout: float = APPROVAL_TIMEOUT_S) -> None:
        self._run_id = run_id
        self._pending = pending
        self._lock = lock
        self._timeout = timeout

    def request(self, call: ToolCall, tool: Any, reason: str) -> tuple[bool, str]:
        slot = _Pending(tool=call.name, reason=reason, arguments=dict(call.arguments))
        with self._lock:
            self._pending[self._run_id] = slot
        try:
            if not slot.answered.wait(self._timeout):
                return False, f"no answer from the page in {int(self._timeout)}s"
            return slot.approved, slot.note
        finally:
            with self._lock:
                self._pending.pop(self._run_id, None)


class ChatRunner:
    """Starts one `run_agent` per turn, and answers approvals from the page."""

    def __init__(self, spec: AgentSpec, db: str, *,
                 client_factory: Callable[[], Any] | None = None,
                 registry: Any = None,
                 approval_timeout: float = APPROVAL_TIMEOUT_S) -> None:
        self.spec = spec
        self.db = db
        self._client_factory = client_factory or (lambda: None)
        self._registry = registry
        self._timeout = approval_timeout
        # The page is an interactive channel, like the terminal prompt it
        # replaces. A manifest that says `auto_deny` or `scripted` made a
        # deliberate unattended choice, and a server flag must not widen it.
        self.approvals = "page" if spec.approval.policy == "cli" else spec.approval.policy
        self._pending: dict[str, _Pending] = {}
        self._lock = threading.Lock()

    def start(self, prompt: str, session_id: str | None = None) -> tuple[str, str]:
        """Open the session and the run, then start the turn. Returns ids."""
        log = EventLog(self.db)
        try:
            sid = log.start_session(session_id, title=self.spec.name,
                                    spec_hash=self.spec.spec_hash)
            run_id = log.start_run(session_id=sid, spec_hash=self.spec.spec_hash,
                                   model=self.spec.model)
        finally:
            log.close()
        threading.Thread(
            target=self._turn, args=(prompt, sid, run_id), name=f"rigging-{run_id}",
            daemon=True,
        ).start()
        return sid, run_id

    def resolve(self, run_id: str, approve: bool, note: str = "") -> bool:
        """Answer the pending approval for a run. False when none is waiting."""
        with self._lock:
            slot = self._pending.get(run_id)
            if slot is None:
                return False
            slot.approved = approve
            slot.note = note or ("approved in the web view" if approve
                                 else "denied in the web view")
            slot.answered.set()
            return True

    def answer_deferred(self, approval_id: str, *, approved: bool, approver: str,
                        note: str = "") -> RunResult:
        """Resume a run parked by a `deferred` policy (ADR-0029).

        Not the page's channel: that one answers a live thread. This is for the
        client that was not there when the run stopped, so the run continues from
        the log in this request — one run, one `resume_run`.
        """
        from rigging.loop import resume_run

        log = EventLog(self.db)
        try:
            match = next((row for row in log.approvals(limit=200)
                          if row.get("approval_id") == approval_id), None)
            if match is None:
                raise LookupError(approval_id)
            return resume_run(self.spec, str(match["run_id"]), approved=approved,
                              approver=approver, note=note, log=log,
                              client=self._client_factory(), registry=self._registry)
        finally:
            log.close()

    # ------------------------------------------------------------------ thread

    def _turn(self, prompt: str, session_id: str, run_id: str) -> None:
        log = EventLog(self.db)
        try:
            # Rebuilt from the log, not kept in the process: a restart loses no
            # conversation, and a second tab sees the same history (ADR-0023).
            history = log.session_messages(session_id)
            approval = (WebApproval(run_id, self._pending, self._lock, self._timeout)
                        if self.approvals == "page" else None)
            run_agent(
                self.spec, prompt, log=log, session_id=session_id, run_id=run_id,
                history=history, client=self._client_factory(),
                registry=self._registry,
                approval=approval,
            )
        except Exception as exc:  # a dead thread must not leave a run "running"
            log.append(run_id, "error", {"error": f"{type(exc).__name__}: {exc}"})
            log.finish_run(run_id, "error")
        finally:
            log.close()
