"""Core tools registered on the default registry at import.

Kept deliberately tiny and deterministic: they exist to exercise the whole
tool path (registry -> JSON Schema -> model call -> arguments -> result) with
zero side effects and zero network. Real capability packs live in third-party
packages or examples.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import zoneinfo

from rigging.registry import DEFAULT_REGISTRY, tool


@tool()
def current_time(tz: str = "UTC") -> str:
    """Return the current time in IANA timezone `tz` (default UTC), ISO 8601."""
    try:
        zone = zoneinfo.ZoneInfo(tz)
    except KeyError:
        return f"error: unknown timezone {tz!r} (examples: UTC, Europe/Zurich, America/New_York)"
    return _dt.datetime.now(zone).isoformat(timespec="seconds")


@tool()
def sha256_hex(text: str) -> str:
    """Return the SHA-256 hex digest of `text` (UTF-8)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


_ = DEFAULT_REGISTRY  # imported for its registration side-effects; see loop.assemble
