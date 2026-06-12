"""Lightweight, dependency-free rate limiting for the public /ask endpoint.

Bounds token spend on a publicly reachable URL: a per-IP requests/minute window plus an
optional global daily cap. In-process (no Redis/extra infra) — correct for a single
instance (the demo case); for a horizontally-scaled deploy use a shared store. Both limits
default to 0 = disabled, so local dev and tests are unaffected.
"""

from __future__ import annotations

import threading
from collections import defaultdict, deque


class RateLimiter:
    _PRUNE_EVERY = 1024  # sweep stale per-IP entries so the table can't grow unbounded

    def __init__(self, *, per_minute: int = 0, per_day_total: int = 0) -> None:
        self.per_minute = per_minute
        self.per_day_total = per_day_total
        self._ip_hits: dict[str, deque[float]] = defaultdict(deque)
        self._day_count = 0
        self._day_start = 0.0
        self._checks = 0
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return self.per_minute > 0 or self.per_day_total > 0

    def check(self, ip: str, *, now: float) -> tuple[bool, str]:
        """Return (allowed, reason). `reason` is "" when allowed. `now` is a unix time
        (injected so the logic is deterministically testable)."""
        with self._lock:
            self._checks += 1
            if self._checks % self._PRUNE_EVERY == 0:
                cutoff = now - 60
                # NB: a distinct loop var — `ip` is the caller's key and must not be rebound.
                stale = [k for k, hits in self._ip_hits.items() if not hits or hits[-1] < cutoff]
                for stale_ip in stale:
                    del self._ip_hits[stale_ip]

            # Global daily cap (rolling 24h window).
            if self.per_day_total > 0 and now - self._day_start >= 86_400:
                self._day_start = now
                self._day_count = 0
            if self.per_day_total > 0 and self._day_count >= self.per_day_total:
                return False, "daily query cap reached; please try again later"

            # Per-IP requests-per-minute (sliding 60s window).
            if self.per_minute > 0:
                hits = self._ip_hits[ip]
                cutoff = now - 60
                while hits and hits[0] < cutoff:
                    hits.popleft()
                if len(hits) >= self.per_minute:
                    return False, f"rate limit: max {self.per_minute} requests/minute per IP"
                hits.append(now)

            if self.per_day_total > 0:
                self._day_count += 1
            return True, ""
