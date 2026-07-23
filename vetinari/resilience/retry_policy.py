"""Local retry and bounded backoff primitives."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeVar

import requests

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class RetryBudget:
    """Bounded retry budget with deterministic jitter hooks."""

    max_attempts: int = 3
    base_delay_seconds: float = 0.25
    max_delay_seconds: float = 2.0
    jitter_seconds: float = 0.0

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if self.base_delay_seconds < 0:
            raise ValueError("base_delay_seconds must be non-negative")
        if self.max_delay_seconds < 0:
            raise ValueError("max_delay_seconds must be non-negative")
        if self.jitter_seconds < 0:
            raise ValueError("jitter_seconds must be non-negative")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"RetryBudget(max_attempts={self.max_attempts!r}, base_delay_seconds={self.base_delay_seconds!r}, max_delay_seconds={self.max_delay_seconds!r})"


RetryClassifier = Callable[[BaseException], bool]
Sleeper = Callable[[float], None]
RandomSource = Callable[[], float]


def is_retryable_transport_error(exc: BaseException) -> bool:
    """Return true for transient transport failures worth retrying."""
    return isinstance(
        exc,
        (
            requests.ConnectionError,
            requests.Timeout,
            requests.HTTPError,
        ),
    )


class RetryPolicy:
    """Execute a callable with bounded exponential delay and injectable jitter."""

    def __init__(
        self,
        budget: RetryBudget | None = None,
        *,
        classifier: RetryClassifier = is_retryable_transport_error,
        random_source: RandomSource | None = None,
        sleeper: Sleeper | None = None,
    ) -> None:
        self.budget = budget or RetryBudget()
        self._classifier = classifier
        self._random_source = random_source or (lambda: 0.0)
        self._sleeper = sleeper or time.sleep

    def delay_for_attempt(self, attempt_number: int) -> float:
        """Return the bounded delay before the next attempt.

                ``attempt_number`` is one-based and refers to the failed attempt that
                just completed.

        Returns:
            float value produced by delay_for_attempt().

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        if attempt_number < 1:
            raise ValueError("attempt_number must be at least 1")
        exponential = self.budget.base_delay_seconds * (2 ** (attempt_number - 1))
        jitter_fraction = min(max(self._random_source(), 0.0), 1.0)
        jitter = self.budget.jitter_seconds * jitter_fraction
        return min(exponential + jitter, self.budget.max_delay_seconds)

    def should_retry(self, exc: BaseException, attempt_number: int) -> bool:
        """Return true when another attempt is allowed for this exception."""
        return attempt_number < self.budget.max_attempts and self._classifier(exc)

    def run(self, operation: Callable[[], T]) -> T:
        """Run ``operation`` until it succeeds or the retry budget is exhausted.

        Returns:
            T value produced by run().

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        attempt = 1
        while True:
            try:
                return operation()
            except Exception as exc:
                if not self.should_retry(exc, attempt):
                    raise
                self._sleeper(self.delay_for_attempt(attempt))
                attempt += 1
