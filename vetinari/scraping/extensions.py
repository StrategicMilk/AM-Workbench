"""Process-local extension registry for scraper hardening plug-ins."""

from __future__ import annotations

import threading
from collections.abc import Callable, Sequence
from typing import Protocol

from vetinari.scraping.contracts import RetryPolicyProtocol, ScraperCacheProtocol, ScrapeRequest, ScraperResult

PreFlightCheck = Callable[[ScrapeRequest], ScraperResult | None]


class EventHook(Protocol):
    """Completion hook called once per dispatcher fetch."""

    def __call__(
        self,
        req: ScrapeRequest,
        result: ScraperResult,
        *,
        started_at_monotonic: float,
    ) -> None:
        """Receive the completed request, result, and start-time monotonic clock."""


_lock = threading.Lock()
_pre_flight_checks: list[PreFlightCheck] = []
_cache: ScraperCacheProtocol | None = None
_retry_policy: RetryPolicyProtocol | None = None
_event_hooks: list[EventHook] = []


def register_pre_flight(check: PreFlightCheck) -> None:
    """Register an idempotent pre-flight check."""
    with _lock:
        if check not in _pre_flight_checks:
            _pre_flight_checks.append(check)


def register_cache(cache: ScraperCacheProtocol) -> None:
    """Register the process cache extension."""
    global _cache
    with _lock:
        _cache = cache


def register_retry_policy(policy: RetryPolicyProtocol) -> None:
    """Register the process retry policy extension."""
    global _retry_policy
    with _lock:
        _retry_policy = policy


def register_event_hook(hook: EventHook) -> None:
    """Register an idempotent scraper completion event hook."""
    with _lock:
        if hook not in _event_hooks:
            _event_hooks.append(hook)


def get_pre_flight_checks() -> Sequence[PreFlightCheck]:
    """Return a snapshot of registered pre-flight checks.

    Returns:
        Tuple of registered checks.
    """
    with _lock:
        return tuple(_pre_flight_checks)


def get_cache() -> ScraperCacheProtocol | None:
    """Return the registered cache extension, if any.

    Returns:
        Cache extension or ``None``.
    """
    with _lock:
        return _cache


def get_retry_policy() -> RetryPolicyProtocol | None:
    """Return the registered retry policy extension, if any.

    Returns:
        Retry policy extension or ``None``.
    """
    with _lock:
        return _retry_policy


def get_event_hooks() -> Sequence[EventHook]:
    """Return a snapshot of registered completion event hooks.

    Returns:
        Tuple of event hooks.
    """
    with _lock:
        return tuple(_event_hooks)


def reset_extensions_for_tests() -> None:
    """Clear registry state for isolated tests."""
    global _cache, _retry_policy
    with _lock:
        _pre_flight_checks.clear()
        _cache = None
        _retry_policy = None
        _event_hooks.clear()
