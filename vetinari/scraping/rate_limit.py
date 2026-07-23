"""Per-host token-bucket rate limiter for scraping dispatch."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from vetinari.guards import GateError


@dataclass
class _HostRateBucket:
    tokens: float
    last_refill: float


class HostRateLimiter:
    """Thread-safe token bucket keyed by target host."""

    def __init__(self, *, requests_per_second: float = 1.0, burst: int = 1) -> None:
        if requests_per_second <= 0:
            raise GateError("scraping_rate_limit", "invalid rate-limit config: requests_per_second must be > 0")
        if burst < 1:
            raise GateError("scraping_rate_limit", "invalid rate-limit config: burst must be >= 1")
        self.requests_per_second = requests_per_second
        self.burst = burst
        self._buckets: dict[str, _HostRateBucket] = {}
        self._lock = threading.Lock()

    def acquire(self, host: str, *, clock: Callable[[], float] = time.monotonic) -> bool:
        """Return whether a request slot is available for ``host``.

        Args:
            host: Target network host.
            clock: Monotonic clock provider, injectable for tests.

        Returns:
            ``True`` if a request can proceed immediately.
        """
        now = clock()
        with self._lock:
            bucket = self._buckets.get(host)
            if bucket is None:
                self._buckets[host] = _HostRateBucket(tokens=float(self.burst - 1), last_refill=now)
                return True

            elapsed = max(0.0, now - bucket.last_refill)
            bucket.tokens = min(float(self.burst), bucket.tokens + elapsed * self.requests_per_second)
            bucket.last_refill = now
            if bucket.tokens >= 1.0:
                bucket.tokens -= 1.0
                return True
            return False
