"""Approval policies for side-effecting tools (ADR-0007).

Enforcement is the loop's job; these are the ways to answer the question.
`AutoDeny` is the default so an unattended run can never quietly issue a refund.
`Deferred` does not answer at all: it ends the run as `pending_approval` so a
human can answer later, from another process (ADR-0029).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from rigging.types import ToolCall


class ApprovalPending(Exception):
    """A `deferred` policy has no answer for this call (ADR-0029).

    Not an error: the loop turns it into `approval_pending` and ends the run in
    the `pending_approval` status. The tool has not run and cannot have run — the
    raise happens before `_run_tool` on every path.
    """

    def __init__(self, call: ToolCall, tool_name: str, reason: str) -> None:
        self.call, self.tool_name, self.reason = call, tool_name, reason
        super().__init__(f"{tool_name} needs approval: {reason}")


class AutoDenyApproval:
    name = "auto_deny"

    def request(self, call: ToolCall, tool: Any, reason: str) -> tuple[bool, str]:
        return False, "denied: no approver attached to this run (unattended)"


class CLIPromptApproval:
    """Interactive y/N on the terminal. Never used under eval or replay."""

    name = "cli"

    def __init__(self, ask: Callable[[str], str] = input) -> None:
        self._ask = ask

    def request(self, call: ToolCall, tool: Any, reason: str) -> tuple[bool, str]:
        args = json.dumps(call.arguments, indent=2, default=str)
        answer = self._ask(
            f"\n⚠️  {tool.name} is consequential.\n{reason}\nargs: {args}\napprove? [y/N] "
        ).strip().lower()
        return answer in ("y", "yes"), f"operator answered {answer or 'no'}"


class ScriptedApproval:
    """Deterministic answers for tests, replay and eval."""

    name = "scripted"

    def __init__(self, approve: Callable[[ToolCall], bool] | bool = False) -> None:
        self._approve = approve

    def request(self, call: ToolCall, tool: Any, reason: str) -> tuple[bool, str]:
        verdict = self._approve(call) if callable(self._approve) else bool(self._approve)
        return verdict, f"scripted approval={verdict}"


class DeferredApproval:
    """Suspend the run instead of answering; a human answers later (ADR-0029).

    The point is that nothing is lost when the process ends: the loop records the
    call it was about to make, and `resume_run` continues from the log.
    """

    name = "deferred"

    def request(self, call: ToolCall, tool: Any, reason: str) -> tuple[bool, str]:
        raise ApprovalPending(call, getattr(tool, "name", call.name), reason)
