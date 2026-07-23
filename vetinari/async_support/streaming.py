"""Streaming pipeline primitives for async chunk-by-chunk delivery."""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from collections.abc import AsyncIterable
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class StreamChunk:
    """A single chunk of a streaming response.

    Attributes:
        content: Text content of this chunk.
        chunk_index: Zero-based position of this chunk in the stream.
        is_final: ``True`` when this is the last chunk in the stream.
        metadata: Arbitrary key-value metadata attached to the chunk.
    """

    content: str
    chunk_index: int
    is_final: bool
    metadata: dict[str, Any] = field(default_factory=dict)

    def __repr__(self) -> str:
        return (
            f"StreamChunk(chunk_index={self.chunk_index!r}, is_final={self.is_final!r}, content={self.content[:40]!r})"
        )


# ---------------------------------------------------------------------------
# Handler ABC
# ---------------------------------------------------------------------------


class StreamHandler(ABC):
    """Abstract base for streaming output handlers.

    Implement the three coroutines to receive streaming events as they
    arrive from the model.
    """

    @abstractmethod
    async def on_chunk(self, chunk: StreamChunk) -> None:
        """Called for every chunk received from the stream.

        Args:
            chunk: The incoming :class:`StreamChunk`.
        """

    @abstractmethod
    async def on_complete(self, full_response: str) -> None:
        """Called once when the stream finishes successfully.

        Args:
            full_response: Concatenation of all chunk content.
        """

    @abstractmethod
    async def on_error(self, error: Exception) -> None:
        """Called when the stream terminates with an error.

        Args:
            error: The exception that caused the stream to fail.
        """


# ---------------------------------------------------------------------------
# Concrete handlers
# ---------------------------------------------------------------------------


class SSEStreamHandler(StreamHandler):
    """Formats stream chunks as Server-Sent Events for web delivery.

    Each chunk is encoded as an SSE ``data:`` line.  The final event
    carries ``event: done``.  Formatted events are stored in
    :attr:`events` for retrieval or forwarding.

    Example::

        handler = SSEStreamHandler()
        await handler.on_chunk(StreamChunk("Hello", 0, False))
        logger.debug(handler.events[0])
        # data: {"content": "Hello", "index": 0, "is_final": false}
    """

    def __init__(self) -> None:
        self.events: list[str] = []

    async def on_chunk(self, chunk: StreamChunk) -> None:
        """Encode *chunk* as an SSE ``data:`` event and append to :attr:`events`.

        Args:
            chunk: Incoming stream chunk.
        """
        payload = json.dumps(
            {
                "content": chunk.content,
                "index": chunk.chunk_index,
                "is_final": chunk.is_final,
            },
        )
        if chunk.is_final:
            event = f"event: done\ndata: {payload}\n\n"
        else:
            event = f"data: {payload}\n\n"
        self.events.append(event)
        logger.debug("SSE event queued (chunk %d)", chunk.chunk_index)

    async def on_complete(self, full_response: str) -> None:
        """Append a terminal SSE ``event: complete`` line.

        Args:
            full_response: Full concatenated response text.
        """
        payload = json.dumps({"full_response": _redact_stream_content(full_response)})
        self.events.append(f"event: complete\ndata: {payload}\n\n")

    async def on_error(self, error: Exception) -> None:
        """Append an SSE ``event: error`` line.

        Args:
            error: The exception that terminated the stream.
        """
        payload = json.dumps({"error": _redact_stream_content(str(error))})
        self.events.append(f"event: error\ndata: {payload}\n\n")
        logger.error("SSE stream error: %s", _redact_stream_content(str(error)))


class RedactingSSEStreamHandler(SSEStreamHandler):
    """SSE handler that minimizes secret-bearing chunks before queuing."""

    def __init__(self, *, max_event_bytes: int = 4096) -> None:
        super().__init__()
        self.max_event_bytes = max_event_bytes

    async def on_chunk(self, chunk: StreamChunk) -> None:
        """Redact and bound an SSE chunk before it enters the event queue."""
        content = _redact_stream_content(chunk.content)
        if len(content.encode("utf-8")) > self.max_event_bytes:
            await self.on_error(ValueError("stream event exceeds size limit"))
            return
        await super().on_chunk(
            StreamChunk(
                content=content,
                chunk_index=chunk.chunk_index,
                is_final=chunk.is_final,
                metadata={k: _redact_stream_content(str(v)) for k, v in chunk.metadata.items()},
            )
        )

    async def on_complete(self, full_response: str) -> None:
        """Redact the terminal aggregate before it enters SSE replay state."""
        content = _redact_stream_content(full_response)
        if len(content.encode("utf-8")) > self.max_event_bytes:
            await self.on_error(ValueError("stream event exceeds size limit"))
            return
        await super().on_complete(content)


class LoggingStreamHandler(StreamHandler):
    """Logs each chunk at DEBUG level for diagnostic purposes.

    Args:
        name: Logger name suffix; defaults to ``"stream"``.
    """

    def __init__(self, name: str = "stream") -> None:
        self._log = logging.getLogger(f"{__name__}.{name}")

    async def on_chunk(self, chunk: StreamChunk) -> None:
        """Log the incoming chunk.

        Args:
            chunk: Incoming stream chunk.
        """
        self._log.debug(
            "chunk[%d] final=%s content=%r",
            chunk.chunk_index,
            chunk.is_final,
            _redact_stream_content(chunk.content),
        )

    async def on_complete(self, full_response: str) -> None:
        """Log stream completion with total length.

        Args:
            full_response: Full concatenated response text.
        """
        self._log.debug("Stream complete, total length=%d", len(full_response))

    async def on_error(self, error: Exception) -> None:
        """Log stream error.

        Args:
            error: The exception that terminated the stream.
        """
        self._log.error("Stream error: %s", _redact_stream_content(str(error)), exc_info=True)


class BufferedStreamHandler(StreamHandler):
    """Buffers all chunks and assembles the full response in memory.

    Access the assembled text via :attr:`buffer` once the stream
    completes.

    Example::

        handler = BufferedStreamHandler()
        await handler.on_chunk(StreamChunk("Hello ", 0, False))
        await handler.on_chunk(StreamChunk("world", 1, True))
        await handler.on_complete("Hello world")
        assert handler.buffer == "Hello world"
    """

    def __init__(self) -> None:
        self._chunks: list[str] = []
        self.buffer: str = ""
        self.completed: bool = False
        self.error: Exception | None = None

    async def on_chunk(self, chunk: StreamChunk) -> None:
        """Append chunk content to the internal buffer.

        Args:
            chunk: Incoming stream chunk.
        """
        self._chunks.append(chunk.content)

    async def on_complete(self, full_response: str) -> None:
        """Store the final assembled response.

        Args:
            full_response: Full concatenated response text.
        """
        self.buffer = full_response
        self.completed = True

    async def on_error(self, error: Exception) -> None:
        """Record the stream error.

        Args:
            error: The exception that terminated the stream.
        """
        self.error = error
        self.buffer = "".join(self._chunks)  # partial result
        logger.error("Buffered stream error: %s", _redact_stream_content(str(error)))


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


class StreamRouter:
    """Fan out streaming events to multiple :class:`StreamHandler` instances.

    Example::

        router = StreamRouter()
        router.add_handler(SSEStreamHandler())
        router.add_handler(LoggingStreamHandler())
        await router.route_chunk(StreamChunk("Hi", 0, True))
        await router.route_complete("Hi")
    """

    def __init__(self) -> None:
        self._handlers: list[StreamHandler] = []

    def add_handler(self, handler: StreamHandler) -> None:
        """Register a handler to receive streaming events.

        Args:
            handler: Any :class:`StreamHandler` implementation.
        """
        self._handlers.append(handler)

    async def route_chunk(self, chunk: StreamChunk) -> None:
        """Deliver *chunk* to every registered handler.

        Errors raised by individual handlers are logged and suppressed so
        that a failing handler cannot disrupt other handlers.

        Args:
            chunk: Incoming stream chunk.
        """
        for handler in self._handlers:
            try:
                await handler.on_chunk(chunk)
            except Exception as exc:
                logger.error(
                    "Handler %s raised on_chunk: %s",
                    handler.__class__.__name__,
                    exc,
                    exc_info=True,
                )

    async def route_complete(self, full_response: str) -> None:
        """Notify every registered handler that the stream has completed.

        Args:
            full_response: Full concatenated response text.
        """
        for handler in self._handlers:
            try:
                await handler.on_complete(full_response)
            except Exception as exc:
                logger.error(
                    "Handler %s raised on_complete: %s",
                    handler.__class__.__name__,
                    exc,
                    exc_info=True,
                )

    async def route_error(self, error: Exception) -> None:
        """Notify every registered handler of a stream error.

        Args:
            error: The exception that terminated the stream.
        """
        for handler in self._handlers:
            try:
                await handler.on_error(error)
            except Exception as exc:
                logger.error(
                    "Handler %s raised on_error: %s — continuing to next handler",
                    handler.__class__.__name__,
                    exc,
                    exc_info=True,
                )


# ---------------------------------------------------------------------------
# High-level helper
# ---------------------------------------------------------------------------


async def stream_tokens(
    token_source: AsyncIterable[str],
    router: StreamRouter,
) -> str:
    """Drive a token-by-token async iterator through a :class:`StreamRouter`.

    Iterates *token_source* (any async-iterable of ``str``), wraps each token
    in a :class:`StreamChunk`, and fans it out to every handler registered on
    *router*.  On success calls :meth:`StreamRouter.route_complete`; on any
    exception calls :meth:`StreamRouter.route_error` before re-raising so the
    caller can decide whether to propagate or handle.

    Args:
        token_source: Async-iterable that yields ``str`` tokens.
        router: Pre-configured :class:`StreamRouter` with at least one handler.

    Returns:
        The full response assembled by concatenating all tokens.

    Raises:
        Exception: Re-raises any exception thrown by *token_source* after
            routing it to all registered error handlers.
    """
    parts: list[str] = []
    index = 0
    try:
        async for token in token_source:
            chunk = StreamChunk(content=token, chunk_index=index, is_final=False)
            await router.route_chunk(chunk)
            parts.append(token)
            index += 1
        full = "".join(parts)
        if parts:
            # Re-send the last logical chunk marked as final so handlers know the stream ended.
            final_chunk = StreamChunk(content="", chunk_index=index, is_final=True)
            await router.route_chunk(final_chunk)
        await router.route_complete(full)
        logger.debug("stream_tokens finished — %d tokens, %d chars", index, len(full))
        return full
    except Exception as exc:
        logger.error(
            "stream_tokens encountered an error after %d tokens — routing error to handlers",
            index,
        )
        await router.route_error(exc)
        raise


def _redact_stream_content(value: str) -> str:
    lowered = value.lower()
    if any(marker in lowered for marker in ("secret", "token", "api_key", "password")):
        return "[redacted]"
    return value
