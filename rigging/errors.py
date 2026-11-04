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


# The loop returns a `RunResult` for denial, budget exhaustion and gateway
# failure; it does not raise. Only errors a caller must handle stay here.
