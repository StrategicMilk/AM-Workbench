"""Remote request rate limiting for the Litestar web entry point.

This middleware applies a small per-client token bucket to remote requests.
Loopback clients are exempt so the desktop and local test workflow remain
unthrottled, while repeated remote bursts receive a bounded 429 response.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from vetinari.web.sensitive_read_guard import resolve_client_ip

logger = logging.getLogger(__name__)

try:
    from litestar.middleware.base import ASGIMiddleware
    from litestar.types import ASGIApp, Receive, Scope, Send

    _LITESTAR_AVAILABLE = True
except ImportError:
    _LITESTAR_AVAILABLE = False


_DEFAULT_RATE_LIMIT_PER_MINUTE = 60
_DEFAULT_WINDOW_SECONDS = 60.0
DEFAULT_RATE_LIMIT_PER_MINUTE = _DEFAULT_RATE_LIMIT_PER_MINUTE
RATE_LIMIT_WINDOW_SECONDS = _DEFAULT_WINDOW_SECONDS
_LOCALHOST_IPS: frozenset[str] = frozenset({"127.0.0.1", "::1", "localhost", "testclient"})
_TRUTHY_ENV_VALUES: frozenset[str] = frozenset({"1", "true", "yes"})
_MAX_BUCKETS = 4096

RateLimitMiddleware: type[Any] | None


@dataclass(slots=True)
class _RemoteRateBucket:
    """Mutable token bucket state for one remote client."""

    tokens: float
    updated_at: float


def _is_truthy_env(name: str) -> bool:
    """Return whether an environment flag is enabled.

    Args:
        name: Environment variable name to inspect.

    Returns:
        True when the value is one of the recognised truthy strings.
    """
    return os.environ.get(name, "").strip().lower() in _TRUTHY_ENV_VALUES


def _configured_limit() -> int:
    """Read the configured remote request limit.

    Returns:
        Positive request count per minute, falling back to the default when
        the environment value is absent or invalid.
    """
    raw = os.environ.get("VETINARI_RATE_LIMIT_PER_MINUTE", "").strip()
    if not raw:
        return _DEFAULT_RATE_LIMIT_PER_MINUTE
    try:
        configured = int(raw)
    except ValueError:
        logger.warning(
            "Invalid VETINARI_RATE_LIMIT_PER_MINUTE=%r - using default %d",
            raw,
            _DEFAULT_RATE_LIMIT_PER_MINUTE,
        )
        return _DEFAULT_RATE_LIMIT_PER_MINUTE
    if configured <= 0:
        logger.warning(
            "Non-positive VETINARI_RATE_LIMIT_PER_MINUTE=%d - using default %d",
            configured,
            _DEFAULT_RATE_LIMIT_PER_MINUTE,
        )
        return _DEFAULT_RATE_LIMIT_PER_MINUTE
    return configured


async def _send_rate_limited(send: Send) -> None:
    """Emit a bounded 429 response.

    Args:
        send: ASGI send callable.
    """
    body = json.dumps({
        "status_code": 429,
        "detail": "Too many requests from this remote client. Try again later.",
    }).encode("utf-8")
    await send({
        "type": "http.response.start",
        "status": 429,
        "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode("utf-8")),
            (b"retry-after", b"60"),
        ],
    })
    await send({"type": "http.response.body", "body": body, "more_body": False})


if _LITESTAR_AVAILABLE:

    class RateLimitMiddleware(ASGIMiddleware):
        """Apply a per-remote-client token bucket before route handling.

        The bucket state is guarded by a lock because the middleware instance is
        shared by all Litestar workers in this process. Localhost clients bypass
        the limiter to preserve the local-first application workflow.
        """

        def __init__(
            self,
            per_minute: int | None = None,
            window_seconds: float = _DEFAULT_WINDOW_SECONDS,
            time_source: Callable[[], float] | None = None,
        ) -> None:
            """Configure token bucket parameters for this middleware instance.

            Args:
                per_minute: Maximum remote requests per window. ``None`` reads
                    ``VETINARI_RATE_LIMIT_PER_MINUTE``.
                window_seconds: Refill window length in seconds.
                time_source: Clock used by tests to advance time deterministically.
            """
            configured_capacity = per_minute if per_minute is not None else _configured_limit()
            self._capacity = max(1, configured_capacity)
            self._window_seconds = window_seconds
            self._tokens_per_second = self._capacity / window_seconds
            self._time_source = time_source or time.monotonic
            self._lock = threading.Lock()
            self._buckets: dict[str, _RemoteRateBucket] = {}

        async def handle(self, scope: Scope, receive: Receive, send: Send, next_app: ASGIApp) -> None:
            """Reject remote bursts that exceed the configured token bucket.

            Args:
                scope: ASGI connection scope for the current request.
                receive: ASGI receive callable.
                send: ASGI send callable.
                next_app: Next ASGI application in the middleware stack.
            """
            if scope["type"] != "http":
                await next_app(scope, receive, send)
                return

            remote_ip = resolve_client_ip(scope)
            if remote_ip in _LOCALHOST_IPS:
                await next_app(scope, receive, send)
                return

            # Operator has explicitly opted in via VETINARI_DISABLE_RATE_LIMIT=1.
            if _is_truthy_env("VETINARI_DISABLE_RATE_LIMIT"):
                await next_app(scope, receive, send)
                return

            if self._consume_token(remote_ip):
                await next_app(scope, receive, send)
                return

            logger.warning("RateLimitMiddleware: blocked remote client after token bucket exhaustion")
            await _send_rate_limited(send)

        def _consume_token(self, remote_ip: str) -> bool:
            """Consume one token for a remote client.

            Args:
                remote_ip: Rate-limit key for the request.

            Returns:
                True when the request is allowed, False when the bucket is empty.
            """
            now = self._time_source()
            with self._lock:
                bucket = self._buckets.get(remote_ip)
                if bucket is None:
                    self._buckets[remote_ip] = _RemoteRateBucket(tokens=self._capacity - 1, updated_at=now)
                    self._prune_locked(now)
                    return True

                elapsed = max(0.0, now - bucket.updated_at)
                bucket.tokens = min(float(self._capacity), bucket.tokens + (elapsed * self._tokens_per_second))
                bucket.updated_at = now
                if bucket.tokens < 1.0:
                    self._prune_locked(now)
                    return False
                bucket.tokens -= 1.0
                self._prune_locked(now)
                return True

        def _prune_locked(self, now: float) -> None:
            """Remove long-idle buckets while the caller holds ``self._lock``.

            Args:
                now: Current monotonic time.
            """
            if len(self._buckets) <= _MAX_BUCKETS:
                return
            stale_before = now - (self._window_seconds * 2)
            stale_keys = [key for key, bucket in self._buckets.items() if bucket.updated_at < stale_before]
            for key in stale_keys:
                self._buckets.pop(key, None)

else:
    RateLimitMiddleware = None


__all__ = ["DEFAULT_RATE_LIMIT_PER_MINUTE", "RATE_LIMIT_WINDOW_SECONDS", "RateLimitMiddleware"]
