"""A coarse in-process rate limiter for the read-only service (ADR-0021).

Deliberately small: fixed one-minute windows, one lock, no dependency. It is a
backstop against a runaway client, a stuck SSE loop or a token-guessing script —
not a substitute for the reverse proxy's limiter, which sees the real client
before the request reaches Python and survives a restart.

Keys are client IPs as Starlette sees them. Behind a proxy that sets
`X-Forwarded-For`, uvicorn's proxy-header middleware fills `request.client` with
the original client; without that header every request shares the proxy's IP and
the limit becomes global.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

#: How often a full sweep of expired windows runs, in calls.
_PRUNE_EVERY = 256


@dataclass
class _Window:
    started: float
    count: int


class RateLimiter:
    """Fixed-window request counter, one window per key.

    `limit <= 0` disables the check, which is how an operator turns it off
    without changing code. `max_keys` bounds memory: an attacker who can vary the
    key (a spoofed `X-Forwarded-For` that the proxy failed to overwrite) must not
    be able to grow the dict without limit.
    """

    def __init__(self, limit: int, window_s: float = 60.0, max_keys: int = 10_000) -> None:
        self.limit = limit
        self.window_s = window_s
        self.max_keys = max_keys
        self._lock = threading.Lock()
        self._windows: dict[str, _Window] = {}
        self._calls = 0
        self._now = time.monotonic

    def allow(self, key: str) -> tuple[bool, float]:
        """Return `(allowed, retry_after_seconds)` and count the request."""
        if self.limit <= 0:
            return True, 0.0

        now = self._now()
        with self._lock:
            self._calls += 1
            if self._calls % _PRUNE_EVERY == 0 or len(self._windows) >= self.max_keys:
                self._prune(now)

            window = self._windows.get(key)
            if window is None or now - window.started >= self.window_s:
                self._windows[key] = _Window(started=now, count=1)
                return True, 0.0
            if window.count >= self.limit:
                return False, max(0.0, self.window_s - (now - window.started))
            window.count += 1
            return True, 0.0

    def _prune(self, now: float) -> None:
        expired = [
            key for key, window in self._windows.items() if now - window.started >= self.window_s
        ]
        for key in expired:
            del self._windows[key]
        overflow = len(self._windows) - self.max_keys + 1
        if overflow > 0:
            # Still full of live windows: drop the oldest insertions. Dicts keep
            # insertion order, so this is cheap and deterministic.
            for key in list(self._windows)[:overflow]:
                del self._windows[key]
