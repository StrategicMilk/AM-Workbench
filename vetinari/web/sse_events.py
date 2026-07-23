"""Typed SSE Event Dataclasses and dual-delivery contract.

Replaces 30+ ad-hoc dict constructions with frozen dataclasses
for each SSE event type emitted by the Vetinari web layer.

Each event has a ``to_sse()`` method that returns the dict expected
by ``_push_sse_event(project_id, event_type, data)``.

Usage::

    from vetinari.web.sse_events import TaskStartEvent
    event = TaskStartEvent(task_id="t1", description="Build auth")
    _push_sse_event(project_id, event.event_type, event.to_sse())

Dual SSE Delivery Contract
--------------------------
Every SSE event published via ``_push_sse_event`` travels two paths:

**Live queue** (ephemeral):
    An in-memory ``queue.Queue`` in ``vetinari.web.shared`` delivers events
    in real-time to connected SSE clients.  The queue is **ephemeral** — its
    contents are lost on process restart or client disconnect.  It exists
    solely to minimise latency for live subscribers.

**sse_event_log** (durable):
    Simultaneously, every event is written to the ``sse_event_log`` SQLite
    table (columns: ``id``, ``project_id``, ``event_type``, ``payload_json``,
    ``sequence_num``, ``emitted_at``).  This store **survives process
    restarts** and is the source of truth for replay.

**Replay endpoint**:
    Reconnecting clients that missed events while disconnected call
    ``GET /api/v1/projects/{project_id}/events/replay?after_sequence=N``
    to fetch all persisted events with ``sequence_num > N``.  The
    ``N`` value comes from the SSE ``id:`` field sent with every live event
    (browsers expose it as ``EventSource.lastEventId``).

**Log-type events** (ThinkingEvent, DecisionEvent, ErrorEvent):
    These are persisted to ``sse_event_log`` via the same
    ``_push_sse_event`` path and are therefore fully replayable, even
    though they are not tied to task lifecycle milestones.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from itertools import count
from typing import Any, ClassVar

from vetinari.web.sse_event_store import (
    _persist_sse_event as _persist_sse_event,
)
from vetinari.web.sse_event_store import (
    cleanup_old_sse_events as cleanup_old_sse_events,
)
from vetinari.web.sse_event_store import (
    cleanup_stale_sse_events as cleanup_stale_sse_events,
)
from vetinari.web.sse_event_store import (
    get_recent_sse_events as get_recent_sse_events,
)

logger = logging.getLogger(__name__)


class _AtomicSequence:
    """Lock-backed monotonic sequence for SSE event IDs."""

    def __init__(self, start: int = 1) -> None:
        self._counter = count(start)
        self._lock = threading.Lock()

    def __next__(self) -> int:
        with self._lock:
            return next(self._counter)


_SSE_EVENT_SEQUENCE = _AtomicSequence(1)


def _durable_global_sse_sequence_floor() -> int:
    """Return the highest persisted SSE sequence across projects, or zero."""
    try:
        from vetinari.database import get_connection

        conn = get_connection()
        row = conn.execute("SELECT COALESCE(MAX(sequence_num), 0) AS max_sequence FROM sse_event_log").fetchone()
        if row is None:
            return 0
        if hasattr(row, "keys") and "max_sequence" in row:
            return int(row["max_sequence"])
        return int(row[0])
    except Exception:
        logger.warning("Failed to read durable typed SSE sequence floor", exc_info=True)
        return 0


def reseed_sse_event_sequence_from_store() -> int:
    """Restart typed SSE event IDs after the durable log's current maximum.

    Returns:
        Durable sequence floor used to seed the in-process counter.
    """
    global _SSE_EVENT_SEQUENCE
    floor = _durable_global_sse_sequence_floor()
    _SSE_EVENT_SEQUENCE = _AtomicSequence(floor + 1)
    return floor


def _next_sse_sequence() -> int:
    """Return the next monotonically increasing SSE event sequence number."""
    return next(_SSE_EVENT_SEQUENCE)


class SseEvent:
    """Shared serializer for typed SSE event payload dataclasses."""

    _payload_fields: ClassVar[tuple[str, ...]] = ()
    _payload_constants: ClassVar[dict[str, Any]] = {}

    def to_sse(self) -> dict[str, Any]:
        """Serialize to the SSE data payload expected by the web event queue.

        Returns:
            Value produced for the caller.
        """
        payload = {field_name: getattr(self, field_name) for field_name in self._payload_fields}
        payload.update(self._payload_constants)
        return payload


# -- Lifecycle events -------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StatusEvent(SseEvent):
    """Pipeline status update (running, idle, etc.)."""

    event_type: str = "status"
    status: str = ""
    total_tasks: int = 0

    _payload_fields: ClassVar[tuple[str, ...]] = ("status", "total_tasks")

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}("
            f"event_type={self.event_type!r}, "
            f"status={self.status!r}, "
            f"total_tasks={self.total_tasks!r}, "
            f"_payload_fields={self._payload_fields!r}"
            ")"
        )


@dataclass(frozen=True, slots=True)
class PlanningStartEvent(SseEvent):
    """Plan generation has begun."""

    event_type: str = "planning_started"
    goal: str = ""
    plan_id: str = ""

    _payload_fields: ClassVar[tuple[str, ...]] = ("goal", "plan_id")

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}("
            f"event_type={self.event_type!r}, "
            f"goal={self.goal!r}, "
            f"plan_id={self.plan_id!r}, "
            f"_payload_fields={self._payload_fields!r}"
            ")"
        )


@dataclass(frozen=True, slots=True)
class PausedEvent(SseEvent):
    """Pipeline execution paused."""

    event_type: str = "paused"
    project_id: str = ""

    _payload_fields: ClassVar[tuple[str, ...]] = ("project_id",)
    _payload_constants: ClassVar[dict[str, Any]] = {"status": "paused"}

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}("
            f"event_type={self.event_type!r}, "
            f"project_id={self.project_id!r}, "
            f"_payload_fields={self._payload_fields!r}, "
            f"_payload_constants={self._payload_constants!r}"
            ")"
        )


@dataclass(frozen=True, slots=True)
class ResumedEvent(SseEvent):
    """Pipeline execution resumed."""

    event_type: str = "resumed"
    project_id: str = ""

    _payload_fields: ClassVar[tuple[str, ...]] = ("project_id",)
    _payload_constants: ClassVar[dict[str, Any]] = {"status": "resumed"}

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}("
            f"event_type={self.event_type!r}, "
            f"project_id={self.project_id!r}, "
            f"_payload_fields={self._payload_fields!r}, "
            f"_payload_constants={self._payload_constants!r}"
            ")"
        )


@dataclass(frozen=True, slots=True)
class CancelledEvent(SseEvent):
    """Pipeline execution cancelled."""

    event_type: str = "cancelled"
    project_id: str = ""
    reason: str = ""

    _payload_fields: ClassVar[tuple[str, ...]] = ("project_id", "reason")

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}("
            f"event_type={self.event_type!r}, "
            f"project_id={self.project_id!r}, "
            f"reason={self.reason!r}, "
            f"_payload_fields={self._payload_fields!r}"
            ")"
        )


# -- Task events ------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TaskStartEvent(SseEvent):
    """A task has started execution."""

    event_type: str = "task_started"
    sequence: int = field(default_factory=_next_sse_sequence)
    task_id: str = ""
    description: str = ""
    agent_type: str = ""
    task_index: int = 0
    total_tasks: int = 0

    def __repr__(self) -> str:
        return (
            f"TaskStartEvent(task_id={self.task_id!r}, agent={self.agent_type!r}, {self.task_index}/{self.total_tasks})"
        )

    _payload_fields: ClassVar[tuple[str, ...]] = (
        "sequence",
        "task_id",
        "description",
        "agent_type",
        "task_index",
        "total_tasks",
    )


@dataclass(frozen=True, slots=True)
class TaskCompleteEvent(SseEvent):
    """A task has completed successfully."""

    event_type: str = "task_completed"
    sequence: int = field(default_factory=_next_sse_sequence)
    task_id: str = ""
    output_summary: str = ""
    task_index: int = 0
    total_tasks: int = 0

    def __repr__(self) -> str:
        return f"TaskCompleteEvent(task_id={self.task_id!r}, {self.task_index}/{self.total_tasks})"

    _payload_fields: ClassVar[tuple[str, ...]] = ("sequence", "task_id", "output_summary", "task_index", "total_tasks")


@dataclass(frozen=True, slots=True)
class TaskFailedEvent(SseEvent):
    """A task has failed."""

    event_type: str = "task_failed"
    sequence: int = field(default_factory=_next_sse_sequence)
    task_id: str = ""
    error: str = ""
    task_index: int = 0
    total_tasks: int = 0

    def __repr__(self) -> str:
        return f"TaskFailedEvent(task_id={self.task_id!r}, error={self.error!r})"

    _payload_fields: ClassVar[tuple[str, ...]] = ("sequence", "task_id", "error", "task_index", "total_tasks")


@dataclass(frozen=True, slots=True)
class TaskCancelledEvent(SseEvent):
    """A task has been cancelled."""

    event_type: str = "task_cancelled"
    sequence: int = field(default_factory=_next_sse_sequence)
    task_id: str = ""
    reason: str = ""

    def __repr__(self) -> str:
        return f"TaskCancelledEvent(task_id={self.task_id!r}, reason={self.reason!r})"

    _payload_fields: ClassVar[tuple[str, ...]] = ("sequence", "task_id", "reason")


@dataclass(frozen=True, slots=True)
class TaskRerunEvent(SseEvent):
    """A task is being re-run (retry)."""

    event_type: str = "task_rerun"
    sequence: int = field(default_factory=_next_sse_sequence)
    task_id: str = ""
    attempt: int = 1

    def __repr__(self) -> str:
        return f"TaskRerunEvent(task_id={self.task_id!r}, attempt={self.attempt})"

    _payload_fields: ClassVar[tuple[str, ...]] = ("sequence", "task_id", "attempt")


# -- Stage events -----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StageStartEvent(SseEvent):
    """A pipeline stage has started."""

    event_type: str = "stage_started"
    sequence: int = field(default_factory=_next_sse_sequence)
    stage: str = ""
    stage_index: int = 0
    total_stages: int = 0

    def __repr__(self) -> str:
        return f"StageStartEvent(stage={self.stage!r}, {self.stage_index}/{self.total_stages})"

    _payload_fields: ClassVar[tuple[str, ...]] = ("sequence", "stage", "stage_index", "total_stages")


@dataclass(frozen=True, slots=True)
class StageProgressEvent(SseEvent):
    """Progress update within a pipeline stage."""

    event_type: str = "stage_progress"
    sequence: int = field(default_factory=_next_sse_sequence)
    stage: str = ""
    progress: float = 0.0
    message: str = ""

    def __repr__(self) -> str:
        return f"StageProgressEvent(stage={self.stage!r}, progress={self.progress:.0%})"

    _payload_fields: ClassVar[tuple[str, ...]] = ("sequence", "stage", "progress", "message")


@dataclass(frozen=True, slots=True)
class StageCompleteEvent(SseEvent):
    """A pipeline stage has completed."""

    event_type: str = "stage_completed"
    sequence: int = field(default_factory=_next_sse_sequence)
    stage: str = ""
    output_summary: str = ""

    def __repr__(self) -> str:
        return f"StageCompleteEvent(stage={self.stage!r}, summary={self.output_summary!r})"

    _payload_fields: ClassVar[tuple[str, ...]] = ("sequence", "stage", "output_summary")


@dataclass(frozen=True, slots=True)
class PipelineStageEvent(SseEvent):
    """Pipeline stage status snapshot for dashboard visualization."""

    event_type: str = "pipeline_stage"
    stage: str = ""
    status: str = "idle"  # idle | active | complete | failed
    entry_count: int = 0
    exit_count: int = 0

    def __repr__(self) -> str:
        return f"PipelineStageEvent(stage={self.stage!r}, status={self.status!r}, entry={self.entry_count}, exit={self.exit_count})"

    _payload_fields: ClassVar[tuple[str, ...]] = ("stage", "status", "entry_count", "exit_count")


# -- Agent/model events -----------------------------------------------------


@dataclass(frozen=True, slots=True)
class ThinkingEvent(SseEvent):
    """Agent thinking/reasoning status."""

    event_type: str = "thinking"
    agent_type: str = ""
    message: str = ""

    _payload_fields: ClassVar[tuple[str, ...]] = ("agent_type", "message")

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}("
            f"event_type={self.event_type!r}, "
            f"agent_type={self.agent_type!r}, "
            f"message={self.message!r}, "
            f"_payload_fields={self._payload_fields!r}"
            ")"
        )


@dataclass(frozen=True, slots=True)
class DecisionEvent(SseEvent):
    """Agent decision notification."""

    event_type: str = "decision"
    decision_type: str = ""
    summary: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    def __repr__(self) -> str:
        return f"DecisionEvent(type={self.decision_type!r}, summary={self.summary!r})"

    _payload_fields: ClassVar[tuple[str, ...]] = ("decision_type", "summary", "details")


@dataclass(frozen=True, slots=True)
class ModelLoadingEvent(SseEvent):
    """A model is being loaded for inference."""

    event_type: str = "model_loading"
    model_id: str = ""
    status: str = "loading"

    _payload_fields: ClassVar[tuple[str, ...]] = ("model_id", "status")

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}("
            f"event_type={self.event_type!r}, "
            f"model_id={self.model_id!r}, "
            f"status={self.status!r}, "
            f"_payload_fields={self._payload_fields!r}"
            ")"
        )


@dataclass(frozen=True, slots=True)
class ModelRecommendationEvent(SseEvent):
    """Model selection recommendation."""

    event_type: str = "model_recommendation"
    recommended_model: str = ""
    reason: str = ""

    _payload_fields: ClassVar[tuple[str, ...]] = ("recommended_model", "reason")

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}("
            f"event_type={self.event_type!r}, "
            f"recommended_model={self.recommended_model!r}, "
            f"reason={self.reason!r}, "
            f"_payload_fields={self._payload_fields!r}"
            ")"
        )


@dataclass(frozen=True, slots=True)
class EtaUpdateEvent(SseEvent):
    """Estimated time of arrival update."""

    event_type: str = "eta_update"
    remaining_seconds: float = 0.0
    message: str = ""

    _payload_fields: ClassVar[tuple[str, ...]] = ("remaining_seconds", "message")

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}("
            f"event_type={self.event_type!r}, "
            f"remaining_seconds={self.remaining_seconds!r}, "
            f"message={self.message!r}, "
            f"_payload_fields={self._payload_fields!r}"
            ")"
        )


# -- Error events -----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ErrorEvent(SseEvent):
    """General error notification."""

    event_type: str = "error"
    error: str = ""
    error_type: str = ""
    recoverable: bool = True

    def __repr__(self) -> str:
        return f"ErrorEvent(type={self.error_type!r}, recoverable={self.recoverable}, error={self.error!r})"

    _payload_fields: ClassVar[tuple[str, ...]] = ("error", "error_type", "recoverable")


# -- Quality events ---------------------------------------------------------


@dataclass(frozen=True, slots=True)
class QualityResultEvent(SseEvent):
    """Quality scoring result from Inspector review."""

    event_type: str = "quality_result"
    project_id: str = ""
    quality_score: float = 0.0
    passed: bool = False
    issues_count: int = 0
    confidence: float = 0.0

    def __repr__(self) -> str:
        """Show key identifying fields for debugging."""
        return (
            f"QualityResultEvent(project_id={self.project_id!r}, score={self.quality_score!r}, passed={self.passed!r})"
        )

    _payload_fields: ClassVar[tuple[str, ...]] = ("project_id", "quality_score", "passed", "issues_count", "confidence")


# -- Training events --------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TrainingCompleteEvent(SseEvent):
    """Training run completed successfully."""

    event_type: str = "training_completed"
    run_id: str = ""
    summary: str = ""

    _payload_fields: ClassVar[tuple[str, ...]] = ("run_id", "summary")

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}("
            f"event_type={self.event_type!r}, "
            f"run_id={self.run_id!r}, "
            f"summary={self.summary!r}, "
            f"_payload_fields={self._payload_fields!r}"
            ")"
        )


@dataclass(frozen=True, slots=True)
class TrainingFailedEvent(SseEvent):
    """Training run failed."""

    event_type: str = "training_failed"
    error: str = ""

    _payload_fields: ClassVar[tuple[str, ...]] = ("error",)


# -- Notification events ----------------------------------------------------


@dataclass(frozen=True, slots=True)
class NotificationEvent(SseEvent):
    """A notification dispatched via the notification manager."""

    event_type: str = "notification"
    notification_id: str = ""
    title: str = ""
    body: str = ""
    priority: str = ""
    action_type: str = ""

    def __repr__(self) -> str:
        return "NotificationEvent(...)"

    _payload_fields: ClassVar[tuple[str, ...]] = ("notification_id", "title", "body", "priority", "action_type")


@dataclass(frozen=True, slots=True)
class ApprovalRequestEvent(SseEvent):
    """An action has been queued for human approval."""

    event_type: str = "approval_requested"
    action_id: str = ""
    action_type: str = ""
    confidence: float = 0.0

    def __repr__(self) -> str:
        return "ApprovalRequestEvent(...)"

    _payload_fields: ClassVar[tuple[str, ...]] = ("action_id", "action_type", "confidence")


# -- Registry ---------------------------------------------------------------

# All event classes keyed by their event_type for programmatic lookup
SSE_EVENT_REGISTRY: dict[str, type] = {
    "status": StatusEvent,
    "planning_started": PlanningStartEvent,
    "paused": PausedEvent,
    "resumed": ResumedEvent,
    "cancelled": CancelledEvent,
    "task_started": TaskStartEvent,
    "task_completed": TaskCompleteEvent,
    "task_failed": TaskFailedEvent,
    "task_cancelled": TaskCancelledEvent,
    "task_rerun": TaskRerunEvent,
    "stage_started": StageStartEvent,
    "stage_progress": StageProgressEvent,
    "stage_completed": StageCompleteEvent,
    "pipeline_stage": PipelineStageEvent,
    "thinking": ThinkingEvent,
    "decision": DecisionEvent,
    "model_loading": ModelLoadingEvent,
    "model_recommendation": ModelRecommendationEvent,
    "eta_update": EtaUpdateEvent,
    "error": ErrorEvent,
    "quality_result": QualityResultEvent,
    "training_completed": TrainingCompleteEvent,
    "training_failed": TrainingFailedEvent,
    "notification": NotificationEvent,
    "approval_requested": ApprovalRequestEvent,
}
