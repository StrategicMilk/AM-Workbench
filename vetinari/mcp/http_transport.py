"""MCP HTTP/SSE transport support retained outside the Python web route layer."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import AsyncIterator
from typing import Any

from vetinari.mcp.resources import McpResource, McpResourceRegistry, McpResourceSession, sse_event

logger = logging.getLogger(__name__)

# Server-owned resource registry populated explicitly by MCP transport handlers.
# Runtime tests may add sessions, but resource definitions stay local.
_RESOURCE_REGISTRY = McpResourceRegistry()
_RESOURCE_REGISTRY_INITIALIZED = False


def initialize_transport() -> None:
    """Register static MCP HTTP resources exactly once."""
    global _RESOURCE_REGISTRY_INITIALIZED
    if _RESOURCE_REGISTRY_INITIALIZED:
        return
    _RESOURCE_REGISTRY.register(
        McpResource(
            uri="resource://workspace/context",
            title="Workspace context",
            payload="local workspace context",
            required_permission="resource:workspace",
        )
    )
    _RESOURCE_REGISTRY_INITIALIZED = True


# Server-owned resource sessions keyed by trusted session ID. Route handlers
# create/read entries; per-session locks guard bounded event queues.
_RESOURCE_SESSIONS: dict[str, McpResourceSession] = {}
_RESOURCE_STREAM_POLL_SECONDS = 5.0
_RESOURCE_STREAM_MAX_IDLE_CYCLES = 120
_SSE_ACCEPT_VALUE = "text/event-stream"
_ACCEPT_ANY_VALUE = "*/*"
_SAFE_RESOURCE_SESSION_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_TRUSTED_INITIALIZE_RESOURCE_PERMISSIONS = frozenset({"resource:workspace"})


def _error_payload(message: str, code: int) -> dict[str, Any]:
    return {"status": "error", "error": message, "code": code}


def initialize_mcp_resource_session(
    session_id: str,
    *,
    permissions: set[str] | frozenset[str] | None = None,
    max_events: int = 16,
) -> McpResourceSession:
    """Create an initialized bounded SSE resource session.

    Args:
        session_id: Server-side session identifier.
        permissions: Permissions granted by the trusted session initializer.
        max_events: Maximum queued SSE events retained for the session.

    Returns:
        Initialized resource session stored by session ID.
    """
    session = McpResourceSession(
        session_id=session_id,
        initialized=True,
        permissions=frozenset(permissions or ()),
        max_events=max_events,
    )
    _RESOURCE_SESSIONS[session_id] = session
    return session


def cleanup_mcp_resource_session(session_id: str) -> int:
    """Disconnect and remove an MCP resource session queue.

    Args:
        session_id: Server-side session identifier.

    Returns:
        Number of queued events removed.
    """
    session = _RESOURCE_SESSIONS.pop(session_id, None)
    return 0 if session is None else session.cleanup()


def handle_mcp_resources_list(session_id: str, *, correlation_id: str) -> tuple[int, str]:
    """Return an SSE resources/list event or a fail-closed support envelope.

    Args:
        session_id: Server-side session identifier.
        correlation_id: Caller-visible request correlation ID.

    Returns:
        HTTP status code and SSE body.
    """
    initialize_transport()
    session = _RESOURCE_SESSIONS.get(session_id, McpResourceSession(session_id=session_id, initialized=False))
    resources, support = _RESOURCE_REGISTRY.list_resources(session)
    if support is not None:
        return 409, sse_event("error", {"support": support.to_dict()}, correlation_id=correlation_id)
    return 200, sse_event("resources/list", {"resources": resources}, correlation_id=correlation_id)


def handle_mcp_resources_read(
    session_id: str,
    uri: str,
    *,
    correlation_id: str,
) -> tuple[int, str]:
    """Return an SSE resources/read event or a fail-closed support envelope.

    Args:
        session_id: Server-side session identifier.
        uri: MCP resource URI to read.
        correlation_id: Caller-visible request correlation ID.

    Returns:
        HTTP status code and SSE body.
    """
    initialize_transport()
    session = _RESOURCE_SESSIONS.get(session_id, McpResourceSession(session_id=session_id, initialized=False))
    payload, support = _RESOURCE_REGISTRY.read_resource(session, uri)
    if support is not None:
        return 409, sse_event("error", {"support": support.to_dict()}, correlation_id=correlation_id)
    privacy, privacy_support = _RESOURCE_REGISTRY.resource_privacy_receipt(session, uri)
    if privacy_support is not None:
        return 409, sse_event("error", {"support": privacy_support.to_dict()}, correlation_id=correlation_id)
    event = sse_event(
        "resources/read", {"uri": uri, "payload": payload, "privacy": privacy}, correlation_id=correlation_id
    )
    overflow = session.enqueue(event)
    if overflow is not None:
        return 413, sse_event("error", {"support": overflow.to_dict()}, correlation_id=correlation_id)
    stream_message, stream_overflow = session.enqueue_stream_event(
        event_type="resources/read",
        uri=uri,
        payload=payload or "",
        correlation_id=correlation_id,
    )
    if stream_overflow is not None:
        return 413, sse_event("error", {"support": stream_overflow.to_dict()}, correlation_id=correlation_id)
    if stream_message is None:
        return 409, sse_event(
            "error",
            {
                "support": {
                    "code": "MCP_STREAM_EVENT",
                    "message": "MCP resource stream event could not be queued",
                    "recovery": "reinitialize the resource stream session",
                }
            },
            correlation_id=correlation_id,
        )
    return 200, event


async def stream_mcp_resource_events(
    session_id: str,
    uri: str,
    *,
    correlation_id: str,
    last_event_id: int = 0,
    poll_interval_seconds: float = _RESOURCE_STREAM_POLL_SECONDS,
    max_idle_cycles: int = _RESOURCE_STREAM_MAX_IDLE_CYCLES,
) -> AsyncIterator[dict[str, str]]:
    """Stream MCP resource events for one initialized HTTP+SSE session.

    Args:
        session_id: Server-owned session identifier.
        uri: MCP resource URI to subscribe to.
        correlation_id: Caller-visible correlation identifier.
        last_event_id: Last SSE event ID observed by a reconnecting client.
        poll_interval_seconds: Delay between queue polls.
        max_idle_cycles: Maximum keepalive-only cycles before the stream ends.

    Yields:
        SSE message dictionaries with ``event``/``data`` fields.
    """
    initialize_transport()
    session = _RESOURCE_SESSIONS.get(session_id, McpResourceSession(session_id=session_id, initialized=False))
    _payload, support = _RESOURCE_REGISTRY.read_resource(session, uri)
    if support is not None:
        yield _support_sse_message(support.to_dict(), correlation_id=correlation_id, status="error")
        return

    current_event_id = max(0, last_event_id)
    idle_cycles = 0
    yield {
        "data": json.dumps({"correlation_id": correlation_id, "uri": uri}, sort_keys=True),
        "event": "resources/subscribed",
    }
    try:
        while idle_cycles < max_idle_cycles:
            next_event = session.next_stream_event(uri, after_event_id=current_event_id)
            if next_event is None:
                idle_cycles += 1
                yield {"comment": "keepalive"}
                await asyncio.sleep(poll_interval_seconds)
                continue
            idle_cycles = 0
            current_event_id = _parse_event_id(next_event.get("id", ""), current_event_id)
            yield next_event
    finally:
        logger.debug(
            "MCP resource SSE stream closed for session_id=%s uri=%s last_event_id=%s",
            session_id,
            uri,
            current_event_id,
        )


def _support_sse_message(
    support: dict[str, str],
    *,
    correlation_id: str,
    status: str,
) -> dict[str, str]:
    payload = {"correlation_id": correlation_id, "status": status, "support": support}
    return {"data": json.dumps(payload, sort_keys=True), "event": "error"}


def _supports_sse_accept(request: Any) -> bool:
    accept = str(request.headers.get("accept", "") or request.headers.get("Accept", ""))
    if not accept:
        return True
    accepted = {part.split(";", 1)[0].strip().lower() for part in accept.split(",")}
    return _SSE_ACCEPT_VALUE in accepted or _ACCEPT_ANY_VALUE in accepted


def _parse_event_id(raw_event_id: str, fallback: int = 0) -> int:
    try:
        parsed = int(raw_event_id)
    except (TypeError, ValueError):
        logger.debug("Invalid MCP SSE event id; using fallback", extra={"raw_event_id": raw_event_id})
        return fallback
    return max(0, parsed)


def _unsupported_transport_body(correlation_id: str) -> str:
    return sse_event(
        "error",
        {
            "support": {
                "code": "MCP_TRANSPORT",
                "message": "MCP resource streams require Accept: text/event-stream",
                "recovery": "open the resource stream with an SSE-capable client",
            }
        },
        correlation_id=correlation_id,
    )


async def _parse_mcp_message_body(request: Any) -> dict[str, Any] | Any:
    from vetinari.api.request_validation import body_depth_exceeded, body_has_oversized_key

    raw_body = await request.body()
    if not raw_body:
        return _error_payload("Request body must be a JSON object", 400)
    try:
        parsed = json.loads(raw_body)
    except json.JSONDecodeError:
        logger.warning("MCP transport received invalid JSON body - rejecting with 400")
        return _error_payload("Invalid JSON", 400)
    if not isinstance(parsed, dict):
        return _error_payload("Request body must be a JSON object", 400)
    if body_depth_exceeded(parsed):
        return _error_payload("Request body nesting depth exceeds maximum", 400)
    if body_has_oversized_key(parsed):
        return _error_payload("Request body contains oversized key", 400)
    return parsed


async def _dispatch_mcp_message(parsed: dict[str, Any]) -> Any:
    resource_session_error = _resource_session_initialize_error(parsed)
    if resource_session_error is not None:
        return resource_session_error
    try:
        from vetinari.mcp.server import get_mcp_server

        response = await asyncio.to_thread(get_mcp_server().handle_message, parsed)
        if response is None:
            return {"status": "accepted", "code": 202}
        _maybe_initialize_resource_session(parsed, response)
        return response
    except Exception:
        logger.exception("MCP message dispatch failed")
        raw_id = parsed.get("id")
        msg_id = raw_id if isinstance(raw_id, (str, int)) and not isinstance(raw_id, bool) else None
        return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32603, "message": "Internal server error"}}


def _resource_session_initialize_error(parsed: dict[str, Any]) -> dict[str, Any] | None:
    if parsed.get("method") != "initialize":
        return None
    params = parsed.get("params", {})
    if not isinstance(params, dict):
        return None
    session_id = _requested_resource_session_id(params)
    if session_id is None:
        return None
    if not isinstance(session_id, str) or _SAFE_RESOURCE_SESSION_ID.fullmatch(session_id) is None:
        return {
            "jsonrpc": "2.0",
            "id": _jsonrpc_response_id(parsed),
            "error": {
                "code": -32602,
                "message": "Invalid params: vetinari.resourceSessionId must be 1-128 safe characters",
            },
        }
    return None


def _maybe_initialize_resource_session(parsed: dict[str, Any], response: Any) -> None:
    if parsed.get("method") != "initialize" or not isinstance(response, dict) or "error" in response:
        return
    params = parsed.get("params", {})
    if not isinstance(params, dict):
        return
    session_id = _requested_resource_session_id(params)
    if not isinstance(session_id, str) or _SAFE_RESOURCE_SESSION_ID.fullmatch(session_id) is None:
        return
    permissions = _trusted_initialize_resource_permissions(params)
    initialize_mcp_resource_session(session_id, permissions=permissions)
    result = response.get("result")
    if not isinstance(result, dict):
        return
    capabilities = result.setdefault("capabilities", {})
    if isinstance(capabilities, dict):
        capabilities["resources"] = {"listChanged": False, "subscribe": True}
    result["vetinari"] = {"resourceSessionId": session_id}


def _requested_resource_session_id(params: dict[str, Any]) -> Any:
    capabilities = params.get("capabilities", {})
    if not isinstance(capabilities, dict):
        return None
    vetinari_capabilities = capabilities.get("vetinari", {})
    if not isinstance(vetinari_capabilities, dict):
        return None
    return vetinari_capabilities.get("resourceSessionId")


def _declared_resource_permissions(params: dict[str, Any]) -> set[str]:
    """Return server-owned resource permissions.

    Client-advertised permission dictionaries are capability requests, not
    grants. Route code that authenticates a local session can still call
    ``initialize_mcp_resource_session(..., permissions=...)`` directly.
    """
    _ = params
    return set()


def _trusted_initialize_resource_permissions(params: dict[str, Any]) -> set[str]:
    """Return the bounded resource grants accepted by the local MCP route."""
    return _client_declared_resource_permissions(params) & set(_TRUSTED_INITIALIZE_RESOURCE_PERMISSIONS)


def _client_declared_resource_permissions(params: dict[str, Any]) -> set[str]:
    capabilities = params.get("capabilities", {})
    if not isinstance(capabilities, dict):
        return set()
    vetinari_capabilities = capabilities.get("vetinari", {})
    permission_sources: tuple[Any, ...] = (
        (capabilities.get("permissions", {}), vetinari_capabilities.get("permissions", {}))
        if isinstance(vetinari_capabilities, dict)
        else (capabilities.get("permissions", {}),)
    )
    permissions: set[str] = set()
    for source in permission_sources:
        if not isinstance(source, dict):
            continue
        permissions.update(
            key
            for key, value in source.items()
            if isinstance(key, str) and key.startswith("resource:") and value is True
        )
    return permissions


def _jsonrpc_response_id(parsed: dict[str, Any]) -> str | int | None:
    raw_id = parsed.get("id")
    return raw_id if isinstance(raw_id, (str, int)) and not isinstance(raw_id, bool) else None
