"""Context-managed streaming iterators for the AM Engine HTTP client."""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from contextlib import AbstractContextManager
from typing import TYPE_CHECKING, Any

import httpx

from vetinari.engine.client_types import (
    ChatStreamEvent,
    EngineEventLagError,
    EngineProtocolError,
    EngineStreamError,
)
from vetinari.exceptions import InferenceError

if TYPE_CHECKING:
    from vetinari.engine.client import EngineClient


class EngineStream(AbstractContextManager["EngineStream"], Iterator[ChatStreamEvent]):
    """Context-managed SSE iterator whose close aborts the HTTP response."""

    def __init__(self, client: EngineClient, payload: Mapping[str, Any], *, request_id: str) -> None:
        self._client = client
        self._payload = dict(payload)
        self._request_id = request_id
        self._context: Any | None = None
        self._response: httpx.Response | None = None
        self._lines: Iterator[str] | None = None
        self._closed = False

    @property
    def request_id(self) -> str:
        """Return the stable request identity available before stream entry.

        Returns:
            Caller-owned identifier sent to the engine and accepted by ``cancel``.
        """
        return self._request_id

    def __enter__(self) -> EngineStream:
        if self._closed:
            raise EngineStreamError("engine stream is already closed")
        if self._response is None:
            try:
                self._context, self._response = self._client._open_stream(
                    self._payload,
                    request_id=self._request_id,
                )
                self._lines = self._response.iter_lines()
            except httpx.TransportError as exc:
                self.close()
                raise EngineStreamError("engine SSE stream disconnected") from exc
        return self

    def __next__(self) -> ChatStreamEvent:
        if self._closed:
            raise StopIteration
        if self._response is None:
            self.__enter__()
        assert self._lines is not None
        try:
            while True:
                line = next(self._lines)
                if not line or line.startswith(":"):
                    continue
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    self.close()
                    raise StopIteration
                try:
                    decoded = json.loads(data)
                except json.JSONDecodeError as exc:
                    raise EngineProtocolError("engine SSE event must be valid JSON") from exc
                if not isinstance(decoded, Mapping):
                    raise EngineProtocolError("engine SSE event must be a JSON object")
                self._client._validate_payload(decoded)
                self._client._raise_envelope(decoded)
                return ChatStreamEvent(payload=dict(decoded))
        except StopIteration as exc:
            completed = self._closed
            self.close()
            if completed:
                raise
            raise EngineStreamError("engine SSE stream disconnected before completion") from exc
        except (httpx.HTTPError, EngineProtocolError, InferenceError) as exc:
            self.close()
            if isinstance(exc, (EngineProtocolError, InferenceError)):
                raise
            raise EngineStreamError("engine SSE stream disconnected") from exc

    def close(self) -> None:
        """Close the response immediately, propagating disconnect cancellation."""
        if self._closed:
            return
        self._closed = True
        if self._response is not None:
            self._response.close()
        if self._context is not None:
            self._context.__exit__(None, None, None)

    def cancel(self) -> Mapping[str, Any]:
        """Request server-side cancellation and close the local response.

        Returns:
            Versioned cancellation acknowledgement from the engine.

        Raises:
            InferenceError: If the cancellation request fails.
        """
        try:
            return self._client.cancel(self._request_id)
        finally:
            self.close()

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()


class EngineEventsStream(AbstractContextManager["EngineEventsStream"], Iterator[dict[str, Any]]):
    """Context-managed NDJSON event iterator whose close aborts the HTTP response."""

    def __init__(
        self,
        client: EngineClient,
        *,
        generation: str | None = None,
        after_cursor: int | None = None,
    ) -> None:
        self._client = client
        self._resume_generation = generation
        self._resume_cursor = after_cursor
        self._context: Any | None = None
        self._response: httpx.Response | None = None
        self._lines: Iterator[str] | None = None
        self._closed = False
        self.generation: str | None = None
        self.start_cursor: int | None = None
        self.last_cursor: int | None = None

    def __enter__(self) -> EngineEventsStream:
        if self._closed:
            raise EngineStreamError("engine event stream is already closed")
        if self._response is None:
            try:
                self._context, self._response = self._client._open_events_stream(
                    generation=self._resume_generation,
                    after_cursor=self._resume_cursor,
                )
                generation = self._response.headers.get("x-engine-event-generation")
                raw_cursor = self._response.headers.get("x-engine-event-start-cursor")
                if not generation or raw_cursor is None:
                    raise EngineProtocolError("engine event stream is missing generation or starting cursor metadata")
                try:
                    start_cursor = int(raw_cursor)
                except ValueError as exc:
                    raise EngineProtocolError("engine event stream starting cursor is malformed") from exc
                if start_cursor < 0:
                    raise EngineProtocolError("engine event stream starting cursor is negative")
                self.generation = generation
                self.start_cursor = start_cursor
                self.last_cursor = start_cursor
                self._lines = self._response.iter_lines()
            except (httpx.TransportError, EngineProtocolError) as exc:
                self.close()
                if isinstance(exc, EngineProtocolError):
                    raise
                raise EngineStreamError("engine NDJSON event stream disconnected") from exc
        return self

    def __next__(self) -> dict[str, Any]:
        if self._closed:
            raise StopIteration
        if self._response is None:
            self.__enter__()
        assert self._lines is not None
        try:
            while True:
                line = next(self._lines)
                if not line.strip():
                    continue
                try:
                    decoded = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise EngineProtocolError("engine NDJSON event must be valid JSON") from exc
                if not isinstance(decoded, Mapping):
                    raise EngineProtocolError("engine NDJSON event must be a JSON object")
                transport_error = decoded.get("transport_error")
                if transport_error is not None:
                    if not isinstance(transport_error, Mapping) or transport_error.get("code") != "lagged":
                        raise EngineProtocolError("engine event stream returned malformed transport metadata")
                    raise EngineEventLagError(
                        "engine event replay cursor fell behind retained history",
                        generation=self.generation,
                        cursor=self.last_cursor,
                        missed=transport_error.get("missed"),
                    )
                if self.last_cursor is None:
                    raise EngineProtocolError("engine event cursor was not initialized")
                self.last_cursor += 1
                return dict(decoded)
        except StopIteration as exc:
            self.close()
            raise EngineStreamError("engine NDJSON event stream disconnected") from exc
        except (EngineProtocolError, EngineEventLagError):
            self.close()
            raise
        except httpx.HTTPError as exc:
            self.close()
            raise EngineStreamError("engine NDJSON event stream disconnected") from exc

    def close(self) -> None:
        """Close the event response immediately and idempotently."""
        if self._closed:
            return
        self._closed = True
        if self._response is not None:
            self._response.close()
        if self._context is not None:
            self._context.__exit__(None, None, None)

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()


__all__ = ["EngineEventsStream", "EngineStream"]
