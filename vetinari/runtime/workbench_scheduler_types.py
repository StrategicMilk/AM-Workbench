"""Type and value objects for the workbench scheduler."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from vetinari.inference import ComputeTarget


class WorkbenchSchedulerConfigError(RuntimeError):
    """Raised when workbench scheduler YAML fails schema validation."""


class VRAMOverCommit(RuntimeError):
    """Raised when declared VRAM shares or active workload shares exceed 1.0."""


class WorkbenchSchedulerLaneFull(RuntimeError):
    """Raised when a lane has no capacity and cannot preempt lower-priority work."""


class WorkbenchSchedulerCapacityRetryExceeded(RuntimeError):
    """Raised when capacity reconciliation fails after the retry budget."""


class WorkbenchSchedulerOutsideTrainingWindow(RuntimeError):
    """Raised when callers choose to refuse training outside allowed windows."""


class Lane(Enum):
    """Priority lanes that compete for the shared workbench GPU."""

    INTERACTIVE = "interactive"
    HUB_AGENT = "hub_agent"
    TRAINING = "training"


@dataclass(frozen=True, slots=True)
class Lease:
    """A scheduler lease for one lane-bound inference workload.

    Attributes:
        lane: Priority lane that owns the lease.
        target: Compute target selected by the inference router.
        caller_subsystem: Subsystem that acquired the lease.
        checkpoint_fn: Optional callback used by training preemption.
        lease_id: Stable receipt and release identifier.
    """

    lane: Lane
    target: ComputeTarget
    caller_subsystem: str
    checkpoint_fn: Callable[[], None] | None = None
    lease_id: str = ""
    acquired_at: float = 0.0
    project_id: str = "default"
    workload_id: str = ""

    def __repr__(self) -> str:
        """Return a compact lease representation for diagnostics.

        Returns:
            String containing the lane, caller, and lease id.
        """
        return f"Lease(lane={self.lane.value!r}, caller={self.caller_subsystem!r}, lease_id={self.lease_id!r})"


@dataclass(frozen=True, slots=True)
class LaneUsageReceipt:
    """Telemetry emitted when a scheduler lease is released.

    Attributes:
        lane: Priority lane used by the workload.
        caller_subsystem: Subsystem that acquired the lease.
        target_compute: Compute backend selected by the router.
        target_model: Model selected by the router.
        tokens_in: Input token count reported by the caller.
        tokens_out: Output token count reported by the caller.
        duration_s: Lease duration in seconds.
        outcome: Release outcome: ok, error, or preempted.
        lease_id: Lease identifier that produced this receipt.
    """

    lane: Lane
    caller_subsystem: str
    target_compute: str
    target_model: str
    tokens_in: int
    tokens_out: int
    duration_s: float
    outcome: str
    lease_id: str
    project_id: str = "default"
    gpu_hours: float = 0.0
    total_cost_usd: float = 0.0
    cost_breakdown: dict[str, Any] | None = None

    def __repr__(self) -> str:
        """Return a compact receipt representation for diagnostics.

        Returns:
            String containing the lane, outcome, and lease id.
        """
        return f"LaneUsageReceipt(lane={self.lane.value!r}, outcome={self.outcome!r}, lease_id={self.lease_id!r})"


@dataclass(slots=True)
class RecurringTask:
    """One persistent recurring scheduled task (FSA-0399)."""

    task_id: str
    name: str
    capability: str
    payload: dict[str, Any]
    interval_seconds: float
    start_at: float
    next_run_at: float = 0.0

    def __post_init__(self) -> None:
        if not self.task_id:
            raise ValueError("task_id must be a non-empty string")
        if self.interval_seconds <= 0:
            raise ValueError("interval_seconds must be > 0")
        if self.next_run_at == 0.0:
            self.next_run_at = self.start_at

    def __repr__(self) -> str:
        return (
            f"RecurringTask(task_id={self.task_id!r}, capability={self.capability!r}, next_run_at={self.next_run_at!r})"
        )


@dataclass(slots=True)
class _LaneState:
    """Mutable in-memory state for one scheduler lane."""

    capacity: int
    active_count: int = 0
    queued_count: int = 0
    active_checkpoint: Callable[[], None] | None = None

    def __repr__(self) -> str:
        """Return compact lane-state diagnostics.

        Returns:
            String containing capacity, active count, and queued count.
        """
        return (
            f"_LaneState(capacity={self.capacity}, active_count={self.active_count}, queued_count={self.queued_count})"
        )


__all__ = [
    "Lane",
    "LaneUsageReceipt",
    "Lease",
    "VRAMOverCommit",
    "WorkbenchSchedulerCapacityRetryExceeded",
    "WorkbenchSchedulerConfigError",
    "WorkbenchSchedulerLaneFull",
    "WorkbenchSchedulerOutsideTrainingWindow",
]
