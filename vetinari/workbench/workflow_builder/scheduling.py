"""Scheduling contracts for saved Workbench workflow-builder graphs."""

from __future__ import annotations

import re
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum

from vetinari.workbench.workflow_builder.contracts import WorkflowBuilderError

SCHEDULED_WORKFLOW_CAPABILITY = "workbench_workflow_builder.saved_workflow"
SCHEDULED_WORKFLOW_SOURCE = "workflow_builder"

_CRON_FIELD_COUNT = 5
_SECONDS_PER_MINUTE = 60.0
_SECONDS_PER_HOUR = 60.0 * _SECONDS_PER_MINUTE
_SECONDS_PER_DAY = 24.0 * _SECONDS_PER_HOUR
_SECONDS_PER_WEEK = 7.0 * _SECONDS_PER_DAY
_SAFE_ID = re.compile(r"^[A-Za-z0-9_.-]+$")


class WorkflowScheduleTriggerKind(str, Enum):
    """Scheduler trigger kinds stored as typed contract values."""

    CRON = "cron"
    INTERVAL = "interval"


class WorkflowScheduleNodeType(str, Enum):
    """Workflow-builder node kinds stored as typed contract values."""

    SCHEDULED_WORKFLOW = "scheduled_workflow"


@dataclass(frozen=True, slots=True)
class WorkflowScheduleRequest:
    """Request to register a saved workflow graph with the recurring scheduler."""

    project_id: str
    graph_id: str
    cron_expression: str | None = None
    interval_seconds: float | None = None
    next_run_at: float | None = None
    schedule_id: str | None = None
    name: str | None = None

    def __post_init__(self) -> None:
        _require_safe_id(self.project_id, "project_id")
        _require_safe_id(self.graph_id, "graph_id")
        if self.schedule_id is not None:
            _require_safe_id(self.schedule_id, "schedule_id")
        if self.name is not None and not self.name.strip():
            raise WorkflowBuilderError("schedule-name-invalid")
        if self.cron_expression is not None:
            cron_expression = self.cron_expression.strip()
            if not cron_expression:
                raise WorkflowBuilderError("schedule-cron-empty")
            object.__setattr__(self, "cron_expression", cron_expression)
        has_cron = self.cron_expression is not None
        has_interval = self.interval_seconds is not None
        if has_cron and has_interval:
            raise WorkflowBuilderError("schedule-trigger-ambiguous")
        if not has_cron and not has_interval:
            raise WorkflowBuilderError("schedule-trigger-missing")
        if has_cron and self.next_run_at is not None:
            raise WorkflowBuilderError("schedule-next-run-not-supported-for-cron")
        if self.interval_seconds is not None and self.interval_seconds <= 0:
            raise WorkflowBuilderError("schedule-interval-invalid")
        if self.next_run_at is not None and self.next_run_at < 0:
            raise WorkflowBuilderError("schedule-next-run-invalid")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return (
            "WorkflowScheduleRequest("
            f"project_id={self.project_id!r}, graph_id={self.graph_id!r}, schedule_id={self.schedule_id!r})"
        )


@dataclass(frozen=True, slots=True)
class NormalizedWorkflowSchedule:
    """Scheduler-ready cadence for a saved workflow graph."""

    schedule_id: str
    name: str
    trigger_kind: WorkflowScheduleTriggerKind | str
    interval_seconds: float
    next_run_at: float
    cron_expression: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "trigger_kind", WorkflowScheduleTriggerKind(self.trigger_kind))

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return (
            "NormalizedWorkflowSchedule("
            f"schedule_id={self.schedule_id!r}, trigger_kind={self.trigger_kind!r}, "
            f"next_run_at={self.next_run_at!r})"
        )


@dataclass(frozen=True, slots=True)
class WorkflowScheduleRecord:
    """Workflow-builder view of a recurring scheduler task."""

    schedule_id: str
    project_id: str
    graph_id: str
    name: str
    capability: str
    trigger_kind: WorkflowScheduleTriggerKind | str
    interval_seconds: float
    start_at: float
    next_run_at: float
    cron_expression: str | None = None
    node_type: WorkflowScheduleNodeType | str = WorkflowScheduleNodeType.SCHEDULED_WORKFLOW
    payload: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "trigger_kind", WorkflowScheduleTriggerKind(self.trigger_kind))
        object.__setattr__(self, "node_type", WorkflowScheduleNodeType(self.node_type))
        object.__setattr__(self, "payload", dict(self.payload))

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return (
            "WorkflowScheduleRecord("
            f"schedule_id={self.schedule_id!r}, project_id={self.project_id!r}, graph_id={self.graph_id!r})"
        )


@dataclass(frozen=True, slots=True)
class _CronCadence:
    trigger_kind: str
    interval_seconds: float
    next_run_at: float


def workflow_schedule_request_from_dict(raw: Mapping[str, object]) -> WorkflowScheduleRequest:
    """Build a schedule request from an API or service payload.

    Args:
        raw: Raw mapping containing ``project_id``, ``graph_id``, and either a
            ``cron_expression`` or ``interval_seconds`` trigger. Trigger fields
            may also be nested under a ``trigger`` mapping.

    Returns:
        A validated workflow schedule request.

    Raises:
        WorkflowBuilderError: If required fields are missing or numeric trigger
            fields cannot be parsed safely.
    """
    trigger = raw.get("trigger")
    trigger_mapping = trigger if isinstance(trigger, Mapping) else None
    return WorkflowScheduleRequest(
        project_id=_required_text(raw.get("project_id"), "project_id"),
        graph_id=_required_text(raw.get("graph_id"), "graph_id"),
        cron_expression=_optional_text(_lookup_trigger_value(raw, trigger_mapping, "cron_expression")),
        interval_seconds=_optional_float(
            _lookup_trigger_value(raw, trigger_mapping, "interval_seconds"), "interval_seconds"
        ),
        next_run_at=_optional_float(_lookup_trigger_value(raw, trigger_mapping, "next_run_at"), "next_run_at"),
        schedule_id=_optional_text(raw.get("schedule_id")),
        name=_optional_text(raw.get("name")),
    )


def normalize_workflow_schedule(
    request: WorkflowScheduleRequest, *, now: float | None = None
) -> NormalizedWorkflowSchedule:
    """Normalize a workflow schedule request into scheduler registry fields.

    Args:
        request: Validated workflow schedule request.
        now: Optional reference timestamp for deterministic tests. Defaults to
            the current POSIX timestamp.

    Returns:
        Scheduler-ready schedule cadence with an interval and first run time.

    Raises:
        WorkflowBuilderError: If a cron expression is malformed, unsupported,
            or an explicit next run timestamp is already in the past.
    """
    reference = _reference_timestamp(now)
    schedule_id = request.schedule_id or f"workflow-builder-{request.project_id}-{request.graph_id}"
    name = request.name or f"Workflow Builder: {request.project_id}/{request.graph_id}"
    if request.cron_expression is not None:
        cadence = _normalize_cron_expression(request.cron_expression, reference)
        return NormalizedWorkflowSchedule(
            schedule_id=schedule_id,
            name=name,
            trigger_kind=cadence.trigger_kind,
            interval_seconds=cadence.interval_seconds,
            next_run_at=cadence.next_run_at,
            cron_expression=request.cron_expression,
        )

    if request.interval_seconds is None:
        raise WorkflowBuilderError("schedule-trigger-missing")
    next_run_at = reference if request.next_run_at is None else float(request.next_run_at)
    if next_run_at < reference:
        raise WorkflowBuilderError("schedule-next-run-in-past")
    return NormalizedWorkflowSchedule(
        schedule_id=schedule_id,
        name=name,
        trigger_kind="interval",
        interval_seconds=float(request.interval_seconds),
        next_run_at=next_run_at,
    )


def _normalize_cron_expression(expression: str, reference: float) -> _CronCadence:
    fields = expression.split()
    if len(fields) != _CRON_FIELD_COUNT:
        raise WorkflowBuilderError("schedule-cron-invalid", "expected five fields")
    minute_field, hour_field, day_of_month_field, month_field, day_of_week_field = fields
    if day_of_month_field != "*" or month_field != "*":
        raise WorkflowBuilderError("schedule-cron-unsupported", "day-of-month and month fields must be wildcard")

    if minute_field == "*" and hour_field == "*" and day_of_week_field == "*":
        return _CronCadence(
            trigger_kind=WorkflowScheduleTriggerKind.CRON.value,
            interval_seconds=_SECONDS_PER_MINUTE,
            next_run_at=_next_minute_boundary(reference, 1),
        )
    if minute_field.startswith("*/") and hour_field == "*" and day_of_week_field == "*":
        step_minutes = _parse_minute_step(minute_field)
        return _CronCadence(
            trigger_kind=WorkflowScheduleTriggerKind.CRON.value,
            interval_seconds=step_minutes * _SECONDS_PER_MINUTE,
            next_run_at=_next_minute_boundary(reference, step_minutes),
        )

    minute = _parse_cron_int(minute_field, minimum=0, maximum=59, field_name="minute")
    if hour_field == "*" and day_of_week_field == "*":
        return _CronCadence(
            trigger_kind=WorkflowScheduleTriggerKind.CRON.value,
            interval_seconds=_SECONDS_PER_HOUR,
            next_run_at=_next_hourly_run(reference, minute),
        )

    hour = _parse_cron_int(hour_field, minimum=0, maximum=23, field_name="hour")
    if day_of_week_field == "*":
        return _CronCadence(
            trigger_kind=WorkflowScheduleTriggerKind.CRON.value,
            interval_seconds=_SECONDS_PER_DAY,
            next_run_at=_next_daily_run(reference, hour, minute),
        )

    day_of_week = _parse_day_of_week(day_of_week_field)
    return _CronCadence(
        trigger_kind=WorkflowScheduleTriggerKind.CRON.value,
        interval_seconds=_SECONDS_PER_WEEK,
        next_run_at=_next_weekly_run(reference, hour, minute, day_of_week),
    )


def _reference_timestamp(now: float | None) -> float:
    if now is None:
        return time.time()
    try:
        reference = float(now)
    except (TypeError, ValueError) as exc:
        raise WorkflowBuilderError("schedule-reference-time-invalid") from exc
    if reference < 0:
        raise WorkflowBuilderError("schedule-reference-time-invalid")
    return reference


def _next_minute_boundary(reference: float, step_minutes: int) -> float:
    candidate = _reference_datetime(reference).replace(second=0, microsecond=0)
    if candidate.timestamp() <= reference:
        candidate += timedelta(minutes=1)
    remainder = candidate.minute % step_minutes
    if remainder:
        candidate += timedelta(minutes=step_minutes - remainder)
    return candidate.timestamp()


def _next_hourly_run(reference: float, minute: int) -> float:
    candidate = _reference_datetime(reference).replace(minute=minute, second=0, microsecond=0)
    if candidate.timestamp() <= reference:
        candidate += timedelta(hours=1)
    return candidate.timestamp()


def _next_daily_run(reference: float, hour: int, minute: int) -> float:
    candidate = _reference_datetime(reference).replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate.timestamp() <= reference:
        candidate += timedelta(days=1)
    return candidate.timestamp()


def _next_weekly_run(reference: float, hour: int, minute: int, cron_day_of_week: int) -> float:
    base = _reference_datetime(reference)
    python_day_of_week = (cron_day_of_week - 1) % 7
    days_until_run = (python_day_of_week - base.weekday()) % 7
    candidate = base.replace(hour=hour, minute=minute, second=0, microsecond=0) + timedelta(days=days_until_run)
    if candidate.timestamp() <= reference:
        candidate += timedelta(days=7)
    return candidate.timestamp()


def _reference_datetime(reference: float) -> datetime:
    return datetime.fromtimestamp(reference, tz=timezone.utc)


def _parse_minute_step(value: str) -> int:
    step = _parse_cron_int(value[2:], minimum=1, maximum=59, field_name="minute-step")
    if 60 % step != 0:
        raise WorkflowBuilderError("schedule-cron-unsupported", "minute step must divide 60")
    return step


def _parse_day_of_week(value: str) -> int:
    day_of_week = _parse_cron_int(value, minimum=0, maximum=7, field_name="day-of-week")
    return 0 if day_of_week == 7 else day_of_week


def _parse_cron_int(value: str, *, minimum: int, maximum: int, field_name: str) -> int:
    if not value.isdecimal():
        raise WorkflowBuilderError("schedule-cron-unsupported", field_name)
    parsed = int(value)
    if parsed < minimum or parsed > maximum:
        raise WorkflowBuilderError("schedule-cron-field-out-of-range", field_name)
    return parsed


def _lookup_trigger_value(
    raw: Mapping[str, object], trigger: Mapping[object, object] | None, key: str
) -> object | None:
    if key in raw:
        return raw[key]
    if trigger is not None and key in trigger:
        return trigger[key]
    return None


def _required_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise WorkflowBuilderError("schedule-field-required", field_name)
    return value.strip()


def _optional_text(value: object | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise WorkflowBuilderError("schedule-field-invalid")
    return value.strip()


def _optional_float(value: object | None, field_name: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise WorkflowBuilderError("schedule-field-invalid", field_name)
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise WorkflowBuilderError("schedule-field-invalid", field_name) from exc


def _require_safe_id(value: str, field_name: str) -> None:
    if not value or "/" in value or "\\" in value or ".." in value or _SAFE_ID.fullmatch(value) is None:
        raise WorkflowBuilderError("id-invalid", field_name)


__all__ = [
    "SCHEDULED_WORKFLOW_CAPABILITY",
    "SCHEDULED_WORKFLOW_SOURCE",
    "NormalizedWorkflowSchedule",
    "WorkflowScheduleNodeType",
    "WorkflowScheduleRecord",
    "WorkflowScheduleRequest",
    "WorkflowScheduleTriggerKind",
    "normalize_workflow_schedule",
    "workflow_schedule_request_from_dict",
]
