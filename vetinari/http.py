"""Shared HTTP session factory for connection pooling and lifecycle management.

Provides a centralized way to create ``requests.Session`` instances with
consistent retry middleware, timeout defaults, and connection pooling.
All adapters, tools, and internal HTTP callers should use this module
instead of creating raw ``requests.Session()`` or calling ``requests.get/post``
directly.
"""

from __future__ import annotations

import logging
import sys
import threading
import types
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from vetinari.resilience.retry_policy import RetryBudget, RetryPolicy
from vetinari.security.redaction import redact_text
from vetinari.security.ssrf_guard_ext import validate_outbound_url

logger = logging.getLogger(__name__)
__path__: list[str] = []


# Default configuration
DEFAULT_TIMEOUT = 30  # seconds — override per-call with timeout= kwarg
DEFAULT_RETRIES = 3  # total retry attempts for transient failures
DEFAULT_BACKOFF_FACTOR = 0.5  # exponential backoff: 0.5s, 1s, 2s
DEFAULT_POOL_CONNECTIONS = 10  # urllib3 connection pool size
DEFAULT_POOL_MAXSIZE = 20  # max connections per host

# Retry on these HTTP status codes (server errors + rate limiting)
RETRY_STATUS_CODES = frozenset({429, 500, 502, 503, 504})

# Registry of all sessions created via this module for cleanup on shutdown
_MAX_SESSIONS = 100  # prevent unbounded session accumulation
_sessions: list[requests.Session] = []
_sessions_lock = threading.Lock()


@dataclass(frozen=True, slots=True)
class HttpTelemetryEvent:
    """Data-only telemetry emitted by the governed client."""

    method: str
    url: str
    timeout_seconds: float
    pool_connections: int
    pool_maxsize: int
    telemetry_label: str
    comparison_label: str | None = None
    attempt: int | None = None

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return (
            f"HttpTelemetryEvent(method={self.method!r}, "
            f"url={redact_text(self.url)!r}, timeout_seconds={self.timeout_seconds!r})"
        )


@dataclass(frozen=True, slots=True)
class GovernedHttpConfig:
    """Explicit HTTP settings for owned outbound adapters."""

    timeout_seconds: float = DEFAULT_TIMEOUT
    pool_connections: int = DEFAULT_POOL_CONNECTIONS
    pool_maxsize: int = DEFAULT_POOL_MAXSIZE
    retry_budget: RetryBudget = field(default_factory=RetryBudget)
    telemetry_label: str = "vetinari.http"
    comparison_label: str | None = None

    def __post_init__(self) -> None:
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if self.pool_connections < 1:
            raise ValueError("pool_connections must be at least 1")
        if self.pool_maxsize < 1:
            raise ValueError("pool_maxsize must be at least 1")
        if not self.telemetry_label:
            raise ValueError("telemetry_label must be non-empty")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"GovernedHttpConfig(timeout_seconds={self.timeout_seconds!r}, pool_connections={self.pool_connections!r}, pool_maxsize={self.pool_maxsize!r})"


Transport = Callable[..., Any]
TelemetrySink = Callable[[HttpTelemetryEvent], None]
RateLimitHook = Callable[[str], None]


def create_session(
    *,
    retries: int = DEFAULT_RETRIES,
    backoff_factor: float = DEFAULT_BACKOFF_FACTOR,
    timeout: float = DEFAULT_TIMEOUT,
    pool_connections: int = DEFAULT_POOL_CONNECTIONS,
    pool_maxsize: int = DEFAULT_POOL_MAXSIZE,
    headers: dict[str, str] | None = None,
) -> requests.Session:
    """Create a configured ``requests.Session`` with retry and pooling.

    The session is registered for cleanup on shutdown via ``close_all()``.

    Args:
        retries: Maximum number of retry attempts for transient failures.
        backoff_factor: Exponential backoff factor between retries.
        timeout: Default timeout in seconds (applied per-request if not overridden).
        pool_connections: Number of connection pools to cache.
        pool_maxsize: Maximum number of connections per pool.
        headers: Optional default headers to set on the session.

    Returns:
        A configured ``requests.Session`` instance.
    """
    session = requests.Session()

    retry_strategy = Retry(
        total=retries,
        backoff_factor=backoff_factor,
        status_forcelist=list(RETRY_STATUS_CODES),
        allowed_methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD"],
        raise_on_status=False,
    )

    adapter = HTTPAdapter(
        max_retries=retry_strategy,
        pool_connections=pool_connections,
        pool_maxsize=pool_maxsize,
    )

    session.mount("http://", adapter)
    session.mount("https://", adapter)

    if headers:
        session.headers.update(headers)

    # Store default timeout as a custom attribute for middleware use.
    vars(session)["_default_timeout"] = timeout

    with _sessions_lock:
        # Evict oldest session if at capacity to prevent unbounded growth
        if len(_sessions) >= _MAX_SESSIONS:
            oldest = _sessions.pop(0)
            try:
                oldest.close()
            except Exception:
                logger.warning("Error closing evicted HTTP session", exc_info=True)
        _sessions.append(session)

    return session


def create_http_session(**kwargs: Any) -> requests.Session:
    """Create a governed HTTP session.

    Args:
        **kwargs: Session construction overrides.

    Returns:
        Configured requests session.
    """
    return create_session(**kwargs)


def is_retryable_method(method: str) -> bool:
    """Return whether an HTTP method can be retried.

    Args:
        method: HTTP method name.

    Returns:
        True when the method is idempotent or explicitly retryable.
    """
    return method.upper() in {"GET", "HEAD", "OPTIONS", "PUT", "DELETE"}


def _register_http_compat_module(name: str, attrs: dict[str, Any]) -> None:
    module_name = f"{__name__}.{name}"
    module = types.ModuleType(module_name)
    module.__dict__.update(attrs)
    sys.modules[module_name] = module
    setattr(sys.modules[__name__], name, module)


_register_http_compat_module("session", {"create_http_session": create_http_session})
_register_http_compat_module("retry", {"is_retryable_method": is_retryable_method})


class GovernedHttpClient:
    """Single governed request surface for owned external HTTP adapters."""

    def __init__(
        self,
        config: GovernedHttpConfig | None = None,
        *,
        transport: Transport | None = None,
        retry_policy: RetryPolicy | None = None,
        telemetry_sink: TelemetrySink | None = None,
        rate_limit_hook: RateLimitHook | None = None,
    ) -> None:
        self.config = config or GovernedHttpConfig()
        if transport is None:
            self._session = create_session(
                retries=0,
                timeout=self.config.timeout_seconds,
                pool_connections=self.config.pool_connections,
                pool_maxsize=self.config.pool_maxsize,
            )
            self._transport: Transport = self._session.request
        else:
            self._session = None
            self._transport = transport
        self._retry_policy = retry_policy or RetryPolicy(self.config.retry_budget)
        self._telemetry_sink = telemetry_sink
        self._rate_limit_hook = rate_limit_hook

    def request(
        self,
        method: str,
        url: str,
        *,
        timeout: float | None = None,
        telemetry_label: str | None = None,
        **kwargs: Any,
    ) -> Any:
        """Execute one governed HTTP request.

        Args:
            method: Method value consumed by request().
            url: Url value consumed by request().
            timeout: Timeout value controlling how long the operation may wait.
            telemetry_label: Telemetry label value consumed by request().
            kwargs: Kwargs value consumed by request().

        Returns:
            Any value produced by request().

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        effective_timeout = self.config.timeout_seconds if timeout is None else timeout
        if effective_timeout <= 0:
            raise ValueError("timeout must be positive")
        validate_outbound_url(url, resolve_hostname=self._session is not None)
        label = telemetry_label or self.config.telemetry_label
        if not label:
            raise ValueError("telemetry_label must be non-empty")
        if self._rate_limit_hook is not None:
            self._rate_limit_hook(label)

        attempts = {"count": 0}

        def _call() -> Any:
            attempts["count"] += 1
            self._emit_telemetry(method, url, effective_timeout, label, attempts["count"])
            response = self._transport(method, url, timeout=effective_timeout, **kwargs)
            raise_for_status = getattr(response, "raise_for_status", None)
            if callable(raise_for_status):
                raise_for_status()
            return response

        return self._retry_policy.run(_call)

    def get(self, url: str, **kwargs: Any) -> Any:
        """Execute a governed GET request."""
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> Any:
        """Execute a governed POST request."""
        return self.request("POST", url, **kwargs)

    def _emit_telemetry(
        self,
        method: str,
        url: str,
        timeout: float,
        label: str,
        attempt: int,
    ) -> None:
        if self._telemetry_sink is None:
            return
        self._telemetry_sink(
            HttpTelemetryEvent(
                method=method.upper(),
                url=redact_text(url),
                timeout_seconds=timeout,
                pool_connections=self.config.pool_connections,
                pool_maxsize=self.config.pool_maxsize,
                telemetry_label=label,
                comparison_label=self.config.comparison_label,
                attempt=attempt,
            ),
        )


def create_governed_client(
    config: GovernedHttpConfig | None = None,
    *,
    transport: Transport | None = None,
    retry_policy: RetryPolicy | None = None,
    telemetry_sink: TelemetrySink | None = None,
    rate_limit_hook: RateLimitHook | None = None,
) -> GovernedHttpClient:
    """Create a governed HTTP client with explicit configuration.

    Returns:
        Newly constructed governed client value.
    """
    if transport is None and retry_policy is not None:
        # The default requests session already applies urllib3 retry; owned
        # retries must be explicit and test-injectable, so disable adapter retry
        # above and use RetryPolicy here.
        return GovernedHttpClient(
            config,
            retry_policy=retry_policy,
            telemetry_sink=telemetry_sink,
            rate_limit_hook=rate_limit_hook,
        )
    return GovernedHttpClient(
        config,
        transport=transport,
        retry_policy=retry_policy,
        telemetry_sink=telemetry_sink,
        rate_limit_hook=rate_limit_hook,
    )


def close_all() -> None:
    """Close all sessions created by this module.

    Called during graceful shutdown to release connection pools and
    underlying sockets. Safe to call multiple times.
    """
    with _sessions_lock:
        for session in _sessions:
            try:
                session.close()
            except Exception:
                logger.warning("Error closing HTTP session", exc_info=True)
        _sessions.clear()
    logger.info("All HTTP sessions closed")


def session_count() -> int:
    """Return the number of active tracked sessions.

    Returns:
        Number of sessions currently registered.
    """
    with _sessions_lock:
        return len(_sessions)
