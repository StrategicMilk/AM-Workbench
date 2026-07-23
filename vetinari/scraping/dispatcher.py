"""Dispatcher for static-then-dynamic scraping fallback.

Fallback table:
- robots or rate-limit denial returns before any backend runs.
- static success returns with ``backend_chain=("static",)``.
- static NEEDS_JS, EXTRACTION_TOO_SHORT, or MIME_HTML_BUT_EMPTY escalates to
  dynamic and returns ``backend_chain=("static", "dynamic")``.
- all other static failures return fail-closed without escalation.
"""

from __future__ import annotations

import inspect
import logging
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import datetime, timezone
from importlib import import_module
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

try:
    yaml: Any | None = import_module("yaml")
except ImportError:
    yaml = None

from vetinari.scraping.contracts import (
    RetryPolicyProtocol,
    ScrapeFailureReason,
    ScraperCacheProtocol,
    ScrapeRequest,
    ScraperResult,
)
from vetinari.scraping.dynamic_backend import DynamicBackend
from vetinari.scraping.extensions import get_cache, get_event_hooks, get_pre_flight_checks, get_retry_policy
from vetinari.scraping.rate_limit import HostRateLimiter
from vetinari.scraping.retry import TRANSIENT_REASONS as _TRANSIENT_REASONS
from vetinari.scraping.robots import RobotsCache
from vetinari.scraping.static_backend import DEFAULT_UA, StaticBackend

logger = logging.getLogger(__name__)


PreFlightCheck = Callable[[ScrapeRequest], ScraperResult | None]
_ESCALATE_REASONS = {
    ScrapeFailureReason.NEEDS_JS,
    ScrapeFailureReason.EXTRACTION_TOO_SHORT,
    ScrapeFailureReason.MIME_HTML_BUT_EMPTY,
}
# Defensive hard cap on retry loop — defends against buggy policies that
# return ``should_retry=True`` indefinitely. Production policies
# (e.g. ExponentialBackoffRetryPolicy) enforce smaller caps via their own
# max_attempts; this only fires for misbehaving custom policies.
_HARD_RETRY_CAP = 10
_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "scraping.yaml"
_CONFIG_TTL_S = 30.0
_config_lock = threading.Lock()
_config_loaded_at = 0.0
_config_cache: dict[str, object] = {}
_default_lock = threading.Lock()
_default_dispatcher: Dispatcher | None = None


def _failure(req: ScrapeRequest, reason: ScrapeFailureReason, *, detail: str | None = None) -> ScraperResult:
    return ScraperResult(
        passed=False,
        reason=reason,
        url=req.url,
        final_url=None,
        http_status=None,
        mime=None,
        title=None,
        text=None,
        extracted_chars=0,
        fetched_at_utc=datetime.now(timezone.utc),
        backend="dispatcher",
        backend_chain=(),
        error_detail=detail,
    )


def _load_config() -> dict[str, object]:
    """TTL-cached read of scraper defaults from ``config/scraping.yaml``."""
    global _config_cache, _config_loaded_at
    now = time.monotonic()
    with _config_lock:
        if _config_cache and now - _config_loaded_at < _CONFIG_TTL_S:
            return dict(_config_cache)
        if yaml is None or not _CONFIG_PATH.exists():
            _config_cache = {}
        else:
            loaded = yaml.safe_load(_CONFIG_PATH.read_text(encoding="utf-8"))
            _config_cache = loaded if isinstance(loaded, dict) else {}
        _config_loaded_at = now
        return dict(_config_cache)


def _nested_float(config: dict[str, object], section: str, key: str, default: float) -> float:
    value = config.get(section)
    if isinstance(value, dict):
        nested = value.get(key)
        if isinstance(nested, int | float):
            return float(nested)
    return default


def _nested_int(config: dict[str, object], section: str, key: str, default: int) -> int:
    value = config.get(section)
    if isinstance(value, dict):
        nested = value.get(key)
        if isinstance(nested, int):
            return nested
    return default


class Dispatcher:
    """Coordinate pre-flight checks, static fetch, and dynamic fallback."""

    def __init__(
        self,
        *,
        static: StaticBackend | None = None,
        dynamic: DynamicBackend | None = None,
        robots: RobotsCache | None = None,
        rate_limiter: HostRateLimiter | None = None,
        pre_flight_checks: Sequence[PreFlightCheck] = (),
        cache: ScraperCacheProtocol | None = None,
        retry_policy: RetryPolicyProtocol | None = None,
        _live_extensions: bool = False,
    ) -> None:
        self.static = static or StaticBackend()
        self.dynamic = dynamic
        self.robots = robots or RobotsCache()
        self.rate_limiter = rate_limiter or HostRateLimiter()
        self.pre_flight_checks = tuple(pre_flight_checks)
        self.cache = cache
        self.retry_policy = retry_policy
        # Side effects: when True, _preflight / fetch resolve pre_flight_checks,
        # cache, and retry_policy from the live extension registry on each call
        # instead of using the snapshotted constructor values. This lets
        # default_dispatcher() remain a singleton while still seeing extensions
        # registered after first construction (e.g. Pack K hooks).
        self._live_extensions = _live_extensions

    def _effective_cache(self) -> ScraperCacheProtocol | None:
        """Return cache from live registry when live-extensions mode is active."""
        return get_cache() if self._live_extensions else self.cache

    def _effective_retry_policy(self) -> RetryPolicyProtocol | None:
        """Return retry policy from live registry when live-extensions mode is active."""
        return get_retry_policy() if self._live_extensions else self.retry_policy

    def _effective_pre_flight_checks(self) -> Sequence[PreFlightCheck]:
        """Return pre-flight checks from live registry when live-extensions mode is active."""
        return get_pre_flight_checks() if self._live_extensions else self.pre_flight_checks

    def fetch(self, req: ScrapeRequest) -> ScraperResult:
        """Return a scrape result after cache, pre-flight, and backend routing.

        Args:
            req: Scrape request to dispatch.

        Returns:
            Typed result from cache, pre-flight denial, static backend, or dynamic fallback.
        """
        started_at = time.monotonic()
        result = self._fetch_core(req)
        self._emit_event_hooks(req, result, started_at_monotonic=started_at)
        return result

    def _fetch_core(self, req: ScrapeRequest) -> ScraperResult:
        """Run dispatcher logic without emitting completion hooks."""
        preflight = self._preflight(req)
        if preflight is not None:
            return preflight

        effective_cache = self._effective_cache()
        cached = effective_cache.get(req) if effective_cache is not None else None
        if cached is not None:
            return replace(cached, cached=True)

        effective_retry = self._effective_retry_policy()
        attempt = 1
        result = self._fetch_once(req, attempt=attempt)
        while effective_retry is not None and effective_retry.should_retry(result, attempt):
            if attempt >= _HARD_RETRY_CAP:
                # Defensive backstop only — see _HARD_RETRY_CAP comment.
                logger.warning("scraping_retry impact=hard-cap policy=%r attempts=%d", effective_retry, attempt)
                break
            attempt += 1
            delay_s = effective_retry.delay_s(attempt)
            if delay_s > 0:
                # Synchronous backend (httpx sync client) — event-driven retry
                # is not feasible without restructuring the entire call chain.
                delay_s = min(delay_s, 60.0)  # cap to prevent unbounded blocking
                time.sleep(delay_s)
            result = self._fetch_once(req, attempt=attempt)
        if effective_retry is not None and attempt > 1 and not result.passed and result.reason in _TRANSIENT_REASONS:
            # We actually retried (attempt > 1) and the loop exited while still
            # on a transient failure → policy declined to retry further (its
            # own cap was reached) or the hard backstop fired. The original
            # transient reason is preserved in error_detail for triage.
            result = replace(
                result,
                reason=ScrapeFailureReason.RETRY_EXHAUSTED,
                passed=False,
                attempts=attempt,
                error_detail=f"retry policy exhausted after {attempt} attempts",
            )

        if effective_cache is not None and result.passed:
            effective_cache.put(req, result)
        return result

    @staticmethod
    def _emit_event_hooks(req: ScrapeRequest, result: ScraperResult, *, started_at_monotonic: float) -> None:
        """Invoke registered completion hooks without affecting fetch outcome."""
        for hook in get_event_hooks():
            try:
                hook(req, result, started_at_monotonic=started_at_monotonic)
            except Exception as exc:
                logger.warning("scraping event hook failed hook=%r error=%s", hook, exc)

    def _preflight(self, req: ScrapeRequest) -> ScraperResult | None:
        """Run robots, rate-limit, and registered pre-flight checks.

        When ``_live_extensions`` is True, pre-flight checks are resolved from the
        live extension registry on each call, so hooks registered after
        ``default_dispatcher()`` is first constructed are still applied.

        Args:
            req: Scrape request to evaluate.

        Returns:
            A denial result when any check blocks the request, otherwise ``None``.
        """
        ua = req.user_agent or DEFAULT_UA
        for check in self._effective_pre_flight_checks():
            result = check(req)
            if result is not None:
                return result
        allowed_check = getattr(self.robots, "is_allowed_sync", None)
        if allowed_check is None:
            allowed_check = self.robots.is_allowed
        allowed = allowed_check(req.url, ua)
        if inspect.isawaitable(allowed):
            close = getattr(allowed, "close", None)
            if close is not None:
                close()
            allowed = False
        if not allowed:
            return _failure(req, ScrapeFailureReason.ROBOTS_DENIED)
        host = urlsplit(req.url).netloc
        if not self.rate_limiter.acquire(host):
            return _failure(req, ScrapeFailureReason.RATE_LIMITED)
        return None

    def _fetch_once(self, req: ScrapeRequest, *, attempt: int) -> ScraperResult:
        static_result = replace(self.static.fetch(req), backend_chain=("static",), attempts=attempt)
        if static_result.passed:
            return static_result
        if static_result.reason not in _ESCALATE_REASONS:
            return static_result
        if self.dynamic is None:
            return replace(static_result, error_detail="dynamic backend not configured")
        dynamic_result = self.dynamic.fetch(req)
        return replace(dynamic_result, backend_chain=("static", "dynamic"), attempts=attempt)


def default_dispatcher() -> Dispatcher:
    """Return the process-default dispatcher with extension registry wiring.

    Returns:
        Lazily constructed dispatcher using default config and extension registry.
    """
    global _default_dispatcher
    if _default_dispatcher is None:
        with _default_lock:
            if _default_dispatcher is None:
                config = _load_config()
                robots = RobotsCache(
                    cache_ttl_s=_nested_float(config, "robots", "cache_ttl_s", 3600.0),
                    fetch_timeout_s=_nested_float(config, "robots", "fetch_timeout_s", 5.0),
                )
                limiter = HostRateLimiter(
                    requests_per_second=_nested_float(config, "rate_limit", "requests_per_second", 1.0),
                    burst=_nested_int(config, "rate_limit", "burst", 1),
                )
                dynamic = DynamicBackend(
                    hydration_extra_ms=_nested_int(config, "dynamic", "hydration_extra_ms", 500),
                )
                # Extensions are NOT snapshotted here — _live_extensions=True
                # means fetch() / _preflight() call get_pre_flight_checks(),
                # get_cache(), and get_retry_policy() on every invocation, so
                # hooks registered by Pack K after the first default_dispatcher()
                # call are always applied.
                _default_dispatcher = Dispatcher(
                    dynamic=dynamic,
                    robots=robots,
                    rate_limiter=limiter,
                    _live_extensions=True,
                )
    return _default_dispatcher
