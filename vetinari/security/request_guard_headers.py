"""Header parsing helpers for framework-neutral request guards."""

from __future__ import annotations

import string
from typing import Any, Protocol, cast


class RequestConnectionLike(Protocol):
    """Structural request surface needed by guard header helpers."""

    scope: dict[str, Any]
    headers: Any
    client: Any


def _get_any_header(connection: RequestConnectionLike, *names: str) -> str:
    """Return the first matching request header value without exposing secrets."""
    headers = connection.headers
    for name in names:
        try:
            value = headers.get(name) or headers.get(name.lower())
        except (AttributeError, TypeError):
            value = ""
        if value:
            return str(value)

    scope = connection.scope if isinstance(connection.scope, dict) else {}
    for raw_name, raw_value in cast("list[tuple[bytes, bytes]]", scope.get("headers", [])):
        decoded_name = raw_name.decode("latin-1").lower()
        if decoded_name in {name.lower() for name in names}:
            return raw_value.decode("latin-1")
    return ""


def _traceparent_trace_id(traceparent: str) -> str:
    """Extract a W3C trace ID from a traceparent header when possible."""
    parts = traceparent.split("-")
    if len(parts) >= 4 and len(parts[1]) == 32 and all(char in string.hexdigits for char in parts[1]):
        return parts[1]
    return traceparent


def _request_trace_id(connection: RequestConnectionLike) -> str:
    """Return an explicit trace ID or the trace ID portion of traceparent."""
    explicit_trace_id = _get_any_header(connection, "x-trace-id")
    if explicit_trace_id:
        return explicit_trace_id
    traceparent = _get_any_header(connection, "traceparent")
    return _traceparent_trace_id(traceparent) if traceparent else ""


def _auth_log_extra(request_id: str, trace_id: str) -> dict[str, str]:
    """Return structured auth rejection correlation fields for log records."""
    return {"request_id": request_id, "trace_id": trace_id}


def _request_log_context(connection: RequestConnectionLike) -> tuple[str, str, str, str]:
    """Return method, path, request ID, and trace ID for auth rejection logs."""
    scope = connection.scope if isinstance(connection.scope, dict) else {}
    method = str(getattr(connection, "method", None) or scope.get("method") or "UNKNOWN")
    path = str(scope.get("path") or "unknown")
    request_id = _get_any_header(connection, "x-request-id", "request-id") or "missing"
    trace_id = _request_trace_id(connection) or "missing"
    return method, path, request_id, trace_id


def _get_exact_header(connection: RequestConnectionLike, name: str) -> str:
    """Return the value of a header whose name matches exactly."""
    raw_headers = cast("list[tuple[bytes, bytes]]", connection.scope.get("headers", []))
    name_bytes = name.encode("latin-1")
    for raw_name, raw_value in raw_headers:
        if raw_name == name_bytes:
            return raw_value.decode("latin-1")
    return ""
