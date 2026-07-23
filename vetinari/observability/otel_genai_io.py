"""I/O, export, and reset helpers for GenAITracer."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from copy import deepcopy
from pathlib import Path
from typing import TYPE_CHECKING, Any

from vetinari.constants import TRUNCATE_OTEL_OUTPUT
from vetinari.logging_context import clear_span_id, clear_trace_id

logger = logging.getLogger(__name__)


class _GenAITraceIOMixin:
    """Tool-call recording, export, stats, and reset methods."""

    if TYPE_CHECKING:
        _active: Any
        _lock: Any
        _spans: Any
        _total_tokens: Any

    def record_tool_call(
        self,
        span: Any,
        tool_name: str,
        input_data: str,
        output_data: str,
        *,
        redact_fn: Callable[[str], str] | None = None,
    ) -> None:
        """Append a tool-call event to an open span.

        Args:
            span: The parent :class:`SpanContext`.
            tool_name: Name of the tool (e.g. ``"code_search"``).
            input_data: Serialised input passed to the tool.
            output_data: Serialised output returned from the tool.
            redact_fn: Optional sanitizer applied before truncation and storage.
        """
        from vetinari.observability import otel_genai as owner

        safe_input = redact_fn(input_data) if redact_fn is not None else input_data
        safe_output = redact_fn(output_data) if redact_fn is not None else output_data
        event: dict[str, Any] = {
            "name": "gen_ai.tool.call",
            "timestamp": time.monotonic(),
            "attributes": {
                owner.ATTR_TOOL_NAME: tool_name,
                owner.ATTR_TOOL_INPUT: safe_input[:TRUNCATE_OTEL_OUTPUT],
                owner.ATTR_TOOL_OUTPUT: safe_output[:TRUNCATE_OTEL_OUTPUT],
            },
        }
        span.events.append(event)
        logger.debug("Tool call recorded: tool=%s span=%s", tool_name, span.span_id)

    def export_traces(self, filepath: str) -> int:
        """Write all completed spans to a JSON file for external ingestion.

        Args:
            filepath: Destination path (created if it does not exist).

        Returns:
            Number of spans exported.

        Raises:
            OSError: If the file cannot be written.
        """
        from vetinari.observability import otel_genai as owner

        with self._lock:
            completed = [deepcopy(s.to_dict()) for s in self._spans]

        _redact_exported_tool_attributes(completed, owner._export_redact_fn)

        output = {
            "schema_version": "1.0",
            "service": "vetinari",
            "convention": "opentelemetry-genai-semconv",
            "exported_at": time.time(),
            "spans": completed,
        }
        Path(filepath).parent.mkdir(parents=True, exist_ok=True)
        with Path(filepath).open("w", encoding="utf-8") as fh:
            json.dump(output, fh, indent=2)

        logger.info("Exported %d GenAI spans to %s", len(completed), filepath)
        return len(completed)

    def get_stats(self) -> dict[str, Any]:
        """Return aggregate tracer statistics.

        Returns:
            Dictionary with ``total_spans``, ``active_spans``, and
            ``total_tokens`` keys.
        """
        with self._lock:
            return {
                "total_spans": len(self._spans),
                "active_spans": len(self._active),
                "total_tokens": self._total_tokens,
            }

    def reset(self) -> None:
        """Clear all recorded spans (intended for testing).

        Resets the per-task trace ID so the next call to start_agent_span
        allocates a fresh trace ID for this async task / thread.
        """
        from vetinari.observability import otel_genai as owner

        with self._lock:
            self._spans.clear()
            self._active.clear()
            self._total_tokens = 0
        # Reset logging_context ContextVars for the calling task/thread only;
        # other concurrent tasks are unaffected.
        clear_trace_id()
        clear_span_id()
        owner._clear_recent_span_correlation()


def _redact_exported_tool_attributes(
    spans: list[dict[str, Any]],
    redact_fn: Callable[[str], str] | None,
) -> None:
    """Redact tool input/output attributes in serialized span dictionaries."""
    if redact_fn is None:
        return
    from vetinari.observability import otel_genai as owner

    for span in spans:
        events = span.get("events", [])
        if not isinstance(events, list):
            continue
        for event in events:
            if not isinstance(event, dict):
                continue
            attributes = event.get("attributes", {})
            if not isinstance(attributes, dict):
                continue
            for attribute_name in (owner.ATTR_TOOL_INPUT, owner.ATTR_TOOL_OUTPUT):
                value = attributes.get(attribute_name)
                if isinstance(value, str):
                    attributes[attribute_name] = redact_fn(value)
