"""SSE event handler — bridges pipeline events to Server-Sent Events.

Implements the PipelineEventHandler protocol from
``vetinari.orchestration.pipeline_events`` and translates each
``PipelineEvent`` into a call to ``_push_sse_event`` for delivery
to the browser via the project's SSE queue.

This handler is the web layer's sole hook into the pipeline — the
orchestrator knows nothing about SSE or Flask; it only calls
``handler.on_event(event)``.
"""

from __future__ import annotations

import logging
from typing import Any

from vetinari.orchestration.pipeline_events import (
    EventSeverity,
    PipelineEvent,
    PipelineStage,
)
from vetinari.web.shared import _push_sse_event

logger = logging.getLogger(__name__)


# Map pipeline stages to human-friendly SSE stage names
_STAGE_SSE_NAME: dict[PipelineStage, str] = {
    PipelineStage.INTAKE: "Analyzing request",
    PipelineStage.PREVENTION: "Running safety checks",
    PipelineStage.PLAN_GEN: "Generating plan",
    PipelineStage.MODEL_ASSIGN: "Assigning models",
    PipelineStage.EXECUTION: "Executing tasks",
    PipelineStage.REFINEMENT: "Refining outputs",
    PipelineStage.REVIEW: "Reviewing quality",
    PipelineStage.VERIFICATION: "Verifying goal compliance",
    PipelineStage.ASSEMBLY: "Assembling final output",
}

# Map pipeline event_type strings to SSE event names (past tense, ADR-0076)
_EVENT_TYPE_MAP: dict[str, str] = {
    "stage_started": "stage_started",
    "stage_completed": "stage_completed",
    "task_started": "task_started",
    "task_completed": "task_completed",
    "task_failed": "task_failed",
    "eta_update": "eta_update",
    "thinking": "thinking",
    "error": "error",
    "paused": "paused",
    "resumed": "resumed",
    "status": "status",
    # Legacy aliases accepted at integration boundaries.
    "stage_start": "stage_started",
    "stage_complete": "stage_completed",
    "task_start": "task_started",
    "task_complete": "task_completed",
}


class SSEEventHandler:
    """Bridge between pipeline events and the SSE stream for a project.

    Each instance is bound to a single ``project_id`` and translates
    incoming ``PipelineEvent`` objects into SSE messages that the
    browser can consume.

    Args:
        project_id: The project whose SSE queue receives events.
    """

    def __init__(self, project_id: str) -> None:
        self._project_id = project_id

    def on_event(self, event: PipelineEvent) -> None:
        """Translate a pipeline event to an SSE push.

        The SSE event name is derived from ``event.event_type`` via
        ``_EVENT_TYPE_MAP``, falling back to the raw event_type string.
        The data payload is the event's ``data`` dict, enriched with
        ``stage`` and ``stage_label`` for UI display.

        Args:
            event: The pipeline event to forward to SSE.
        """
        try:
            sse_event_name = _EVENT_TYPE_MAP.get(event.event_type, event.event_type)
            sse_data: dict[str, Any] = {
                **event.data,
                "stage": event.stage.value,
                "stage_label": _STAGE_SSE_NAME.get(event.stage, event.stage.value),
            }

            # Include severity for errors/warnings so the UI can style them
            if event.severity in (EventSeverity.WARNING, EventSeverity.ERROR):
                sse_data["severity"] = event.severity.value

            _push_sse_event(self._project_id, sse_event_name, sse_data)
        except Exception:
            # Handlers MUST NOT raise — log and continue
            logger.exception(
                "SSEEventHandler failed for project %s event %s",
                self._project_id,
                event.event_type,
            )
