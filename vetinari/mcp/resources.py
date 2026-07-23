"""HTTP+SSE resource helpers for MCP."""

from __future__ import annotations

import json
import re
from collections import deque
from dataclasses import dataclass, field
from threading import Lock
from typing import Any

from vetinari.privacy import privacy_receipt

_MAX_RESOURCE_EVENT_BYTES = 4096
_RESOURCE_URI_PREFIX = "resource://"
_RESOURCE_URI_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")


@dataclass(frozen=True, slots=True)
class McpSupportEnvelope:
    """User-facing MCP support envelope for fail-closed transport decisions."""

    code: str
    message: str
    recovery: str

    def to_dict(self) -> dict[str, str]:
        """Return a JSON-ready envelope payload."""
        return {"code": self.code, "message": self.message, "recovery": self.recovery}


@dataclass(frozen=True, slots=True)
class _McpResourceStreamFrame:
    """One bounded MCP resource event retained for SSE subscribers."""

    event_id: int
    event_type: str
    uri: str
    payload: str
    correlation_id: str

    def __repr__(self) -> str:
        return (
            "_McpResourceStreamFrame("
            f"event_id={self.event_id!r}, event_type={self.event_type!r}, uri={self.uri!r}, "
            f"correlation_id={self.correlation_id!r})"
        )

    def to_sse_message(self) -> dict[str, str]:
        """Return the SSE message dict.

        Returns:
            JSON-serializable event fields for the SSE response writer.
        """
        data = {
            "correlation_id": self.correlation_id,
            "payload": self.payload,
            "uri": self.uri,
        }
        return {
            "data": json.dumps(data, sort_keys=True),
            "event": self.event_type,
            "id": str(self.event_id),
        }


@dataclass(frozen=True, slots=True)
class McpResource:
    """Readable MCP resource registered behind a session permission."""

    uri: str
    title: str
    payload: str
    required_permission: str
    privacy_class: str = "operational"
    subject_id: str | None = None

    def __repr__(self) -> str:
        return f"McpResource(uri={self.uri!r}, title={self.title!r}, required_permission={self.required_permission!r})"

    def privacy_receipt(self) -> dict[str, Any]:
        """Return fail-closed privacy metadata for this resource exposure."""
        return privacy_receipt(
            privacy_class=self.privacy_class,
            subject_id=self.subject_id,
            source=f"mcp.resource:{self.uri}",
            retention_days=7,
            redaction_applied=True,
        )


@dataclass(slots=True)
class McpResourceSession:
    """Bound resource session with server-owned permissions and bounded events."""

    session_id: str
    initialized: bool
    permissions: frozenset[str] = frozenset()
    max_events: int = 16
    events: deque[str] = field(default_factory=deque)
    stream_events: deque[_McpResourceStreamFrame] = field(default_factory=deque)
    next_stream_event_id: int = 1
    lock: Any = field(default_factory=Lock, repr=False, compare=False)

    def __repr__(self) -> str:
        return (
            f"McpResourceSession(session_id={self.session_id!r}, initialized={self.initialized!r}, "
            f"permissions={sorted(self.permissions)!r}, events={len(self.events)!r})"
        )

    def enqueue(self, event: str) -> McpSupportEnvelope | None:
        """Append one redacted SSE event or return a support envelope.

        Returns:
            Support envelope when the event is rejected, otherwise None.
        """
        if not self.initialized:
            return McpSupportEnvelope("MCP_SESSION", "MCP session is not initialized", "initialize before streaming")
        if len(event.encode("utf-8")) > _MAX_RESOURCE_EVENT_BYTES:
            return McpSupportEnvelope("MCP_EVENT_SIZE", "MCP event exceeds size limit", "send a smaller event")
        with self.lock:
            while len(self.events) >= self.max_events:
                self.events.popleft()
            self.events.append(redact(event))
        return None

    def enqueue_stream_event(
        self,
        *,
        event_type: str,
        uri: str,
        payload: str,
        correlation_id: str,
    ) -> tuple[dict[str, str] | None, McpSupportEnvelope | None]:
        """Append one structured resource event for SSE subscribers.

        Args:
            event_type: SSE event name, such as ``resources/read``.
            uri: MCP resource URI associated with the event.
            payload: Event payload to expose after redaction.
            correlation_id: Caller-visible correlation identifier.

        Returns:
            The queued SSE message and no support envelope on success, otherwise
            ``None`` and a support envelope describing the fail-closed reason.
        """
        if not self.initialized:
            return None, McpSupportEnvelope("MCP_SESSION", "MCP session is not initialized", "initialize first")
        redacted_payload = redact(payload)
        if len(redacted_payload.encode("utf-8")) > _MAX_RESOURCE_EVENT_BYTES:
            return None, McpSupportEnvelope(
                "MCP_EVENT_SIZE",
                "MCP event exceeds size limit",
                "send a smaller event",
            )
        with self.lock:
            frame = _McpResourceStreamFrame(
                event_id=self.next_stream_event_id,
                event_type=event_type,
                uri=uri,
                payload=redacted_payload,
                correlation_id=correlation_id,
            )
            self.next_stream_event_id += 1
            while len(self.stream_events) >= self.max_events:
                self.stream_events.popleft()
            self.stream_events.append(frame)
            return frame.to_sse_message(), None

    def next_stream_event(self, uri: str, *, after_event_id: int = 0) -> dict[str, str] | None:
        """Return the next retained stream event for ``uri`` after ``after_event_id``.

        Args:
            uri: MCP resource URI the subscriber requested.
            after_event_id: Last event ID the subscriber already observed.

        Returns:
            An SSE message dict, or ``None`` when no
            retained event is ready for this subscriber.
        """
        with self.lock:
            for frame in self.stream_events:
                if frame.uri == uri and frame.event_id > after_event_id:
                    return frame.to_sse_message()
        return None

    def cleanup(self) -> int:
        """Clear queued events and mark the session disconnected.

        Returns:
            Number of queued events removed.
        """
        with self.lock:
            removed = len(self.events) + len(self.stream_events)
            self.events.clear()
            self.stream_events.clear()
        self.initialized = False
        return removed


class McpResourceRegistry:
    """In-memory registry for local MCP resource descriptors."""

    def __init__(self) -> None:
        self._resources: dict[str, McpResource] = {}

    def register(self, resource: McpResource) -> None:
        """Register a resource URI after validating the MCP resource scheme.

        Raises:
            ValueError: If the resource URI does not use the MCP resource scheme.
        """
        if not _is_valid_resource_uri(resource.uri):
            raise ValueError("resource uri must use resource://")
        resource.privacy_receipt()
        self._resources[resource.uri] = resource

    def list_resources(self, session: McpResourceSession) -> tuple[list[dict[str, str]], McpSupportEnvelope | None]:
        """List resources visible to an initialized session.

        Returns:
            Resource summaries and an optional support envelope.
        """
        if not session.initialized:
            return [], McpSupportEnvelope("MCP_SESSION", "MCP session is not initialized", "initialize before listing")
        return (
            [
                {"uri": r.uri, "title": r.title, "privacy_class": r.privacy_receipt()["privacy_class"]}
                for r in self._resources.values()
                if r.required_permission in session.permissions
            ],
            None,
        )

    def read_resource(
        self,
        session: McpResourceSession,
        uri: str,
    ) -> tuple[str | None, McpSupportEnvelope | None]:
        """Read one resource when the session owns the required permission.

        Args:
            session: Server-owned MCP resource session.
            uri: MCP resource URI to read.

        Returns:
            Resource payload and optional support envelope.
        """
        if not session.initialized:
            return None, McpSupportEnvelope(
                "MCP_SESSION", "MCP session is not initialized", "initialize before reading"
            )
        support = _resource_uri_support(uri)
        if support is not None:
            return None, support
        resource = self._resources.get(uri)
        if resource is None:
            return None, McpSupportEnvelope("MCP_RESOURCE", "MCP resource was not found", "refresh resources/list")
        if resource.required_permission not in session.permissions:
            return None, McpSupportEnvelope(
                "MCP_RESOURCE_PERMISSION",
                "MCP resource permission is missing",
                "request declared resource permission",
            )
        return redact(resource.payload), None

    def resource_privacy_receipt(
        self,
        session: McpResourceSession,
        uri: str,
    ) -> tuple[dict[str, Any] | None, McpSupportEnvelope | None]:
        """Return privacy metadata for a readable resource.

        Args:
            session: Server-owned MCP resource session used to authorize the
                resource read before receipt disclosure.
            uri: MCP resource URI whose privacy receipt should be returned.

        Returns:
            A privacy receipt dictionary and no support envelope when the
            resource is readable; otherwise no receipt and the support
            envelope explaining the missing session, resource, or permission.
        """
        _payload, support = self.read_resource(session, uri)
        if support is not None:
            return None, support
        resource = self._resources[uri]
        return resource.privacy_receipt(), None


def sse_event(event: str, data: dict[str, Any], *, correlation_id: str) -> str:
    """Format one MCP payload as a correlated SSE event.

    Args:
        event: SSE event name.
        data: JSON-ready event payload.
        correlation_id: Caller-visible request correlation ID.

    Returns:
        Formatted text/event-stream event body.
    """
    payload = {"correlation_id": correlation_id, **data}
    return f"event: {event}\ndata: {json.dumps(payload, sort_keys=True)}\n\n"


def redact(value: str) -> str:
    """Remove secret-shaped stream content and cap the visible payload size.

    Returns:
        Redacted or truncated stream content.
    """
    lowered = value.lower()
    if any(marker in lowered for marker in ("secret", "token", "api_key", "password")):
        return "[redacted]"
    return value[:_MAX_RESOURCE_EVENT_BYTES]


def _is_valid_resource_uri(uri: str) -> bool:
    if not isinstance(uri, str) or not uri.startswith(_RESOURCE_URI_PREFIX):
        return False
    suffix = uri.removeprefix(_RESOURCE_URI_PREFIX)
    if not suffix or suffix != suffix.strip() or "\\" in suffix:
        return False
    if any(ord(char) < 32 or ord(char) == 127 for char in suffix):
        return False
    segments = suffix.split("/")
    if any(segment in {"", ".", ".."} for segment in segments):
        return False
    return all(_RESOURCE_URI_SEGMENT_RE.fullmatch(segment) for segment in segments)


def _resource_uri_support(uri: str) -> McpSupportEnvelope | None:
    if _is_valid_resource_uri(uri):
        return None
    return McpSupportEnvelope(
        "MCP_RESOURCE_URI",
        "MCP resource identifier must use resource://",
        "refresh resources/list and subscribe to a listed resource URI",
    )
