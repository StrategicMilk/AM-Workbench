"""CSRF protection via custom header validation.

Requires X-Requested-With header on all mutation requests (POST/PUT/DELETE/PATCH).
This prevents cross-origin form submissions since browsers cannot add custom
headers to cross-origin requests without CORS preflight approval.

Decision: custom header CSRF for local-first app (ADR-0071).
"""

from __future__ import annotations

import hmac
import json
import logging
import os
from typing import TYPE_CHECKING

logger = logging.getLogger(__name__)


if TYPE_CHECKING:
    from litestar.types import ASGIApp, Receive, Scope, Send


# Methods that modify state and need CSRF protection
_UNSAFE_METHODS = frozenset({"POST", "PUT", "DELETE", "PATCH"})

# Paths exempt from CSRF check (machine-to-machine or health)
_EXEMPT_PATHS = frozenset({"/health", "/api/v1/a2a"})

CSRF_HEADER = "X-Requested-With"

_ADMIN_HEADER_NAME = b"x-admin-token"
_AUTHORIZATION_HEADER_NAME = b"authorization"
# CSRF alone is not sufficient for these high-impact mutation paths; they also
# require a configured admin token in the same request.
_ADMIN_REQUIRED_MUTATIONS: frozenset[tuple[str, str]] = frozenset({
    ("DELETE", "/api/v1/traces"),
    ("PUT", "/api/v1/settings"),
    ("POST", "/api/v1/browse-directory"),
    ("POST", "/api/v1/validate-path"),
    ("PUT", "/api/v1/preferences"),
})
_ADMIN_REQUIRED_MUTATION_PREFIXES: frozenset[tuple[str, str]] = frozenset({
    ("POST", "/api/v1/dashboard/quality/"),
    ("POST", "/api/v1/memory"),
    ("POST", "/api/v1/project/"),
    ("POST", "/api/v1/skills/"),
    ("POST", "/api/v1/subtasks/"),
    ("POST", "/api/v1/training/experiments/"),
    ("POST", "/api/v1/workflow/gates/"),
    ("PUT", "/api/v1/subtasks/"),
})


def _requires_admin_for_csrf_only(method: str, path: str) -> bool:
    """Return whether CSRF proof alone is insufficient for this mutation."""
    return (method, path) in _ADMIN_REQUIRED_MUTATIONS or any(
        method == prefix_method and path.startswith(prefix)
        for prefix_method, prefix in _ADMIN_REQUIRED_MUTATION_PREFIXES
    )


def _header_value(headers: dict[bytes, bytes], name: bytes) -> str:
    """Decode a request header value from ASGI bytes.

    Args:
        headers: Lowercase ASGI header mapping.
        name: Lowercase header name to read.

    Returns:
        Decoded header value, or an empty string when the header is absent.
    """
    value = headers.get(name)
    if not value:
        return ""
    return value.decode("latin-1").strip()


def _scope_has_admin_token(headers: dict[bytes, bytes]) -> bool:
    """Return whether the request headers satisfy the configured admin token.

    Args:
        headers: Lowercase ASGI header mapping.

    Returns:
        True only when `VETINARI_ADMIN_TOKEN` is configured and either
        `X-Admin-Token` or `Authorization: Bearer` matches it.
    """
    admin_token = os.environ.get("VETINARI_ADMIN_TOKEN", "")
    if not admin_token:
        return False
    provided = _header_value(headers, _ADMIN_HEADER_NAME)
    if not provided:
        auth_header = _header_value(headers, _AUTHORIZATION_HEADER_NAME)
        parts = auth_header.split(" ", 1)
        if len(parts) == 2 and parts[0].lower() == "bearer":
            provided = parts[1].strip()
    if not provided:
        return False
    try:
        return hmac.compare_digest(provided.encode(), admin_token.encode())
    except TypeError:
        logger.exception("CSRF admin comparison failed — denying sensitive mutation until credentials are valid")
        return False


async def _send_json_error(send: Send, status: int, error: str, detail: str) -> None:
    """Send a JSON error response and finish the ASGI response.

    Args:
        send: ASGI send callable.
        status: HTTP response status code.
        error: Short machine-readable error label.
        detail: Human-readable failure detail.
    """
    body = json.dumps({"error": error, "code": error.lower().replace(" ", "_"), "detail": detail}).encode("utf-8")
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode("utf-8")),
        ],
    })
    await send({
        "type": "http.response.body",
        "body": body,
        "more_body": False,
    })


class CSRFMiddleware:
    """ASGI middleware that enforces custom-header CSRF protection.

    For every mutation request (POST, PUT, DELETE, PATCH) the middleware
    checks that the ``X-Requested-With`` header is present and non-empty.
    Browsers cannot attach arbitrary headers to cross-origin requests without
    a CORS preflight, so this header acts as an unforgeable same-origin proof.

    Requests to exempt paths (``/health``, ``/api/v1/a2a``) and safe HTTP
    methods are passed through without inspection.
    """

    def __init__(self, app: ASGIApp) -> None:
        """Store the next ASGI application in the middleware chain.

        Args:
            app: The downstream ASGI application to delegate to.
        """
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Intercept HTTP requests and enforce CSRF header presence.

        Args:
            scope: The ASGI connection scope.
            receive: The ASGI receive callable.
            send: The ASGI send callable.
        """
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        method: str = scope.get("method", "")
        path: str = scope.get("path", "")

        # Safe methods and exempt paths bypass the check
        if method not in _UNSAFE_METHODS or path in _EXEMPT_PATHS:
            await self.app(scope, receive, send)
            return

        # Extract headers; ASGI delivers them as a list of (name, value) byte pairs
        headers: dict[bytes, bytes] = {name.lower(): value for name, value in scope.get("headers", [])}

        csrf_value = headers.get(CSRF_HEADER.lower().encode())
        if csrf_value:
            if _requires_admin_for_csrf_only(method, path) and not _scope_has_admin_token(headers):
                client = scope.get("client")
                client_ip = client[0] if client else "unknown"
                logger.warning(
                    "Admin role check failed: %s %s from %s — valid admin token required",
                    method,
                    path,
                    client_ip,
                )
                await _send_json_error(
                    send,
                    403,
                    "Admin authorization failed",
                    "This mutation requires a valid configured admin token.",
                )
                return
            await self.app(scope, receive, send)
            return

        # Missing or empty header — block with 403
        client = scope.get("client")
        client_ip = client[0] if client else "unknown"
        logger.warning(
            "CSRF check failed: %s %s from %s — missing %s header",
            method,
            path,
            client_ip,
            CSRF_HEADER,
        )

        await _send_json_error(
            send,
            403,
            "CSRF validation failed",
            f"Mutation requests must include the '{CSRF_HEADER}' header.",
        )
