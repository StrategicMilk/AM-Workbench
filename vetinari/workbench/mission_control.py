"""Mission-control runtime snapshot support for the Workbench operator surface.

The Rust kernel owns the HTTP route surface. This module keeps the Python
domain projection that other Python runtime components and tests consume:
scheduler lanes, metadata-spine queue state, and recursive-Foreman snapshots.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any

from vetinari.agents.consolidated.foreman_decomposition import snapshot_plan_parent_map as foreman_plan_parent_snapshot
from vetinari.agents.contracts import AgentResult
from vetinari.api.responses import json_safe
from vetinari.runtime.workbench_scheduler import Lane, ensure_registered_workbench_scheduler
from vetinari.workbench import (
    LeaseStatus,
    RunStatus,
    WorkbenchLease,
    WorkbenchRun,
    WorkbenchSpine,
)
from vetinari.workbench.metadata_spine import get_workbench_spine
from vetinari.workbench.mission_control_types import (
    AgentTaskRow,
    EscalationRow,
    LaneName,
    LaneState,
    MissionControlSnapshot,
    Pressure,
    QueueEntry,
    QueueState,
    RecursiveChildLink,
)

logger = logging.getLogger(__name__)

_REGISTRATION_LOCK: threading.Lock = threading.Lock()
_SCHEDULER_REGISTERED: bool = False
_SNAPSHOT_TTL_SECONDS: float = 1.0
_SNAPSHOT_CACHE: dict[str, tuple[float, MissionControlSnapshot]] = {}
_SNAPSHOT_CACHE_LOCK: threading.Lock = threading.Lock()
_RECURSIVE_PLAN_DISPLAY_LIMIT: int = 200


def ensure_mission_control_scheduler_registered() -> None:
    """Install the shared Workbench scheduler used by mission-control views."""
    global _SCHEDULER_REGISTERED
    if _SCHEDULER_REGISTERED:
        return
    with _REGISTRATION_LOCK:
        if _SCHEDULER_REGISTERED:
            return
        ensure_registered_workbench_scheduler()
        _SCHEDULER_REGISTERED = True


def _snapshot_plan_parent_map(*, limit: int = _RECURSIVE_PLAN_DISPLAY_LIMIT) -> tuple[dict[str, str], int]:
    return foreman_plan_parent_snapshot(limit=limit)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    raw = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        logger.warning("Mission Control could not parse timestamp value=%r; age defaults to zero", value)
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _age_seconds(value: str | None) -> float:
    parsed = _parse_utc(value)
    if parsed is None:
        return 0.0
    return max(0.0, (datetime.now(timezone.utc) - parsed).total_seconds())


def _coerce_enum_member(enum_type: Any, value: Any) -> Any:
    raw_value = getattr(value, "value", value)
    try:
        return value if isinstance(value, enum_type) else enum_type(str(raw_value))
    except ValueError:
        raw_name = getattr(value, "name", None)
        if raw_name in enum_type.__members__:
            return enum_type[raw_name]
        raise


def _coerce_lane_name(value: Lane | str) -> LaneName:
    lane = _coerce_enum_member(Lane, value)
    if lane is Lane.INTERACTIVE:
        return "interactive"
    if lane is Lane.HUB_AGENT:
        return "hub_agent"
    return "training"


def _queue_state(lease: WorkbenchLease) -> QueueState:
    status = _coerce_enum_member(LeaseStatus, lease.status)
    if status is LeaseStatus.REQUESTED:
        return "pending"
    if status is LeaseStatus.GRANTED:
        return "active"
    if status is LeaseStatus.RELEASED or status is LeaseStatus.EXPIRED:
        return "released"
    return "rejected"


def _normalize_run_for_queue(
    run: WorkbenchRun, leases: list[WorkbenchLease] | tuple[WorkbenchLease, ...] = ()
) -> AgentTaskRow:
    lane_name: LaneName | None = None
    if run.lease_id:
        for lease in leases:
            if lease.lease_id == run.lease_id or lease.requested_for_run_id == run.run_id:
                lane_name = _coerce_lane_name(lease.lane)
                break
    status = _coerce_enum_member(RunStatus, run.status)
    blocker = "blocked" if status is RunStatus.BLOCKED else None
    paused = status.value == "paused"
    return AgentTaskRow(
        run_id=run.run_id,
        task_id=run.run_id,
        agent_type=run.actor_agent_type.value,
        status=status.value,
        lane=lane_name,
        escalated=False,
        escalation_reason=None,
        recursive_parent_run_id=None,
        blocker_summary=blocker,
        retries=0,
        paused=paused,
        evidence_links=(),
        started_at_utc=run.started_at_utc or None,
        finished_at_utc=run.finished_at_utc or None,
    )


def _queue_entries(leases: list[WorkbenchLease]) -> tuple[QueueEntry, ...]:
    entries: list[QueueEntry] = []
    for lease in leases:
        timestamp = lease.granted_at_utc or lease.released_at_utc
        entries.append(
            QueueEntry(
                lease_id=lease.lease_id,
                caller_subsystem=lease.lease_handle,
                target=_coerce_lane_name(lease.lane),
                state=_queue_state(lease),
                age_seconds=_age_seconds(timestamp),
                run_id=lease.requested_for_run_id or None,
                requested_at_utc=timestamp,
            )
        )
    return tuple(entries)


def _pressure_for_lane(active_count: int, queued_count: int, committed: float, observed: float) -> Pressure:
    if queued_count > 0 or observed >= 0.9 or active_count >= 3:
        return "red"
    if active_count > 0 or observed >= 0.6 or committed >= 0.6:
        return "amber"
    return "green"


def _lease_pressure_summary(leases: list[WorkbenchLease]) -> tuple[LaneState, ...]:
    states: list[LaneState] = []
    for lane in Lane:
        lane_name = _coerce_lane_name(lane)
        lane_leases = [lease for lease in leases if _coerce_lane_name(lease.lane) == lane_name]
        active = [
            lease for lease in lane_leases if _coerce_enum_member(LeaseStatus, lease.status) is LeaseStatus.GRANTED
        ]
        queued = [
            lease for lease in lane_leases if _coerce_enum_member(LeaseStatus, lease.status) is LeaseStatus.REQUESTED
        ]
        committed = sum(max(0.0, lease.vram_share) for lease in active)
        observed = min(1.0, committed)
        states.append(
            LaneState(
                lane=lane_name,
                active_count=len(active),
                queued_count=len(queued),
                vram_share_committed=round(committed, 3),
                vram_share_observed=round(observed, 3),
                pressure=_pressure_for_lane(len(active), len(queued), committed, observed),
            )
        )
    return tuple(states)


def _recursive_links(parent_map: dict[str, str]) -> tuple[RecursiveChildLink, ...]:
    return tuple(
        RecursiveChildLink(
            parent_plan_id=parent,
            child_plan_id=child,
            created_at_utc=None,
            decomposition_decision="FOREMAN",
        )
        for child, parent in parent_map.items()
    )


def _escalations_from_runs(runs: list[WorkbenchRun]) -> tuple[EscalationRow, ...]:
    rows: list[EscalationRow] = []
    for run in runs:
        outcome = getattr(run, "outcome", None)
        result = outcome if isinstance(outcome, AgentResult) else None
        if result is None or not result.escalated:
            continue
        rows.append(
            EscalationRow(
                run_id=run.run_id,
                task_id=result.task_id or run.run_id,
                agent_type=run.actor_agent_type.value,
                escalation_reason=result.escalation_reason or "escalated without reason",
                escalated_at_utc=run.finished_at_utc or run.started_at_utc,
                child_plan_id=None,
            )
        )
    return tuple(rows)


def _resolve_spine() -> WorkbenchSpine:
    return get_workbench_spine()


def _build_snapshot(project_id: str) -> MissionControlSnapshot:
    now = time.monotonic()
    with _SNAPSHOT_CACHE_LOCK:
        cached = _SNAPSHOT_CACHE.get(project_id)
        if cached is not None and now - cached[0] <= _SNAPSHOT_TTL_SECONDS:
            return cached[1]

    spine = _resolve_spine()
    runs = [run for run in spine.list_runs() if run.project_id == project_id]
    project_run_ids = {run.run_id for run in runs}
    leases = [lease for lease in spine.list_leases() if lease.requested_for_run_id in project_run_ids]
    parent_map, parent_total = _snapshot_plan_parent_map(limit=_RECURSIVE_PLAN_DISPLAY_LIMIT)
    lanes = _lease_pressure_summary(leases)
    queue = _queue_entries(leases)
    agent_tasks = tuple(_normalize_run_for_queue(run, leases) for run in runs)
    recursive_children = _recursive_links(parent_map)
    escalations = _escalations_from_runs(runs)
    truncated_at = parent_total if parent_total > len(parent_map) else None
    empty = not queue and not agent_tasks and not recursive_children and not escalations
    degraded = truncated_at is not None
    snapshot = MissionControlSnapshot(
        project_id=project_id,
        generated_at_utc=_utc_now_iso(),
        status="empty" if empty else ("degraded" if degraded else "ok"),
        degraded=degraded,
        degraded_reason=(
            f"Recursive plan snapshot truncated to {len(parent_map)} of {parent_total} entries"
            if truncated_at is not None
            else None
        ),
        lanes=lanes,
        queue=queue,
        agent_tasks=agent_tasks,
        recursive_children=recursive_children,
        escalations=escalations,
        recursive_children_truncated_at=truncated_at,
    )
    with _SNAPSHOT_CACHE_LOCK:
        _SNAPSHOT_CACHE[project_id] = (time.monotonic(), snapshot)
    return snapshot


def build_mission_control_snapshot(project_id: str) -> MissionControlSnapshot:
    """Return the current mission-control snapshot for a project."""
    return _build_snapshot(project_id)


def mission_control_snapshot_payload(project_id: str) -> dict[str, Any]:
    """Return a JSON-compatible mission-control snapshot payload."""
    return json_safe(asdict(build_mission_control_snapshot(project_id)))


def mission_control_queue_payload(project_id: str) -> list[dict[str, Any]]:
    """Return JSON-compatible queue rows for a project mission-control view.

    Returns:
        Queue rows ready for API serialization.
    """
    snapshot = build_mission_control_snapshot(project_id)
    return [asdict(entry) for entry in snapshot.queue]


def mission_control_agents_payload(project_id: str) -> dict[str, list[dict[str, Any]]]:
    """Return JSON-compatible agent, escalation, and recursive-child rows.

    Returns:
        Agent task, escalation, and recursive child rows grouped by key.
    """
    snapshot = build_mission_control_snapshot(project_id)
    return {
        "agent_tasks": [asdict(row) for row in snapshot.agent_tasks],
        "escalations": [asdict(row) for row in snapshot.escalations],
        "recursive_children": [asdict(row) for row in snapshot.recursive_children],
    }


__all__ = [
    "AgentTaskRow",
    "EscalationRow",
    "LaneState",
    "MissionControlSnapshot",
    "QueueEntry",
    "RecursiveChildLink",
    "build_mission_control_snapshot",
    "ensure_mission_control_scheduler_registered",
    "mission_control_agents_payload",
    "mission_control_queue_payload",
    "mission_control_snapshot_payload",
]
