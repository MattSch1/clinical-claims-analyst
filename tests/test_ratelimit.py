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


def test_prune_keeps_callers_own_bucket():
    """The periodic stale-IP sweep must not rebind the caller's key: the caller's request
    is recorded under their own IP, and a pruned IP is not resurrected."""
    rl = RateLimiter(per_minute=1)
    rl.check("stale", now=0.0)                 # seed an entry that will age out
    rl._checks = rl._PRUNE_EVERY - 1           # next check triggers the prune sweep
    assert rl.check("caller", now=200.0)[0]    # stale (>60s old) is pruned this call
    # caller's hit landed in caller's bucket (not the pruned IP's) → 2nd hit is limited
    assert not rl.check("caller", now=200.5)[0]
    assert "stale" not in rl._ip_hits          # pruned and not resurrected
