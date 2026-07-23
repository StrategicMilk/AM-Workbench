"""Response value objects for Workbench mission-control support."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

LaneName = Literal["interactive", "hub_agent", "training"]
Pressure = Literal["green", "amber", "red"]
QueueState = Literal["pending", "active", "released", "rejected"]
SnapshotStatus = Literal["ok", "empty", "degraded"]


@dataclass(frozen=True, slots=True)
class LaneState:
    """Runtime contract for LaneState."""

    lane: LaneName
    active_count: int
    queued_count: int
    vram_share_committed: float
    vram_share_observed: float
    pressure: Pressure

    def __repr__(self) -> str:
        return (
            f"LaneState(lane={self.lane!r}, active={self.active_count}, "
            f"queued={self.queued_count}, pressure={self.pressure!r})"
        )


@dataclass(frozen=True, slots=True)
class QueueEntry:
    """Runtime contract for QueueEntry."""

    lease_id: str
    caller_subsystem: str
    target: LaneName
    state: QueueState
    age_seconds: float
    run_id: str | None
    requested_at_utc: str

    def __repr__(self) -> str:
        return (
            f"QueueEntry(lease_id={self.lease_id!r}, target={self.target!r}, "
            f"state={self.state!r}, run_id={self.run_id!r})"
        )


@dataclass(frozen=True, slots=True)
class AgentTaskRow:
    """Runtime contract for AgentTaskRow."""

    run_id: str
    task_id: str
    agent_type: str
    status: str
    lane: LaneName | None
    escalated: bool
    escalation_reason: str | None
    recursive_parent_run_id: str | None
    blocker_summary: str | None
    retries: int
    paused: bool
    evidence_links: tuple[str, ...]
    started_at_utc: str | None
    finished_at_utc: str | None

    def __repr__(self) -> str:
        return (
            f"AgentTaskRow(run_id={self.run_id!r}, task_id={self.task_id!r}, "
            f"status={self.status!r}, escalated={self.escalated!r})"
        )


@dataclass(frozen=True, slots=True)
class RecursiveChildLink:
    """Runtime contract for RecursiveChildLink."""

    parent_plan_id: str
    child_plan_id: str
    created_at_utc: str | None
    decomposition_decision: Literal["INSPECTOR", "WORKER", "FOREMAN"]

    def __repr__(self) -> str:
        return f"RecursiveChildLink(parent={self.parent_plan_id!r}, child={self.child_plan_id!r})"


@dataclass(frozen=True, slots=True)
class EscalationRow:
    """Runtime contract for EscalationRow."""

    run_id: str
    task_id: str
    agent_type: str
    escalation_reason: str
    escalated_at_utc: str | None
    child_plan_id: str | None

    def __repr__(self) -> str:
        return f"EscalationRow(run_id={self.run_id!r}, task_id={self.task_id!r}, child_plan_id={self.child_plan_id!r})"


@dataclass(frozen=True, slots=True)
class MissionControlSnapshot:
    """Runtime contract for MissionControlSnapshot."""

    project_id: str
    generated_at_utc: str
    status: SnapshotStatus
    degraded: bool
    degraded_reason: str | None
    lanes: tuple[LaneState, ...]
    queue: tuple[QueueEntry, ...]
    agent_tasks: tuple[AgentTaskRow, ...]
    recursive_children: tuple[RecursiveChildLink, ...]
    escalations: tuple[EscalationRow, ...]
    recursive_children_truncated_at: int | None

    def __repr__(self) -> str:
        return (
            f"MissionControlSnapshot(project_id={self.project_id!r}, status={self.status!r}, "
            f"lanes={len(self.lanes)}, queue={len(self.queue)}, tasks={len(self.agent_tasks)})"
        )
