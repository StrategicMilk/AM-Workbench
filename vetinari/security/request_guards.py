"""Request security predicates for Vetinari admin and local-user access.

The Rust kernel owns the live HTTP route surface. These helpers remain as
framework-neutral request predicates for tests and Python runtime callers that
need the same token and loopback checks.
"""

from __future__ import annotations

import hmac
import logging
import os
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass
from importlib.util import find_spec
from typing import Any, Protocol, cast

from vetinari.security.request_guard_headers import _auth_log_extra, _request_log_context
from vetinari.web.sensitive_read_guard import resolve_client_ip

logger = logging.getLogger(__name__)


if find_spec("litestar.exceptions") is not None:
    from litestar.exceptions import NotAuthorizedException, TooManyRequestsException
else:  # pragma: no cover - Litestar-free public/runtime probes use local exception types.

    class NotAuthorizedException(Exception):
        """Raised when a request fails authorization."""

    class TooManyRequestsException(Exception):
        """Raised when invalid credential attempts exceed the endpoint limit."""


BaseRouteHandler = Any


class RequestConnection(Protocol):
    """Structural request surface needed by the guard predicates."""

    scope: dict[str, Any]
    headers: Any
    client: Any


# Loopback addresses are retained for middleware compatibility checks. The
# admin guard itself requires an explicit token and no longer grants localhost
# admin access when VETINARI_ADMIN_TOKEN is unset.
_LOCALHOST_IPS: frozenset[str] = frozenset({"127.0.0.1", "::1", "localhost", "testclient", "testclient.local"})

# Primary admin token header name. Authorization: Bearer <token> is accepted as a fallback.
_ADMIN_HEADER_NAME = "X-Admin-Token"

# Optional secondary API token header. When VETINARI_API_TOKEN is configured the
# local-user guard requires this header on EVERY request, including loopback,
# so a misconfigured host that exposes the loopback interface to a network
# cannot leak local-first routes. See test_api_token_enforcement.py.
_API_TOKEN_HEADER_NAME = "X-Vetinari-Api-Token"  # noqa: S105 - header name, not a credential value

# App-state key populated by create_app(). Route guards read this stable startup
# value instead of re-reading the process environment on every request.
_ADMIN_STATE_ATTR = "vetinari_admin_value"
_CACHED_ENV_ADMIN_TOKEN: str = os.environ.get("VETINARI_ADMIN_TOKEN", "")
_DEFAULT_ADMIN_AUTH_FAILURE_LIMIT = 5
_ADMIN_AUTH_FAILURE_WINDOW_SECONDS = 60.0
_ADMIN_AUTH_FAILURES: dict[tuple[str, str, str], tuple[int, float]] = {}
_ADMIN_AUTH_FAILURE_LOCK = threading.Lock()


@dataclass(frozen=True, slots=True)
class PrivacyTrustEvidenceViolation:
    """Fail-closed privacy-trust release evidence violation."""

    path: str
    detail: str


def check_privacy_trust_release_evidence(
    root: str | os.PathLike[str] = ".",
) -> tuple[PrivacyTrustEvidenceViolation, ...]:
    """Validate RCG-0069-P02 privacy-trust evidence with fail-closed semantics.

    This is a runtime-consumable guard for release and documentation callers that
    need to prove privacy/compliance prose is backed by source rows, tests, and
    terminal closure. Missing, stale, unreadable, or contradictory evidence is
    returned as a violation instead of being treated as release-safe.

    Args:
        root: Repository root containing the required source, test, doc, and
            closure-evidence paths.

    Returns:
        Tuple of evidence violations. An empty tuple means all required
        privacy-trust evidence is present and consistent.

    Raises:
        ModuleNotFoundError: If the repository-private validator is present but
            one of its own dependencies is unexpectedly unavailable.
    """
    if find_spec("vetinari.security._privacy_trust_release_evidence") is not None:
        from vetinari.security._privacy_trust_release_evidence import (
            check_privacy_trust_release_evidence as _check_privacy_trust_release_evidence,
        )

        return _check_privacy_trust_release_evidence(root)
    logger.warning("Repository-private privacy-trust evidence validator is unavailable")
    return (
        PrivacyTrustEvidenceViolation(
            "privacy-trust-release-evidence",
            "repository-private privacy-trust evidence validator is unavailable",
        ),
    )


def _get_exact_header(connection: RequestConnection, name: str) -> str:
    """Return the value of a header whose name matches exactly (case-sensitive).

    ASGI frameworks commonly normalize stored header names to lowercase, so this function
    iterates the raw scope headers to find the first entry whose name matches
    ``name`` byte-for-byte after UTF-8 decoding.  Returns empty string when
    no exact match exists.

    This enforces strict header-name casing so that ``x-admin-token`` is NOT
    treated as equivalent to ``X-Admin-Token`` â€” closing the lowercase-alias
    auth bypass vector.

    Args:
        connection: The active ASGI connection carrying the incoming request.
        name: The exact header name to look up (case-sensitive).

    Returns:
        The header value string, or empty string when absent.
    """
    # ASGI scope["headers"] is a list of (name_bytes, value_bytes) tuples
    # with the original casing from the HTTP client preserved.
    raw_headers = cast("list[tuple[bytes, bytes]]", connection.scope.get("headers", []))
    name_bytes = name.encode("latin-1")
    for raw_name, raw_value in raw_headers:
        if raw_name == name_bytes:
            return raw_value.decode("latin-1")
    return ""


def _configured_admin_auth_failure_limit() -> int:
    """Return the per-endpoint invalid-token limit for admin guarded routes."""
    raw = os.environ.get("VETINARI_ADMIN_AUTH_FAILURE_LIMIT", "").strip()
    if not raw:
        return _DEFAULT_ADMIN_AUTH_FAILURE_LIMIT
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "Invalid VETINARI_ADMIN_AUTH_FAILURE_LIMIT=%r - using default %d",
            raw,
            _DEFAULT_ADMIN_AUTH_FAILURE_LIMIT,
        )
        return _DEFAULT_ADMIN_AUTH_FAILURE_LIMIT
    if value <= 0:
        logger.warning(
            "Non-positive VETINARI_ADMIN_AUTH_FAILURE_LIMIT=%d - using default %d",
            value,
            _DEFAULT_ADMIN_AUTH_FAILURE_LIMIT,
        )
        return _DEFAULT_ADMIN_AUTH_FAILURE_LIMIT
    return value


def reset_admin_auth_failures() -> None:
    """Clear in-process admin-auth failure buckets for deterministic tests."""
    with _ADMIN_AUTH_FAILURE_LOCK:
        _ADMIN_AUTH_FAILURES.clear()


def _provided_admin_token(connection: RequestConnection) -> str:
    """Return the explicit admin credential supplied by a request, if any."""
    req_token = connection.headers.get(_ADMIN_HEADER_NAME.lower(), "") or connection.headers.get(_ADMIN_HEADER_NAME, "")
    auth_header = connection.headers.get("authorization", "") or connection.headers.get("Authorization", "")
    if req_token:
        return str(req_token)
    if auth_header:
        parts = str(auth_header).split(" ", 1)
        if len(parts) == 2 and parts[0].lower() == "bearer":
            return parts[1].strip()
    return ""


def _admin_credential_supplied(connection: RequestConnection) -> bool:
    """Return whether the request explicitly carried an admin credential header."""
    raw_headers = cast("list[tuple[bytes, bytes]]", connection.scope.get("headers", []))
    credential_header_names = {b"x-admin-token", b"x-admin-tok", b"authorization"}
    if any(raw_name.lower() in credential_header_names for raw_name, _raw_value in raw_headers):
        return True
    header_keys = getattr(connection.headers, "keys", None)
    if callable(header_keys):
        try:
            keys = cast("Iterable[Any]", header_keys())
            return any(str(key).lower() in {"x-admin-token", "x-admin-tok", "authorization"} for key in keys)
        except Exception:
            logger.warning("Could not inspect admin credential header keys", exc_info=True)
            return False
    return False


def _admin_auth_failure_key(connection: RequestConnection) -> tuple[str, str, str]:
    """Return a client and endpoint key for invalid admin-token attempts."""
    scope = connection.scope
    try:
        client_host = resolve_client_ip(scope)
    except Exception:
        client = connection.client
        client_host = client.host if client else "unknown"
    method = str(scope.get("method", "UNKNOWN")).upper()
    path = str(scope.get("path", ""))
    return (client_host, method, path)


def _admin_auth_is_limited(connection: RequestConnection) -> bool:
    """Return whether invalid admin-token attempts exhausted this endpoint bucket."""
    limit = _configured_admin_auth_failure_limit()
    now = time.monotonic()
    key = _admin_auth_failure_key(connection)
    with _ADMIN_AUTH_FAILURE_LOCK:
        current = _ADMIN_AUTH_FAILURES.get(key)
        if current is None:
            return False
        count, expires_at = current
        if expires_at <= now:
            _ADMIN_AUTH_FAILURES.pop(key, None)
            return False
        return count >= limit


def _record_admin_auth_failure(connection: RequestConnection) -> None:
    """Record one invalid explicit admin-token attempt for this endpoint."""
    now = time.monotonic()
    expires_at = now + _ADMIN_AUTH_FAILURE_WINDOW_SECONDS
    key = _admin_auth_failure_key(connection)
    with _ADMIN_AUTH_FAILURE_LOCK:
        current = _ADMIN_AUTH_FAILURES.get(key)
        if current is None or current[1] <= now:
            _ADMIN_AUTH_FAILURES[key] = (1, expires_at)
            return
        count, existing_expires_at = current
        _ADMIN_AUTH_FAILURES[key] = (count + 1, existing_expires_at)


def _clear_admin_auth_failure(connection: RequestConnection) -> None:
    """Clear the failure bucket after a valid admin credential is presented."""
    key = _admin_auth_failure_key(connection)
    with _ADMIN_AUTH_FAILURE_LOCK:
        _ADMIN_AUTH_FAILURES.pop(key, None)


def configure_admin_token(app: Any, token: str | None = None) -> None:
    """Store the configured admin token on an application state object.

    Args:
        app: Application or compatible object exposing ``state``.
        token: Explicit token value. When omitted, ``VETINARI_ADMIN_TOKEN`` is
            read once during app construction.
    """
    state = getattr(app, "state", None)
    if state is None:
        return
    setattr(state, _ADMIN_STATE_ATTR, token if token is not None else os.environ.get("VETINARI_ADMIN_TOKEN", ""))


def _set_cached_admin_token(value: str) -> None:
    """Set the fallback admin token cache for direct-call tests."""
    global _CACHED_ENV_ADMIN_TOKEN
    _CACHED_ENV_ADMIN_TOKEN = value


def _configured_admin_token(connection: RequestConnection) -> str:
    """Return the startup-cached admin token for an app-bound connection.

    Test doubles and direct guard use can omit ``connection.app``; those calls
    fall back to the module-level startup cache for compatibility with focused
    unit tests.
    """
    app = getattr(connection, "app", None)
    state = getattr(app, "state", None)
    state_values = getattr(state, "_state", None)
    if isinstance(state_values, dict) and _ADMIN_STATE_ATTR in state_values:
        value = state_values[_ADMIN_STATE_ATTR]
        return value if isinstance(value, str) else ""
    return _CACHED_ENV_ADMIN_TOKEN


def is_admin_connection(connection: RequestConnection) -> bool:
    """Return True if the connection originates from an authorised admin.

    Accepts the ``X-Admin-Token`` header (case-insensitive per RFC 7230)
    carrying the value configured in ``VETINARI_ADMIN_TOKEN``.  Falls back to
    ``Authorization: Bearer <token>`` when ``X-Admin-Token`` is absent.

    Uses constant-time comparison (``hmac.compare_digest``) to prevent
    timing-based token oracle attacks (P1.C1/P1.H10).

    HTTP header names are case-insensitive (RFC 7230 Â§3.2).  Using
    ``connection.headers.get()`` on a case-insensitive header mapping ensures
    compatibility with HTTP clients that normalise header names to lowercase
    (e.g. httpx, h2).

    When no token is set, returns False. The remote-access middleware family
    handles localhost-only development exposure separately; route-level admin
    authorization must fail closed without an operator-configured secret.

    Args:
        connection: The active ASGI connection carrying the incoming request.

    Returns:
        True when the request is authorised as admin, False otherwise.
    """
    admin_token = _configured_admin_token(connection)
    if admin_token:
        # Prefer X-Admin-Token; fall back to Authorization: Bearer <token>.
        # Header name lookup is case-insensitive per RFC 7230 Â§3.2.
        # Try lowercase first (real HTTP clients â€” httpx/h2 normalise header names).
        # Fall back to exact-case for unit test mocks that use plain dicts (case-sensitive).
        req_token = connection.headers.get(_ADMIN_HEADER_NAME.lower(), "") or connection.headers.get(
            _ADMIN_HEADER_NAME, ""
        )
        auth_header = connection.headers.get("authorization", "") or connection.headers.get("Authorization", "")
        # RFC 7235 Â§5.1.2: auth-scheme tokens are case-insensitive. Accept "Bearer",
        # "bearer", "BEARER", and mixed case. The scheme must still be separated
        # from the credential by a single space.
        bearer = ""
        if auth_header:
            parts = auth_header.split(" ", 1)
            if len(parts) == 2 and parts[0].lower() == "bearer":
                bearer = parts[1].strip()
        provided = req_token or bearer
        # hmac.compare_digest requires both operands to be the same type
        try:
            return hmac.compare_digest(provided.encode(), admin_token.encode())
        except (AttributeError, TypeError, UnicodeEncodeError, ValueError):
            # If comparison fails for any reason, fail CLOSED (anti-pattern: fail-open security)
            logger.exception("Admin token comparison failed unexpectedly â€” denying route-level admin access")
            return False

    logger.warning("Admin token is not configured â€” denying route-level admin access")
    return False


def admin_guard(connection: RequestConnection, _: BaseRouteHandler) -> None:
    """Request guard that enforces admin access on a route handler.

    Raises ``NotAuthorizedException`` when the caller is not an admin,
    which route adapters translate to a 401 response.

    Replicates the legacy ``require_admin`` decorator logic for request guards.

    Args:
        connection: The active ASGI connection for the incoming request.
        _: The route handler (unused by this guard protocol).

    Raises:
        NotAuthorizedException: When the caller is not an authorised admin.
    """
    provided = _provided_admin_token(connection)
    if provided and _admin_auth_is_limited(connection):
        raise TooManyRequestsException("Too many invalid admin-token attempts for this endpoint")
    if is_admin_connection(connection):
        if provided:
            _clear_admin_auth_failure(connection)
        return
    if provided:
        _record_admin_auth_failure(connection)
    method, path, request_id, trace_id = _request_log_context(connection)
    host = connection.client.host if connection.client else "unknown"
    logger.warning(
        "Admin guard rejected %s request to %s from %s request_id=%s trace_id=%s",
        method,
        path,
        host,
        request_id,
        trace_id,
        extra=_auth_log_extra(request_id, trace_id),
    )
    raise NotAuthorizedException("Admin privileges required")


def is_local_user_connection(connection: RequestConnection) -> bool:
    """Return True for loopback callers or token-authorized remote callers.

    Vetinari is a local-first single-user tool, so ADR-0098 /
    DEEP-DISCOVERY Option B permits localhost, IPv6 loopback, and in-process
    callers to access user-facing lifecycle and workbench routes
    without an admin token. Non-localhost callers delegate to
    ``is_admin_connection()`` and therefore use the same HMAC token check as
    admin routes. Any unexpected error fails closed by logging and returning
    ``False``.

    Args:
        connection: The active ASGI connection carrying the incoming request.

    Returns:
        True for local callers or remote callers with a valid admin token;
        False for missing credentials, invalid credentials, or any exception.
    """
    try:
        from vetinari.web.sensitive_read_guard import resolve_client_ip

        client = connection.client
        if client is None:
            return False
        client_host = resolve_client_ip(connection.scope)
        if client_host in _LOCALHOST_IPS:
            return True
        return is_admin_connection(connection)
    except Exception:
        logger.exception("Local-user guard predicate failed unexpectedly - denying access")
        return False


def _api_token_check(connection: RequestConnection) -> bool | None:
    """Return None when API-token enforcement is OFF, else True/False for match.

    When ``VETINARI_API_TOKEN`` is set, ``local_user_guard`` requires every
    caller (including loopback) to supply a matching ``X-Vetinari-Api-Token``
    header. Comparison uses ``hmac.compare_digest``; any exception raised by
    the comparator fails closed (returns False), never True.
    """
    configured = os.environ.get("VETINARI_API_TOKEN", "")
    if not configured:
        return None
    supplied = connection.headers.get(_API_TOKEN_HEADER_NAME.lower(), "") or connection.headers.get(
        _API_TOKEN_HEADER_NAME, ""
    )
    if not supplied:
        return False
    try:
        return bool(hmac.compare_digest(str(supplied).encode("utf-8"), configured.encode("utf-8")))
    except Exception:
        logger.exception("API-token compare_digest raised â€” failing closed")
        return False


def local_user_guard(connection: RequestConnection, _: BaseRouteHandler) -> None:
    """Request guard for local-first user-facing routes.

    Loopback callers pass automatically when ``VETINARI_API_TOKEN`` is unset; otherwise
    every caller must supply a matching ``X-Vetinari-Api-Token`` header.
    Non-localhost callers without an API token must still provide the same
    valid HMAC token accepted by ``admin_guard``. First-run recovery for remote
    local-user access must surface or persist the operator token via the serve
    startup path; this guard fails closed and names ``VETINARI_ADMIN_TOKEN``
    in the rejection detail rather than silently bypassing auth.

    Args:
        connection: The active ASGI connection for the incoming request.
        _: The route handler (unused by this guard protocol).

    Raises:
        NotAuthorizedException: When the caller is neither local nor token
            authorized.
    """
    api_token_result = _api_token_check(connection)
    if api_token_result is False:
        method, path, request_id, trace_id = _request_log_context(connection)
        logger.warning(
            "Local-user guard rejected %s request to %s: missing or invalid X-Vetinari-Api-Token "
            "request_id=%s trace_id=%s",
            method,
            path,
            request_id,
            trace_id,
            extra=_auth_log_extra(request_id, trace_id),
        )
        raise NotAuthorizedException("Authorization required; supplied X-Vetinari-Api-Token is missing or invalid")
    if api_token_result is True:
        return

    credential_supplied = _admin_credential_supplied(connection)
    if credential_supplied and _admin_auth_is_limited(connection):
        raise TooManyRequestsException("Too many invalid admin-token attempts for this endpoint")

    if credential_supplied:
        if is_admin_connection(connection):
            _clear_admin_auth_failure(connection)
            return
        _record_admin_auth_failure(connection)
        try:
            from vetinari.web.sensitive_read_guard import resolve_client_ip

            host = resolve_client_ip(connection.scope)
        except Exception:
            client = connection.client
            host = client.host if client else "unknown"
        method, path, request_id, trace_id = _request_log_context(connection)
        logger.warning(
            "Local-user guard rejected %s request to %s from %s with invalid explicit token request_id=%s trace_id=%s",
            method,
            path,
            host,
            request_id,
            trace_id,
            extra=_auth_log_extra(request_id, trace_id),
        )
        raise NotAuthorizedException("Authorization required; supplied admin token is invalid")

    if is_local_user_connection(connection):
        return

    try:
        from vetinari.web.sensitive_read_guard import resolve_client_ip

        host = resolve_client_ip(connection.scope)
    except Exception:
        client = connection.client
        host = client.host if client else "unknown"
    method, path, request_id, trace_id = _request_log_context(connection)
    logger.warning(
        "Local-user guard rejected %s request to %s from %s request_id=%s trace_id=%s",
        method,
        path,
        host,
        request_id,
        trace_id,
        extra=_auth_log_extra(request_id, trace_id),
    )
    raise NotAuthorizedException("Authorization required; set VETINARI_ADMIN_TOKEN for remote access")
