"""Sensitive read guard for Litestar diagnostic and export routes.

This middleware protects GET and HEAD routes that expose logs, traces,
conversation exports, attachments, and system snapshots. Local clients pass
through by default. Remote clients must either provide a configured admin token
or the operator must explicitly opt in to unauthenticated remote reads.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

try:
    from litestar.middleware.base import ASGIMiddleware
    from litestar.types import ASGIApp, Receive, Scope, Send

    _LITESTAR_AVAILABLE = True
except ImportError:
    _LITESTAR_AVAILABLE = False


_READ_METHODS: frozenset[str] = frozenset({"GET", "HEAD"})
_LOCALHOST_IPS: frozenset[str] = frozenset({"127.0.0.1", "::1", "localhost", "testclient", "testclient.local"})
_TRUTHY_ENV_VALUES: frozenset[str] = frozenset({"1", "true", "yes"})
_ADMIN_HEADER_NAME = "x-admin-token"

_PROTECTED_READ_ROUTES: tuple[str, ...] = (
    "/api/projects",
    "/api/logs/stream",
    "/api/logs/recent",
    "/api/v1/chat/export/",
    "/api/v1/chat/attachments/",
    "/api/v1/metrics/latest",
    "/api/v1/metrics/timeseries",
    "/api/v1/traces",
    "/api/v1/status",
    "/api/v1/token-stats",
    "/api/v1/search",
)

RemoteSensitiveReadGuardMiddleware: type[Any] | None


def _is_truthy_env(name: str) -> bool:
    """Return whether an environment flag is enabled.

    Args:
        name: Environment variable name to inspect.

    Returns:
        True when the variable is set to a recognised truthy value.
    """
    return os.environ.get(name, "").strip().lower() in _TRUTHY_ENV_VALUES


def _get_header(scope: Any, name: bytes) -> str:
    """Extract a lowercase HTTP header from an ASGI scope.

    Args:
        scope: ASGI connection scope.
        name: Lowercase header name as bytes.

    Returns:
        Decoded header value, or an empty string when absent.
    """
    for key, value in scope.get("headers", []):
        if key.lower() == name:
            return str(value.decode("latin-1", errors="replace"))
    return ""


def _trusted_proxy_depth() -> int:
    """Return the configured count of trusted proxy hops.

    ``VETINARI_TRUSTED_PROXY`` enables proxy-header support, but headers are
    still ignored until ``VETINARI_TRUSTED_PROXY_DEPTH`` names at least one
    trusted hop. This keeps a direct remote client from spoofing localhost via
    ``X-Forwarded-For``.
    """
    if not _is_truthy_env("VETINARI_TRUSTED_PROXY"):
        return 0
    try:
        return max(0, int(os.environ.get("VETINARI_TRUSTED_PROXY_DEPTH", "0")))
    except ValueError:
        logger.warning("Invalid VETINARI_TRUSTED_PROXY_DEPTH; ignoring proxy headers")
        return 0


def _trusted_proxy_ips() -> set[str]:
    configured = os.environ.get("VETINARI_TRUSTED_PROXY_IPS", "")
    values = {item.strip() for item in configured.split(",") if item.strip()}
    return values or set(_LOCALHOST_IPS)


def resolve_client_ip(scope: Any) -> str:
    """Resolve the client IP for a request without trusting proxy headers by default.

    Args:
        scope: ASGI connection scope.

    Returns:
        The direct client IP, or the trusted forwarded IP when explicitly enabled.
    """
    client_tuple = scope.get("client")
    direct_ip = str(client_tuple[0]) if client_tuple else ""
    depth = _trusted_proxy_depth()
    if depth <= 0:
        return direct_ip
    if direct_ip not in _trusted_proxy_ips():
        logger.warning("Ignoring X-Forwarded-For from untrusted direct peer %s", direct_ip)
        return direct_ip
    forwarded = _get_header(scope, b"x-forwarded-for")
    if not forwarded:
        return direct_ip
    chain = [part.strip() for part in forwarded.split(",") if part.strip()] + [direct_ip]
    if len(chain) <= depth:
        logger.warning("Ignoring X-Forwarded-For with fewer hops than VETINARI_TRUSTED_PROXY_DEPTH")
        return direct_ip
    return chain[-depth - 1]


def _path_is_protected(path: str) -> bool:
    """Return whether a path is covered by the sensitive-read allow-list.

    Args:
        path: Request path from the ASGI scope.

    Returns:
        True when the path exactly matches or is nested under a protected route.
    """
    for route in _PROTECTED_READ_ROUTES:
        if route.endswith("/"):
            if path.startswith(route):
                return True
            continue
        if path == route or path.startswith(f"{route}/"):
            return True
    return False


def _admin_token_matches(scope: Any) -> bool:
    """Return whether the request carries the configured admin token.

    Args:
        scope: ASGI connection scope.

    Returns:
        True when the request token matches the configured value.
    """
    admin_token = os.environ.get("VETINARI_ADMIN_TOKEN", "")
    if not admin_token:
        return False
    provided = _get_header(scope, _ADMIN_HEADER_NAME.encode("latin-1"))
    auth_header = _get_header(scope, b"authorization")
    if not provided and auth_header:
        parts = auth_header.split(" ", 1)
        if len(parts) == 2 and parts[0].lower() == "bearer":
            provided = parts[1].strip()
    return hmac.compare_digest(provided.encode("utf-8"), admin_token.encode("utf-8"))


async def _send_blocked_read(send: Send) -> None:
    """Emit a bounded 401 response for blocked sensitive reads.

    Args:
        send: ASGI send callable.
    """
    body = json.dumps({
        "status_code": 401,
        "detail": (
            "Remote sensitive reads require localhost access or a valid VETINARI_ADMIN_TOKEN. "
            "Set VETINARI_ALLOW_UNAUTHENTICATED_REMOTE_READ=1 to disable this check."
        ),
    }).encode("utf-8")
    await send({
        "type": "http.response.start",
        "status": 401,
        "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode("utf-8")),
            (b"www-authenticate", b'Bearer realm="vetinari"'),
        ],
    })
    await send({"type": "http.response.body", "body": body, "more_body": False})


if _LITESTAR_AVAILABLE:

    class RemoteSensitiveReadGuardMiddleware(ASGIMiddleware):
        """Block remote reads of sensitive diagnostic and export routes.

        The guard is fail-closed for remote clients when no admin token is
        configured. Localhost remains usable for the desktop-first workflow.
        Remote operators can either configure and send ``VETINARI_ADMIN_TOKEN``
        or explicitly opt in with ``VETINARI_ALLOW_UNAUTHENTICATED_REMOTE_READ``.
        """

        async def handle(self, scope: Scope, receive: Receive, send: Send, next_app: ASGIApp) -> None:
            """Enforce the sensitive-read remote access policy.

            Args:
                scope: ASGI connection scope for the current request.
                receive: ASGI receive callable.
                send: ASGI send callable.
                next_app: Next ASGI application in the middleware stack.
            """
            if scope["type"] != "http":
                await next_app(scope, receive, send)
                return

            method = str(scope.get("method", ""))
            path = str(scope.get("path", ""))
            if method not in _READ_METHODS or not _path_is_protected(path):
                await next_app(scope, receive, send)
                return

            remote_ip = resolve_client_ip(scope)
            if remote_ip in _LOCALHOST_IPS:
                await next_app(scope, receive, send)
                return

            if _admin_token_matches(scope):
                await next_app(scope, receive, send)
                return

            # Operator has explicitly opted in via VETINARI_ALLOW_UNAUTHENTICATED_REMOTE_READ=1.
            if _is_truthy_env("VETINARI_ALLOW_UNAUTHENTICATED_REMOTE_READ"):
                await next_app(scope, receive, send)
                return

            logger.warning(
                "RemoteSensitiveReadGuard: blocked %s %s from remote client without admin token",
                method,
                path,
            )
            await _send_blocked_read(send)

else:
    RemoteSensitiveReadGuardMiddleware = None


__all__ = ["_PROTECTED_READ_ROUTES", "RemoteSensitiveReadGuardMiddleware", "resolve_client_ip"]
