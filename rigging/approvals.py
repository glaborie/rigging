"""Approval policies for side-effecting tools (ADR-0007).

Enforcement is the loop's job; these are the three ways to answer the question.
`AutoDeny` is the default so an unattended run can never quietly issue a refund.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from rigging.types import ToolCall


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
