"""Transient-only exponential-backoff retry policy for scraper dispatch.

The transient set is NETWORK_ERROR, HTTP_5XX, and TIMEOUT. The policy reports
retry decisions and delay values only; the dispatcher owns sleeping and
executing additional attempts.
"""

from __future__ import annotations

import random

from vetinari.scraping.contracts import ScrapeFailureReason, ScraperResult

_TRANSIENT_REASONS = frozenset({
    ScrapeFailureReason.NETWORK_ERROR,
    ScrapeFailureReason.HTTP_5XX,
    ScrapeFailureReason.TIMEOUT,
})

# Canonical public alias; consumers must import this instead of defining local copies.
TRANSIENT_REASONS: frozenset[ScrapeFailureReason] = _TRANSIENT_REASONS


class ExponentialBackoffRetryPolicy:
    """Retry transient scraper failures with capped exponential backoff."""

    def __init__(
        self,
        max_attempts: int = 3,
        base_delay_s: float = 1.0,
        max_delay_s: float = 30.0,
        jitter: float = 0.1,
    ) -> None:
        if max_attempts < 1:
            msg = "max_attempts must be >= 1"
            raise ValueError(msg)
        if base_delay_s <= 0:
            msg = "base_delay_s must be > 0"
            raise ValueError(msg)
        if max_delay_s < base_delay_s:
            msg = "max_delay_s must be >= base_delay_s"
            raise ValueError(msg)
        if jitter < 0 or jitter > 1:
            msg = "jitter must be between 0 and 1"
            raise ValueError(msg)
        self.max_attempts = max_attempts
        self.base_delay_s = base_delay_s
        self.max_delay_s = max_delay_s
        self.jitter = jitter
        self._random = random.SystemRandom()

    def should_retry(self, result: ScraperResult, attempt: int) -> bool:
        """Return whether another attempt should run after ``attempt``.

        Args:
            result: Scrape result from the previous attempt.
            attempt: One-based attempt number that just completed.

        Returns:
            True when the result is transient and budget remains.
        """
        if attempt >= self.max_attempts:
            return False
        if result.passed:
            return False
        return result.reason in _TRANSIENT_REASONS

    def delay_s(self, attempt: int) -> float:
        """Return capped backoff delay for one-based ``attempt``.

        Returns:
            Delay in seconds after jitter and max-delay clamping.
        """
        base = self.base_delay_s * (2 ** max(0, attempt - 1))
        if self.jitter:
            base *= 1 + self._random.uniform(-self.jitter, self.jitter)
        return min(base, self.max_delay_s)
