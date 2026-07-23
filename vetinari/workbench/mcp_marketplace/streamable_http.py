"""MCP Streamable HTTP transport per MCP Specification 2025-11-05.

Unlike the prior HTTP+SSE transport (one GET /sse + POST /message), Streamable
HTTP uses a **single POST** endpoint that returns either a synchronous JSON
response or a stream of newline-delimited JSON frames for long-running calls.

This module provides:
  - ``StreamableHttpRequest``  — typed outgoing call descriptor
  - ``StreamableHttpResponse`` — typed per-frame response record
  - ``StreamableHttpClient``   — sends requests to a remote MCP endpoint
  - ``StreamableHttpServer``   — base handler that dispatches to registered methods

References:
  MCP Spec 2025-11-05 §3.2 Streamable HTTP Transport
  https://spec.modelcontextprotocol.io/specification/2025-11-05/basic/transports/

Usage::

    from vetinari.workbench.mcp_marketplace.streamable_http import (
        StreamableHttpClient,
        StreamableHttpRequest,
    )
    client = StreamableHttpClient("https://example.com/mcp")
    for frame in client.send(StreamableHttpRequest("tools/list", {}, "req-1")):
        if frame.is_final:
            print(frame.data)
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

import requests

logger = logging.getLogger(__name__)

# -- Constants ---------------------------------------------------------------

# Maximum bytes to read per streaming line to guard against runaway responses.
_MAX_LINE_BYTES = 1 * 1024 * 1024  # 1 MB per frame

# Default request timeout (connect, read) in seconds.
_DEFAULT_TIMEOUT_SECONDS = (5, 30)

# Content-type header sent on all Streamable HTTP requests per spec §3.2.
_CONTENT_TYPE_JSON = "application/json"

# -- Data types --------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StreamableHttpRequest:
    """An outgoing MCP call over the Streamable HTTP transport.

    Attributes:
        method: MCP method name (e.g. ``"tools/call"``, ``"tools/list"``).
        params: Method parameters — any JSON-serialisable mapping.
        request_id: Caller-chosen unique identifier echoed in every response frame.
    """

    method: str
    params: dict[str, Any]
    request_id: str

    def to_json_rpc(self) -> dict[str, Any]:
        """Serialise to a JSON-RPC 2.0 request object.

        Returns:
            A dict ready for ``json.dumps``.
        """
        return {
            "jsonrpc": "2.0",
            "id": self.request_id,
            "method": self.method,
            "params": self.params,
        }


@dataclass(frozen=True, slots=True)
class StreamableHttpResponse:
    """A single response frame from the Streamable HTTP transport.

    Attributes:
        request_id: Echo of the originating :attr:`StreamableHttpRequest.request_id`.
        status: Frame classification — ``"ok"``, ``"error"``, or ``"stream"``.
            ``"ok"`` and ``"error"`` are final frames; ``"stream"`` indicates
            more frames will follow.
        data: Parsed JSON payload from the frame.
        is_final: ``True`` when this is the last frame for the request.
    """

    request_id: str
    status: str  # "ok" | "error" | "stream"
    data: dict[str, Any]
    is_final: bool

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return (
            "StreamableHttpResponse("
            f"request_id={self.request_id!r}, status={self.status!r}, "
            f"is_final={self.is_final!r})"
        )


# -- Client ------------------------------------------------------------------


class StreamableHttpClient:
    """MCP Streamable HTTP transport client per MCP Spec 2025-11-05.

    Unlike the prior HTTP+SSE transport, Streamable HTTP uses a single POST
    endpoint that can return either a synchronous response or a stream of
    newline-delimited JSON responses for long-running calls.

    The client performs one POST per :meth:`send` call, streams the response
    line by line, and yields a :class:`StreamableHttpResponse` per frame.
    The last frame has ``is_final=True``.

    Args:
        endpoint: Full URL of the MCP server's Streamable HTTP endpoint.
        timeout: ``(connect_timeout, read_timeout)`` pair in seconds.
        headers: Extra HTTP headers merged into every request (e.g. auth tokens).

    Example::

        client = StreamableHttpClient("https://example.com/mcp")
        for frame in client.send(StreamableHttpRequest("tools/list", {}, "r1")):
            print(frame.status, frame.data)
    """

    def __init__(
        self,
        endpoint: str,
        *,
        timeout: tuple[float, float] = _DEFAULT_TIMEOUT_SECONDS,
        headers: dict[str, str] | None = None,
    ) -> None:
        if not endpoint:
            raise ValueError("StreamableHttpClient endpoint must be a non-empty URL")
        self._endpoint = endpoint
        self._timeout = timeout
        self._extra_headers: dict[str, str] = dict(headers) if headers else {}

    @property
    def endpoint(self) -> str:
        """The configured MCP endpoint URL."""
        return self._endpoint

    def send(self, request: StreamableHttpRequest) -> Iterator[StreamableHttpResponse]:
        """Send a Streamable HTTP request and yield response frames.

        Posts ``request`` as a JSON-RPC 2.0 body to :attr:`endpoint` with
        ``stream=True``.  Each newline-delimited JSON line is decoded and
        yielded as a :class:`StreamableHttpResponse`.  The final line sets
        ``is_final=True``.

        Args:
            request: The MCP call to dispatch.

        Yields:
            :class:`StreamableHttpResponse` frames in arrival order.

        Raises:
            requests.exceptions.RequestException: On transport-level failures
                (connection refused, timeout, non-2xx without JSON body).
            ValueError: When a response line cannot be decoded as JSON.
        """
        body = json.dumps(request.to_json_rpc())
        http_headers = {
            "Content-Type": _CONTENT_TYPE_JSON,
            "Accept": f"{_CONTENT_TYPE_JSON}, text/event-stream",
        }
        http_headers.update(self._extra_headers)

        logger.debug("StreamableHttpClient POST %s method=%s id=%s", self._endpoint, request.method, request.request_id)

        response = requests.post(
            self._endpoint,
            data=body,
            headers=http_headers,
            timeout=self._timeout,
            stream=True,
        )
        response.raise_for_status()

        pending: dict[str, Any] | None = None
        pending_index = -1
        for index, raw_line in enumerate(response.iter_lines(chunk_size=_MAX_LINE_BYTES)):
            if not raw_line:
                continue
            parsed = _parse_response_frame(raw_line, index=index, request_id=request.request_id)
            if "error" in parsed:
                yield StreamableHttpResponse(
                    request_id=request.request_id,
                    status="error",
                    data=parsed,
                    is_final=True,
                )
                return
            if pending is not None:
                yield StreamableHttpResponse(
                    request_id=request.request_id,
                    status="stream",
                    data=pending,
                    is_final=False,
                )
            pending = parsed
            pending_index = index

        if pending is not None:
            yield StreamableHttpResponse(
                request_id=request.request_id,
                status="ok",
                data=pending,
                is_final=True,
            )
        elif pending_index < 0:
            return


def _parse_response_frame(raw_line: bytes | str, *, index: int, request_id: str) -> dict[str, Any]:
    text = raw_line.decode("utf-8") if isinstance(raw_line, bytes) else raw_line
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"StreamableHttpClient: could not decode response frame {index} (id={request_id!r}): {exc}"
        ) from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"StreamableHttpClient: response frame {index} (id={request_id!r}) must be a JSON object")
    return parsed


# -- Server ------------------------------------------------------------------

# Method handler type: receives params dict, returns response data dict.
_MethodHandler = Callable[[dict[str, Any]], dict[str, Any]]


class StreamableHttpServer:
    """Server-side Streamable HTTP handler — dispatches to registered method handlers.

    Register named MCP methods via :meth:`register_method`.  Dispatch
    incoming :class:`StreamableHttpRequest` objects via :meth:`handle`, which
    yields exactly one :class:`StreamableHttpResponse` frame (``is_final=True``).
    Subclass and override :meth:`handle` if you need multi-frame streaming.

    Registered handlers receive the ``params`` dict from the request and must
    return a JSON-serialisable dict.  Unregistered methods return a JSON-RPC
    ``-32601 Method not found`` error frame.

    Example::

        server = StreamableHttpServer()
        server.register_method("tools/list", lambda p: {"tools": []})
        for frame in server.handle(StreamableHttpRequest("tools/list", {}, "r1")):
            print(frame)
    """

    def __init__(self) -> None:
        # Map of method name → handler callable.
        # Side effects: none (pure in-memory registry).
        self._handlers: dict[str, _MethodHandler] = {}

    def register_method(self, name: str, handler: _MethodHandler) -> None:
        """Register a handler for an MCP method name.

        Args:
            name: MCP method name (e.g. ``"tools/call"``).
            handler: Callable that accepts ``params: dict[str, Any]`` and
                returns ``dict[str, Any]``.

        Raises:
            ValueError: If ``name`` is empty.
        """
        if not name:
            raise ValueError("method name must be non-empty")
        self._handlers[name] = handler

    def handle(self, request: StreamableHttpRequest) -> Iterator[StreamableHttpResponse]:
        """Dispatch an incoming request to a registered handler.

        Yields one final :class:`StreamableHttpResponse`.  Errors from the
        handler are caught and returned as JSON-RPC error frames rather than
        raised, so the transport layer stays clean.

        Args:
            request: Incoming MCP call.

        Yields:
            A single :class:`StreamableHttpResponse` with ``is_final=True``.
        """
        handler = self._handlers.get(request.method)
        if handler is None:
            logger.warning(
                "StreamableHttpServer: method %r not found — returning JSON-RPC -32601",
                request.method,
            )
            yield StreamableHttpResponse(
                request_id=request.request_id,
                status="error",
                data={
                    "jsonrpc": "2.0",
                    "id": request.request_id,
                    "error": {"code": -32601, "message": f"Method not found: {request.method!r}"},
                },
                is_final=True,
            )
            return

        try:
            result = handler(request.params)
        except Exception as exc:
            logger.warning(
                "StreamableHttpServer: handler for %r raised — returning JSON-RPC -32603: %s",
                request.method,
                exc,
            )
            yield StreamableHttpResponse(
                request_id=request.request_id,
                status="error",
                data={
                    "jsonrpc": "2.0",
                    "id": request.request_id,
                    "error": {"code": -32603, "message": "Internal error", "data": str(exc)},
                },
                is_final=True,
            )
            return

        yield StreamableHttpResponse(
            request_id=request.request_id,
            status="ok",
            data={"jsonrpc": "2.0", "id": request.request_id, "result": result},
            is_final=True,
        )


__all__ = [
    "StreamableHttpClient",
    "StreamableHttpRequest",
    "StreamableHttpResponse",
    "StreamableHttpServer",
]
