"""AG-UI streaming protocol implementation for CopilotKit compatibility.

Implements the CopilotKit Agent-UI (AG-UI) streaming protocol, which defines
17 server-sent event (SSE) types for streaming agent state and output to
connected front-end UIs.

The :class:`AGUIEventEmitter` is the main entry point.  It buffers
:class:`AGUIEvent` objects and can convert them to SSE strings or to
:class:`~vetinari.async_support.streaming.StreamChunk` objects compatible
with Vetinari's existing streaming pipeline.

Reference: CopilotKit AG-UI protocol specification v1.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from vetinari.async_support.streaming import StreamChunk

logger = logging.getLogger(__name__)


# ── AG-UI event type enum ────────────────────────────────────────────────────


class AGUIEventType(str, Enum):
    """The 17 AG-UI event types defined by the CopilotKit AG-UI protocol.

    These events cover the full lifecycle of an agent run, including
    text streaming, tool invocation, state management, step tracking,
    and error reporting.
    """

    # Text message lifecycle
    TEXT_MESSAGE_START = "TEXT_MESSAGE_START"
    TEXT_MESSAGE_CONTENT = "TEXT_MESSAGE_CONTENT"
    TEXT_MESSAGE_END = "TEXT_MESSAGE_END"

    # Tool call lifecycle
    TOOL_CALL_START = "TOOL_CALL_START"
    TOOL_CALL_ARGS = "TOOL_CALL_ARGS"
    TOOL_CALL_END = "TOOL_CALL_END"

    # State management
    STATE_SNAPSHOT = "STATE_SNAPSHOT"
    STATE_DELTA = "STATE_DELTA"
    MESSAGES_SNAPSHOT = "MESSAGES_SNAPSHOT"

    # Raw and custom events
    RAW = "RAW"
    CUSTOM = "CUSTOM"

    # Step lifecycle
    STEP_STARTED = "STEP_STARTED"
    STEP_FINISHED = "STEP_FINISHED"

    # Run lifecycle
    RUN_STARTED = "RUN_STARTED"
    RUN_FINISHED = "RUN_FINISHED"
    RUN_ERROR = "RUN_ERROR"

    # Metadata
    METADATA = "METADATA"


# ── AGUIEvent dataclass ──────────────────────────────────────────────────────


def _utc_now_iso() -> str:
    """Return the current UTC time as an ISO 8601 string.

    Returns:
        ISO 8601 timestamp string in UTC.
    """
    return datetime.now(tz=timezone.utc).isoformat()


@dataclass(frozen=True, slots=True)
class AGUIEvent:
    """A single AG-UI protocol event ready for SSE transport.

    Attributes:
        event_type: The AG-UI event type from :class:`AGUIEventType`.
        data: Structured payload for this event.
        timestamp: ISO 8601 UTC timestamp at which the event was created.
    """

    event_type: AGUIEventType
    data: dict[str, Any]
    timestamp: str = field(default_factory=_utc_now_iso)

    def to_sse(self) -> str:
        """Serialise this event as a Server-Sent Events (SSE) string.

        The SSE format for AG-UI is::

            event: {event_type}
            data: {json_payload}

            (blank line terminates the event)

        Returns:
            SSE-formatted string ready to write to a streaming response.
        """
        payload = {
            "type": self.event_type.value,
            "timestamp": self.timestamp,
            **self.data,
        }
        return f"event: {self.event_type.value}\ndata: {json.dumps(payload)}\n\n"

    def to_stream_chunk(self, chunk_index: int = 0, is_final: bool = False) -> StreamChunk:
        """Convert this event to a :class:`~vetinari.async_support.streaming.StreamChunk`.

        Args:
            chunk_index: Zero-based position of this chunk in the stream.
            is_final: ``True`` if this is the last chunk in the stream.

        Returns:
            :class:`StreamChunk` wrapping the SSE representation of this event.
        """
        return StreamChunk(
            content=self.to_sse(),
            chunk_index=chunk_index,
            is_final=is_final,
            metadata={
                "event_type": self.event_type.value,
                "timestamp": self.timestamp,
            },
        )


# ── AGUIEventEmitter ─────────────────────────────────────────────────────────


class AGUIEventEmitter:
    """Emits and buffers AG-UI streaming events.

    The emitter maintains an ordered buffer of :class:`AGUIEvent` objects.
    Each :meth:`emit` call appends to the buffer and returns the SSE string
    for that event so callers can stream it immediately.

    Use :meth:`to_stream_chunks` to convert the entire buffer into
    :class:`~vetinari.async_support.streaming.StreamChunk` objects compatible
    with Vetinari's streaming pipeline.

    Example::

        emitter = AGUIEventEmitter()
        emitter.emit_run_start("run-001")
        emitter.emit_text_start("msg-001")
        emitter.emit_text_content("msg-001", "Hello from the agent!")
        emitter.emit_text_end("msg-001")
        emitter.emit_run_finish("run-001")
        chunks = emitter.to_stream_chunks()
    """

    def __init__(self) -> None:
        """Initialise the emitter with an empty event buffer."""
        self._events: list[AGUIEvent] = []
        logger.debug("AGUIEventEmitter initialised")

    # ── Core emit ─────────────────────────────────────────────────────────────

    def emit(self, event_type: AGUIEventType, data: dict[str, Any]) -> str:
        """Create and buffer an AG-UI event, returning its SSE string.

        Args:
            event_type: The AG-UI event type to emit.
            data: Structured payload for the event.

        Returns:
            SSE-formatted string for this event.
        """
        event = AGUIEvent(event_type=event_type, data=data)
        self._events.append(event)
        sse = event.to_sse()
        logger.debug("Emitted AG-UI event type=%s", event_type.value)
        return sse

    # ── Text message helpers ──────────────────────────────────────────────────

    def emit_text_start(self, message_id: str) -> str:
        """Emit a TEXT_MESSAGE_START event.

        Args:
            message_id: Unique identifier for the text message stream.

        Returns:
            SSE string for the emitted event.
        """
        return self.emit(
            AGUIEventType.TEXT_MESSAGE_START,
            {"messageId": message_id},
        )

    def emit_text_content(self, message_id: str, content: str) -> str:
        """Emit a TEXT_MESSAGE_CONTENT event with a content chunk.

        Args:
            message_id: Identifier of the in-progress text message.
            content: Text chunk to stream to the UI.

        Returns:
            SSE string for the emitted event.
        """
        return self.emit(
            AGUIEventType.TEXT_MESSAGE_CONTENT,
            {"messageId": message_id, "content": content},
        )

    def emit_text_end(self, message_id: str) -> str:
        """Emit a TEXT_MESSAGE_END event to close a text message stream.

        Args:
            message_id: Identifier of the text message being closed.

        Returns:
            SSE string for the emitted event.
        """
        return self.emit(
            AGUIEventType.TEXT_MESSAGE_END,
            {"messageId": message_id},
        )

    # ── Tool call helpers ──────────────────────────────────────────────────────

    def emit_tool_call(self, tool_name: str, args: dict[str, Any]) -> list[str]:
        """Emit the full tool call lifecycle (start, args, end).

        If ``args`` cannot be JSON-serialised, emits TOOL_CALL_START followed
        immediately by TOOL_CALL_END carrying an ``error`` field so the
        lifecycle is always closed — never left half-open with a START and no END.

        Args:
            tool_name: Name of the tool being called.
            args: Argument dict for the tool invocation.

        Returns:
            On success: list of three SSE strings — TOOL_CALL_START,
            TOOL_CALL_ARGS, TOOL_CALL_END.
            On serialisation failure: list of two SSE strings —
            TOOL_CALL_START, TOOL_CALL_END (with error payload).
        """
        call_id = str(uuid.uuid4())
        start_sse = self.emit(
            AGUIEventType.TOOL_CALL_START,
            {"callId": call_id, "toolName": tool_name},
        )
        try:
            args_json = json.dumps(args)
        except (TypeError, ValueError) as exc:
            # Close the lifecycle immediately so the UI is never left waiting
            # for an END event that would never arrive.
            logger.warning(
                "Tool call args for %s are not JSON-serialisable — emitting error end event: %s",
                tool_name,
                exc,
            )
            error_sse = self.emit(
                AGUIEventType.TOOL_CALL_END,
                {"callId": call_id, "error": f"Args not serialisable: {exc}"},
            )
            return [start_sse, error_sse]
        args_sse = self.emit(
            AGUIEventType.TOOL_CALL_ARGS,
            {"callId": call_id, "args": args_json},
        )
        end_sse = self.emit(
            AGUIEventType.TOOL_CALL_END,
            {"callId": call_id},
        )
        return [start_sse, args_sse, end_sse]

    # -- State, raw, and custom helpers ----------------------------------------

    def emit_state_snapshot(self, state: dict[str, Any]) -> str:
        """Emit a STATE_SNAPSHOT event with a complete state payload."""
        return self.emit(AGUIEventType.STATE_SNAPSHOT, {"state": state})

    def emit_state_delta(self, delta: dict[str, Any]) -> str:
        """Emit a STATE_DELTA event with an incremental state payload."""
        return self.emit(AGUIEventType.STATE_DELTA, {"delta": delta})

    def emit_messages_snapshot(self, messages: list[dict[str, Any]]) -> str:
        """Emit a MESSAGES_SNAPSHOT event with the current message list."""
        return self.emit(AGUIEventType.MESSAGES_SNAPSHOT, {"messages": messages})

    def emit_raw(self, payload: dict[str, Any]) -> str:
        """Emit a RAW event with an unwrapped AG-UI payload."""
        return self.emit(AGUIEventType.RAW, payload)

    def emit_custom(self, name: str, value: Any) -> str:
        """Emit a CUSTOM event with a name and arbitrary value."""
        return self.emit(AGUIEventType.CUSTOM, {"name": name, "value": value})

    def emit_metadata(self, metadata: dict[str, Any]) -> str:
        """Emit a METADATA event with caller-provided metadata."""
        return self.emit(AGUIEventType.METADATA, {"metadata": metadata})

    # ── Step helpers ──────────────────────────────────────────────────────────

    @contextmanager
    def emit_step(self, step_name: str) -> Generator[None, None, None]:
        """Context manager that emits STEP_STARTED, optional RUN_ERROR, and STEP_FINISHED events.

        On success the ``STEP_FINISHED`` payload carries ``status="ok"``.
        On any exception (including ``KeyboardInterrupt`` and ``GeneratorExit``)
        a ``RUN_ERROR`` event is emitted first, then ``STEP_FINISHED`` carries
        ``status="failed"`` and an ``error`` field with the exception description.
        The exception is always re-raised so the caller is not silently swallowed.

        Using ``BaseException`` (not ``Exception``) ensures that
        ``KeyboardInterrupt``, ``SystemExit``, and generator cancellation
        (``GeneratorExit``) all produce a ``RUN_ERROR`` signal rather than
        leaving the UI in an indeterminate state.

        Usage::

            with emitter.emit_step("compile"):
                # ... execute the step ...
                pass

        Args:
            step_name: Human-readable name of the pipeline step.

        Yields:
            None — the caller performs the step body inside the ``with`` block.

        Raises:
            BaseException: Re-raises any exception raised inside the ``with``
                block.  The emitter first publishes ``RUN_ERROR`` + a failing
                ``STEP_FINISHED`` so the UI observes the failure, then the
                original exception propagates to the caller.
        """
        step_id = str(uuid.uuid4())
        self.emit(
            AGUIEventType.STEP_STARTED,
            {"stepId": step_id, "stepName": step_name},
        )
        # Captured on failure so the finally block can include it in STEP_FINISHED.
        failure_message: str | None = None
        try:
            yield
        except BaseException as exc:
            failure_message = f"{type(exc).__name__}: {exc}"
            self.emit(
                AGUIEventType.RUN_ERROR,
                {"stepId": step_id, "stepName": step_name, "error": failure_message},
            )
            raise
        finally:
            payload: dict[str, Any] = {"stepId": step_id, "stepName": step_name}
            if failure_message is not None:
                payload["status"] = "failed"
                payload["error"] = failure_message
            else:
                payload["status"] = "ok"
            self.emit(AGUIEventType.STEP_FINISHED, payload)

    # ── Run lifecycle helpers ─────────────────────────────────────────────────

    def emit_run_start(self, run_id: str) -> str:
        """Emit a RUN_STARTED event to signal the beginning of an agent run.

        Args:
            run_id: Unique identifier for this agent run.

        Returns:
            SSE string for the emitted event.
        """
        return self.emit(
            AGUIEventType.RUN_STARTED,
            {"runId": run_id},
        )

    def emit_run_finish(self, run_id: str) -> str:
        """Emit a RUN_FINISHED event to signal successful completion.

        Args:
            run_id: Identifier of the run being closed.

        Returns:
            SSE string for the emitted event.
        """
        return self.emit(
            AGUIEventType.RUN_FINISHED,
            {"runId": run_id},
        )

    def emit_error(self, error_message: str) -> str:
        """Emit a RUN_ERROR event to signal an agent run failure.

        Args:
            error_message: Human-readable description of the error.

        Returns:
            SSE string for the emitted event.
        """
        return self.emit(
            AGUIEventType.RUN_ERROR,
            {"error": error_message},
        )

    # ── Buffer management ─────────────────────────────────────────────────────

    def get_events(self) -> list[AGUIEvent]:
        """Return all buffered events in emission order.

        Returns:
            Ordered list of buffered :class:`AGUIEvent` objects.
        """
        return list(self._events)

    def clear(self) -> None:
        """Clear all buffered events from the emitter."""
        count = len(self._events)
        self._events.clear()
        logger.debug("AGUIEventEmitter cleared %d buffered events", count)

    def to_stream_chunks(self) -> list[StreamChunk]:
        """Convert all buffered events to :class:`StreamChunk` objects.

        Each AG-UI event becomes one :class:`StreamChunk`.  The final event
        in the buffer has ``is_final=True``.

        Returns:
            Ordered list of :class:`StreamChunk` objects, one per buffered
            event.  Returns an empty list if no events have been buffered.
        """
        if not self._events:
            return []

        chunks: list[StreamChunk] = []
        last_index = len(self._events) - 1
        for i, event in enumerate(self._events):
            chunks.append(
                event.to_stream_chunk(
                    chunk_index=i,
                    is_final=(i == last_index),
                ),
            )
        logger.debug("Converted %d AG-UI events to StreamChunks", len(chunks))
        return chunks
