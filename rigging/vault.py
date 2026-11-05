"""Encrypted session store for sentinel maps (ADR-0027, amending ADR-0026).

ADR-0026 kept the map that resolves `[EMAIL_1]` in process memory only, on the
grounds that "the log stays a dead end without the process". The cost showed up
in the first real conversation: a customer gives an address in one turn, the
server restarts, and the next turn has nothing to restore — the contact is gone.
This store keeps that promise on disk instead: one file per session, AES-GCM,
keyed by `RIGGING_PII_KEY`, never readable without the key.

A file name is `sha256(session_id)`, so the vault directory does not leak which
sessions exist. Each file is a single sealed JSON blob; nothing outside the vault
needs the plaintext, and a failure to open one is a warning, never a failed run —
a lost map degrades to the pre-ADR-0027 behaviour (ask the customer again).
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rigging.crypto import open_sealed, seal
from rigging.errors import CryptoError

_warned = False


def _warn(message: str) -> None:
    """Say it once per process: a broken vault must not flood a session's turns."""
    global _warned
    if not _warned:
        _warned = True
        print(f"warning: pii vault: {message}", file=sys.stderr)


@dataclass
class SentinelVault:
    """Per-session sentinel maps, sealed on disk (ADR-0027)."""

    directory: Path
    key: bytes

    def _path(self, session_id: str) -> Path:
        digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:32]
        return self.directory / f"{digest}.pii"

    def save(self, session_id: str, payload: dict[str, Any]) -> None:
        """Write one session's map. Failures are a warning: they never fail a run."""
        body = json.dumps({"session": session_id, "saved_at": time.time(), **payload},
                          separators=(",", ":"))
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            sealed = seal(body, self.key, aad=f"sentinel:{session_id}")
            target = self._path(session_id)
            tmp = target.with_suffix(".tmp")
            tmp.write_text(sealed, encoding="utf-8")
            os.chmod(tmp, 0o600)
            tmp.replace(target)
        except (OSError, CryptoError) as exc:
            _warn(f"could not persist the sentinel map for a session ({exc}); "
                  "it stays in memory and is lost on restart")

    def load(self, session_id: str) -> dict[str, Any] | None:
        """The sealed map for a session, or None when there is none worth trusting."""
        target = self._path(session_id)
        try:
            sealed = target.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as exc:
            _warn(f"could not read {target.name} ({exc})")
            return None
        try:
            payload: dict[str, Any] = json.loads(open_sealed(sealed, self.key,
                                                             aad=f"sentinel:{session_id}"))
        except (CryptoError, json.JSONDecodeError) as exc:
            _warn(f"{target.name} does not open with this key ({exc}); ignoring it. "
                  "Placeholders from that session will be unresolvable.")
            return None
        if payload.get("session") != session_id:
            _warn(f"{target.name} belongs to another session; ignoring it")
            return None
        return payload

    def forget(self, session_id: str) -> None:
        try:
            self._path(session_id).unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:  # pragma: no cover - a locked-down directory
            _warn(f"could not remove a session's vault file ({exc})")

    def prune(self, ttl_s: float, *, now: float | None = None) -> None:
        """Drop files whose map is older than `ttl_s`, so the vault is bounded too."""
        if not self.directory.exists():
            return
        at = time.time() if now is None else now
        for path in self.directory.glob("*.pii"):
            try:
                if at - path.stat().st_mtime > ttl_s:
                    path.unlink()
            except OSError:  # pragma: no cover - racing another turn
                continue
