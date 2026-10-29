"""The security boundary: nothing reaches the event log unredacted.

Tested property (tests/test_redact.py): a secret-shaped string in a tool result
never appears in the SQLite file. Keep the patterns conservative — over-redaction
in a transcript is cheap, under-redaction is a leak.
"""

from __future__ import annotations

import re
from typing import Any

REDACTED = "[REDACTED]"

PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("email", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]{2,}\b")),
    ("card", re.compile(r"\b(?:\d[ -]?){13,19}\b")),
    ("phone", re.compile(r"\b\+?\d{1,3}[ .-]?\(?\d{2,4}\)?[ .-]?\d{3}[ .-]?\d{2,4}\b")),
    ("api_key", re.compile(r"\b(?:sk|rk|pk)-[A-Za-z0-9]{16,}\b")),
    ("openrouter", re.compile(r"\bsk-or-v1-[A-Za-z0-9]{16,}\b")),
    ("bearer", re.compile(r"\bBearer\s+[A-Za-z0-9._-]{12,}\b", re.IGNORECASE)),
    ("aws", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
]


def redact_text(text: str) -> str:
    for _kind, pattern in PATTERNS:
        text = pattern.sub(REDACTED, text)
    return text


def redact_value(value: Any) -> Any:
    """Recursively redact strings inside a JSON-compatible payload."""
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, list):
        return [redact_value(v) for v in value]
    if isinstance(value, dict):
        return {k: redact_value(v) for k, v in value.items()}
    return value


def redact_event(payload: dict[str, Any]) -> dict[str, Any]:
    out = redact_value(payload)
    assert isinstance(out, dict)
    return out
