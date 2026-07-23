"""Small clock abstractions for deterministic tests and elapsed-time probes."""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol


class Clock(Protocol):
    """Clock interface for code that needs injectable time."""

    def utc_now(self) -> datetime:
        """Return the current UTC datetime.

        Returns:
            Current timezone-aware UTC datetime.
        """

    def monotonic(self) -> float:
        """Return a monotonic timestamp for elapsed-time measurement."""


@dataclass(frozen=True)
class SystemClock:
    """Production clock backed by the standard library."""

    def utc_now(self) -> datetime:
        """Return the current UTC datetime."""
        return datetime.now(timezone.utc)

    def monotonic(self) -> float:
        """Return the current monotonic clock value."""
        return time.monotonic()


@dataclass
class FrozenClock:
    """Deterministic clock for tests."""

    current: datetime
    monotonic_value: float = 0.0

    def utc_now(self) -> datetime:
        """Return the frozen UTC datetime.

        Returns:
            Frozen timezone-aware UTC datetime.
        """
        if self.current.tzinfo is None:
            return self.current.replace(tzinfo=timezone.utc)
        return self.current.astimezone(timezone.utc)

    def monotonic(self) -> float:
        """Return the frozen monotonic clock value."""
        return self.monotonic_value

    def advance(self, *, seconds: float) -> None:
        """Move the frozen wall and monotonic clocks forward."""
        self.monotonic_value += seconds
        self.current = self.utc_now() + timedelta(seconds=seconds)


def utc_now_iso(clock: Clock | None = None) -> str:
    """Return an ISO-8601 UTC timestamp from the supplied clock.

    Returns:
        UTC timestamp string.
    """
    active = clock or SystemClock()
    return active.utc_now().astimezone(timezone.utc).isoformat()


def monotonic(clock: Clock | None = None) -> float:
    """Return a monotonic timestamp from the supplied clock.

    Returns:
        Monotonic timestamp value.
    """
    active = clock or SystemClock()
    return active.monotonic()


__all__ = ["Clock", "FrozenClock", "SystemClock", "monotonic", "utc_now_iso"]
