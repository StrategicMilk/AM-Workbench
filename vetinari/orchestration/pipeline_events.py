"""Pipeline event protocol for decoupled stage notifications.

Defines the PipelineEventHandler protocol that allows the TwoLayerOrchestrator
to emit structured stage events without knowing who consumes them. The web SSE
handler, CLI, tests, and any future consumer implement this protocol.

This eliminates the 590-line reimplementation in projects_api.py by allowing
the orchestrator to drive all pipeline logic while the web layer only handles
event display (ADR-0072).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


class PipelineStage(Enum):
    """Stage in the orchestration pipeline.

    Each value corresponds to a logical step in the assembly-line pattern
    implemented by TwoLayerOrchestrator.
    """

    INTAKE = "intake"
    PREVENTION = "prevention"
    PLAN_GEN = "plan_gen"
    MODEL_ASSIGN = "model_assign"
    EXECUTION = "execution"
    REFINEMENT = "refinement"
    REVIEW = "review"
    VERIFICATION = "verification"
    ASSEMBLY = "assembly"


class EventSeverity(Enum):
    """Severity level for pipeline events."""

    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class PipelineEvent:
    """A single event emitted during pipeline execution.

    Carries enough context for any consumer to render meaningful status
    updates without coupling to the orchestrator's internals.

    Args:
        stage: Which pipeline stage emitted this event.
        event_type: Discriminator within the stage (e.g. ``"stage_started"``,
            ``"task_completed"``, ``"error"``).
        data: Arbitrary payload — consumers should tolerate missing keys.
        timestamp: Wall-clock time (``time.time()``) when the event was created.
        severity: How important this event is for display/alerting.
    """

    stage: PipelineStage
    event_type: str
    data: dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)
    severity: EventSeverity = EventSeverity.INFO

    def __repr__(self) -> str:
        return (
            f"PipelineEvent(stage={self.stage.value!r}, "
            f"event_type={self.event_type!r}, "
            f"severity={self.severity.value!r})"
        )


# ---------------------------------------------------------------------------
# Handler protocol + implementations
# ---------------------------------------------------------------------------


@runtime_checkable
class PipelineEventHandler(Protocol):
    """Protocol for objects that receive pipeline stage events.

    Implementations may forward events to SSE streams, log files,
    metrics backends, or simply discard them (NullEventHandler).
    """

    def on_event(self, event: PipelineEvent) -> None:
        """Handle a single pipeline event.

        Implementations MUST NOT raise exceptions — failures should be
        logged internally. The orchestrator will not retry event delivery.

        Args:
            event: The pipeline event to handle.
        """
        ...


class NullEventHandler:
    """No-op handler for CLI usage and tests where events are discarded.

    Satisfies the PipelineEventHandler protocol without any side effects.
    """

    def on_event(self, event: PipelineEvent) -> None:
        """Discard the event silently.

        Args:
            event: The pipeline event (ignored).
        """


class LoggingEventHandler:
    """Handler that logs every pipeline event at the appropriate level.

    Useful for CLI mode where events should appear in structured logs
    but not be forwarded to any streaming consumer.
    """

    def on_event(self, event: PipelineEvent) -> None:
        """Log the event at the severity-appropriate level.

        Args:
            event: The pipeline event to log.
        """
        _level_map = {
            EventSeverity.DEBUG: logging.DEBUG,
            EventSeverity.INFO: logging.INFO,
            EventSeverity.WARNING: logging.WARNING,
            EventSeverity.ERROR: logging.ERROR,
        }
        level = _level_map.get(event.severity, logging.INFO)
        logger.log(
            level,
            "[Pipeline:%s] %s — %s",
            event.stage.value,
            event.event_type,
            {k: v for k, v in event.data.items() if k != "_internal"},
        )


class CompositeEventHandler:
    """Fan-out handler that forwards events to multiple child handlers.

    Exceptions in any child handler are caught and logged so that one
    failing handler does not prevent others from receiving the event.

    Args:
        handlers: Variable number of PipelineEventHandler implementations.
    """

    def __init__(self, *handlers: PipelineEventHandler) -> None:
        self._handlers: list[PipelineEventHandler] = list(handlers)

    def add_handler(self, handler: PipelineEventHandler) -> None:
        """Append a handler to the fan-out list.

        Args:
            handler: A PipelineEventHandler implementation.
        """
        self._handlers.append(handler)

    def on_event(self, event: PipelineEvent) -> None:
        """Forward the event to every registered child handler.

        Args:
            event: The pipeline event to forward.
        """
        for handler in self._handlers:
            try:
                handler.on_event(event)
            except Exception:
                logger.exception(
                    "CompositeEventHandler child %s failed for event %s",
                    type(handler).__name__,
                    event.event_type,
                )
