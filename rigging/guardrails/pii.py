"""PII handling on the model path: one-way redaction, or reversible sentinels.

`PIIRedactionGuardrail` destroys the value (`[REDACTED]`). `PIIPseudonymiseGuardrail`
replaces it with a stable placeholder (`[EMAIL_1]`) and puts the real value back
into tool arguments, so the model can act on a customer without ever seeing them
(ADR-0025). Both are deterministic, transform-only and never block.

The sentinel map is per session and never written to the log: the log stays a
dead end. With `RIGGING_PII_KEY` set it is sealed into `SentinelVault` so a
restart does not strand the next turn (ADR-0027); without a key it is memory-only.
"""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from rigging.protocols import Allow, Deny, Transform, Verdict
from rigging.redact import PATTERNS, redact_text
from rigging.types import Message, ToolCall

#: `[EMAIL_1]`, not `<EMAIL_1>`: the web view renders markdown, where a
#: `<...>` token is swallowed as an unknown HTML tag.
SENTINEL = re.compile(r"\[([A-Z][A-Z0-9_]*)_(\d+)\]")


def _walk_strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from _walk_strings(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from _walk_strings(item)


@dataclass
class SentinelMap:
    """Bidirectional value <-> sentinel for one session. In memory, optionally vaulted.

    Stable within a run: the same value always maps to the same sentinel, so a
    tool result that repeats a customer's address does not mint `[EMAIL_2]`.
    """

    _to_sentinel: dict[str, str] = field(default_factory=dict)
    _to_value: dict[str, str] = field(default_factory=dict)
    _next: dict[str, int] = field(default_factory=dict)
    #: Called after a new sentinel is minted; `SentinelStore` uses it to vault the map.
    on_change: Any = field(default=None, repr=False, compare=False)

    def sentinelise(self, text: str) -> str:
        """Replace every detected PII span with its stable sentinel."""
        for kind, pattern in PATTERNS:
            text = self._replace(text, pattern, kind)
        return text

    def _replace(self, text: str, pattern: re.Pattern[str], label: str) -> str:
        def replace(match: re.Match[str]) -> str:
            return self.for_value(match.group(0), label)
        return pattern.sub(replace, text)

    def for_value(self, value: str, label: str) -> str:
        existing = self._to_sentinel.get(value)
        if existing is not None:
            return existing
        tag = label.upper()
        number = self._next.get(tag, 0) + 1
        self._next[tag] = number
        sentinel = f"[{tag}_{number}]"
        self._to_sentinel[value] = sentinel
        self._to_value[sentinel] = value
        if self.on_change is not None:
            self.on_change()
        return sentinel

    def to_dict(self) -> dict[str, Any]:
        """The parts worth persisting: the pairs and the per-kind counters."""
        return {"to_sentinel": dict(self._to_sentinel), "to_value": dict(self._to_value),
                "next": dict(self._next)}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SentinelMap:
        return cls(_to_sentinel=dict(data.get("to_sentinel") or {}),
                   _to_value=dict(data.get("to_value") or {}),
                   _next={k: int(v) for k, v in (data.get("next") or {}).items()})

    def deanonymise(self, text: str) -> str:
        """Put real values back. A sentinel the map never issued is left alone."""
        return SENTINEL.sub(lambda match: self._to_value.get(match.group(0), match.group(0)), text)

    def deanonymise_value(self, value: Any) -> Any:
        """Recursively restore sentinels inside a JSON-compatible payload."""
        if isinstance(value, str):
            return self.deanonymise(value)
        if isinstance(value, list):
            return [self.deanonymise_value(item) for item in value]
        if isinstance(value, dict):
            return {key: self.deanonymise_value(item) for key, item in value.items()}
        return value

    def unresolved(self, value: Any) -> str | None:
        """The first sentinel-shaped token in `value` that this map never issued.

        A model can echo a placeholder it saw in an earlier turn or copy one from
        the prompt. `deanonymise` leaves it alone (correctly), so without this
        check a literal `[EMAIL_1]` would be handed to a tool.
        """
        for text in _walk_strings(value):
            for match in SENTINEL.finditer(text):
                if match.group(0) not in self._to_value:
                    return match.group(0)
        return None


@dataclass(eq=False)
class SentinelStore:
    """Session-scoped sentinel maps (ADR-0026, ADR-0027).

    The map has to outlive a run: a customer gives an address in one turn and
    confirms the order in the next. It also has to outlive a *process* — a server
    restart used to strand the next turn with an unresolvable placeholder — so
    when a key is configured the map is sealed into `SentinelVault` on every new
    sentinel. Without a key nothing is written and the old memory-only behaviour
    stands; the store never writes plaintext either way.
    """

    ttl_s: float = 3600.0
    max_sessions: int = 256
    vault: Any = None
    _maps: dict[str, SentinelMap] = field(default_factory=dict)
    _seen: dict[str, float] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    @classmethod
    def from_settings(cls) -> SentinelStore:
        """Build the process store from `.env`: vaulted only when a key is present."""
        from rigging.config import settings
        from rigging.crypto import load_key
        from rigging.errors import CryptoError
        from rigging.vault import SentinelVault, _warn

        store = cls(ttl_s=settings.pii_vault_ttl_s)
        if not settings.pii_key.strip():
            return store
        try:
            key = load_key(settings.pii_key)
        except CryptoError as exc:
            _warn(f"{exc}; sentinel maps stay in memory and are lost on restart")
            return store
        store.vault = SentinelVault(Path(settings.pii_vault_dir or "runs/pii-vault"), key)
        return store

    def get(self, session_id: str, *, now: float | None = None) -> SentinelMap:
        with self._lock:
            at = time.monotonic() if now is None else now
            self._prune(at)
            sentinels = self._maps.get(session_id)
            if sentinels is None:
                sentinels = self._restore(session_id)
                sentinels.on_change = lambda: self._save(session_id, sentinels)
                self._maps[session_id] = sentinels
            self._seen[session_id] = at
            self._trim()
            return sentinels

    def forget(self, session_id: str) -> None:
        with self._lock:
            replaced = self._maps.pop(session_id, None)
            self._seen.pop(session_id, None)
            if self.vault is not None:
                self.vault.forget(session_id)
            del replaced

    def clear(self) -> None:
        with self._lock:
            sessions = list(self._maps)
            self._maps.clear()
            self._seen.clear()
            if self.vault is not None:
                for session_id in sessions:
                    self.vault.forget(session_id)

    def __len__(self) -> int:
        with self._lock:
            return len(self._maps)

    def __contains__(self, session_id: object) -> bool:
        with self._lock:
            return session_id in self._maps

    def _restore(self, session_id: str) -> SentinelMap:
        """The vaulted map for this session, or an empty one."""
        if self.vault is None:
            return SentinelMap()
        payload = self.vault.load(session_id)
        if payload is None:
            return SentinelMap()
        saved_at = float(payload.get("saved_at") or 0.0)
        if time.time() - saved_at > self.ttl_s:
            self.vault.forget(session_id)
            return SentinelMap()
        return SentinelMap.from_dict(payload)

    def _save(self, session_id: str, sentinels: SentinelMap) -> None:
        if self.vault is None:
            return
        self.vault.save(session_id, sentinels.to_dict())

    def _prune(self, at: float) -> None:
        for session_id in [s for s, seen in self._seen.items() if at - seen > self.ttl_s]:
            self._maps.pop(session_id, None)
            self._seen.pop(session_id, None)
            if self.vault is not None:
                self.vault.forget(session_id)
        if self.vault is not None:
            self.vault.prune(self.ttl_s)

    def _trim(self) -> None:
        while len(self._maps) > self.max_sessions:
            oldest = min(self._seen, key=lambda session: self._seen[session])
            self._maps.pop(oldest, None)
            self._seen.pop(oldest, None)
            if self.vault is not None:
                self.vault.forget(oldest)


#: The process's store. Tests build their own; the loop resolves this one.
DEFAULT_SENTINEL_STORE = SentinelStore.from_settings()


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


class PIIPseudonymiseGuardrail:
    """Stable placeholders for the model, real values for the tools (ADR-0025/0026).

    The loop's hook order is what makes this work: `on_input` runs before
    `input_received` is logged, `on_tool_call` after `tool_call` is logged (so the
    log keeps the sentinel), and `on_tool_result` before `tool_result` is logged.

    With a `session`, the map is shared across the session's runs, so a value
    given in one turn is still resolvable in the next. Without one (a single
    `harness run`, an eval case) it lives for the run only.
    """

    name = "pii_pseudonymise"
    hooks = ("on_input", "on_tool_call", "on_tool_result", "on_output")
    #: `assemble()` injects the session id; see ADR-0026.
    wants_session = True

    def __init__(self, restore_output: bool = False, session: str | None = None,
                 store: SentinelStore | None = None) -> None:
        # Restoring in `on_output` puts real values in the final answer — and in
        # the root span, because spans are not redacted. Off by default.
        self.restore_output = restore_output
        self.session = session
        self.store = store or DEFAULT_SENTINEL_STORE
        self.sentinels = self.store.get(session) if session else SentinelMap()

    def on_input(self, messages: Sequence[Message]) -> Verdict:
        joined = "\n".join(m.content for m in messages)
        cleaned = self.sentinels.sentinelise(joined)
        return Transform(cleaned, "pii pseudonymised") if cleaned != joined else Allow()

    def on_tool_call(self, call: ToolCall, tool: object) -> Verdict:
        missing = self.sentinels.unresolved(call.arguments)
        if missing is not None:
            return Deny(
                f"{missing} is a placeholder this session never issued; "
                "ask the customer for the value instead of passing it on"
            )
        restored = self.sentinels.deanonymise_value(call.arguments)
        if restored == call.arguments:
            return Allow()
        return Transform(call.model_copy(update={"arguments": restored}), "pii restored for the tool")

    def on_tool_result(self, call: ToolCall, content: str) -> Verdict:
        cleaned = self.sentinels.sentinelise(content)
        return Transform(cleaned, "pii pseudonymised") if cleaned != content else Allow()

    def on_output(self, text: str, messages: Sequence[Message]) -> Verdict:
        if not self.restore_output:
            return Allow()
        restored = self.sentinels.deanonymise(text)
        return Transform(restored, "pii restored for the user") if restored != text else Allow()
