"""Exception hierarchy.

Rule: exceptions are for the *caller* (the loop or the CLI). Anything that happens
inside a tool, a guardrail or the transport is turned into an event and, where the
model can recover, into a tool message. See docs/architecture.md §6.
"""

from __future__ import annotations


class RiggingError(Exception):
    """Base class for every error raised by rigging."""


class ManifestError(RiggingError):
    """The manifest is invalid, or names a capability that is not registered."""


class CapabilityNotFound(ManifestError):
    def __init__(self, kind: str, names: list[str], available: list[str]) -> None:
        self.kind, self.names, self.available = kind, names, available
        super().__init__(
            f"unknown {kind}: {', '.join(names)} (registered: {', '.join(sorted(available)) or 'none'})"
        )


class TransportError(RiggingError):
    """A gateway call failed after retries were exhausted."""

    def __init__(self, status: int | None, body: str, attempts: int) -> None:
        self.status, self.body, self.attempts = status, body, attempts
        super().__init__(f"gateway error status={status} attempts={attempts}: {body[:300]}")


class ToolError(RiggingError):
    """A tool raised. The loop converts this into an `is_error` tool message."""


class GuardrailDenied(RiggingError):
    """An input or output guardrail denied. The run ends; a tool-call denial does not."""

    def __init__(self, guardrail: str, reason: str) -> None:
        self.guardrail, self.reason = guardrail, reason
        super().__init__(f"{guardrail} denied: {reason}")


class ApprovalDenied(RiggingError):
    """A side-effecting tool call was not approved. Becomes a tool message, not a crash."""


class BudgetExhausted(RiggingError):
    """Limits were hit and the model still would not conclude."""

    def __init__(self, reason: str, run_id: str) -> None:
        self.reason, self.run_id = reason, run_id
        super().__init__(f"budget exhausted ({reason}) in run {run_id}")
