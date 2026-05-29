"""Hermetic tests for the /ask rate limiter (deterministic — `now` is injected)."""

from __future__ import annotations

from src.api.ratelimit import RateLimiter


def test_disabled_allows_everything():
    rl = RateLimiter()  # 0 / 0
    assert not rl.enabled
    assert all(rl.check("x", now=float(i))[0] for i in range(100))


def test_per_minute_window_is_per_ip_and_slides():
    rl = RateLimiter(per_minute=2)
    assert rl.check("1.1.1.1", now=100.0)[0]
    assert rl.check("1.1.1.1", now=100.5)[0]
    ok, reason = rl.check("1.1.1.1", now=101.0)
    assert not ok and "rate limit" in reason
    # a different IP is unaffected
    assert rl.check("2.2.2.2", now=101.0)[0]
    # the 60s window slides: the first hit ages out
    assert rl.check("1.1.1.1", now=161.0)[0]


def test_global_daily_cap():
    rl = RateLimiter(per_day_total=2)
    assert rl.check("a", now=0.0)[0]
    assert rl.check("b", now=1.0)[0]
    ok, reason = rl.check("c", now=2.0)
    assert not ok and "daily" in reason
    # the rolling 24h window resets
    assert rl.check("d", now=86_400.0 + 3)[0]
