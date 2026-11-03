"""The service rate limiter (ADR-0021): fixed windows, per key, bounded memory.

The route-level behaviour is in `test_service.py`; these are the properties that
are awkward to observe through HTTP.
"""

from __future__ import annotations

from rigging.service.ratelimit import RateLimiter


def test_each_key_has_its_own_budget():
    limiter = RateLimiter(limit=2)
    assert limiter.allow("a")[0] is True
    assert limiter.allow("a")[0] is True
    assert limiter.allow("a")[0] is False
    assert limiter.allow("b")[0] is True


def test_a_refusal_carries_a_retry_hint():
    limiter = RateLimiter(limit=1, window_s=30)
    assert limiter.allow("a") == (True, 0.0)
    allowed, retry_after = limiter.allow("a")
    assert allowed is False
    assert 0 < retry_after <= 30


def test_the_window_rolls_over(monkeypatch):
    now = [0.0]
    limiter = RateLimiter(limit=1, window_s=10)
    monkeypatch.setattr(limiter, "_now", lambda: now[0])

    assert limiter.allow("a")[0] is True
    assert limiter.allow("a")[0] is False
    now[0] = 10.0
    assert limiter.allow("a")[0] is True


def test_zero_or_negative_limit_disables_the_check():
    for limit in (0, -5):
        limiter = RateLimiter(limit=limit)
        assert all(limiter.allow("a")[0] for _ in range(100))


def test_memory_is_bounded_when_keys_are_attacker_chosen():
    limiter = RateLimiter(limit=5, max_keys=10)
    for i in range(200):
        limiter.allow(f"spoofed-{i}")
    assert len(limiter._windows) <= 10
