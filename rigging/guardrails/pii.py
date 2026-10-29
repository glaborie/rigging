"""PII redaction on input and output. Deterministic, transform-only, never blocks."""

from __future__ import annotations

from collections.abc import Sequence

from rigging.protocols import Allow, Transform, Verdict
from rigging.redact import redact_text
from rigging.types import Message, ToolCall


class PIIRedactionGuardrail:
    name = "pii_redaction"
    hooks = ("on_input", "on_output")

    def on_input(self, messages: Sequence[Message]) -> Verdict:
        joined = "\n".join(m.content for m in messages)
        cleaned = redact_text(joined)
        return Transform(cleaned, "pii redacted") if cleaned != joined else Allow()

    def on_tool_call(self, call: ToolCall, tool: object) -> Verdict:
        return Allow()

    def on_tool_result(self, call: ToolCall, content: str) -> Verdict:
        return Allow()

    def on_output(self, text: str, messages: Sequence[Message]) -> Verdict:
        cleaned = redact_text(text)
        return Transform(cleaned, "pii redacted") if cleaned != text else Allow()
