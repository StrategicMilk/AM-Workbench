"""Extracted WorkbenchScheduler leasetracker responsibilities."""

from __future__ import annotations

import logging
import sys
import threading
import time
from collections.abc import Callable
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from vetinari.runtime.workbench_scheduler_types import (
    Lane,
    LaneUsageReceipt,
    Lease,
    WorkbenchSchedulerCapacityRetryExceeded,
    WorkbenchSchedulerLaneFull,
    WorkbenchSchedulerOutsideTrainingWindow,
)
from vetinari.workbench.resource_cockpit.cost_calculator import calculate_resource_cost

if TYPE_CHECKING:
    from vetinari.inference import ComputeTarget, RoutedInferenceRequest
    from vetinari.runtime.workbench_scheduler_bridge import RustSchedulerBridge
    from vetinari.runtime.workbench_scheduler_types import _LaneState
    from vetinari.workbench.resource_cockpit.lease_registry import PersistentLeaseRegistry

logger = logging.getLogger(__name__)


class LeaseTracker:
    """Named collaborator marker for WorkbenchScheduler leasetracker responsibilities."""


class WorkbenchSchedulerLeaseMixin:
    """Mixin containing WorkbenchScheduler leasetracker behavior."""

    if TYPE_CHECKING:
        _active_leases: dict[str, Lease]
        _active_count: dict[Lane, int]
        _capacity_retry_attempts: int
        _config: dict[str, Any]
        _lane_capacity: dict[Lane, int]
        _lane_state: dict[Lane, _LaneState]
        _lease_registry: PersistentLeaseRegistry | None
        _rust_bridge: RustSchedulerBridge
        _state_lock: threading.Lock
        receipt_sink: Callable[[LaneUsageReceipt], None] | None

        def _cancel_and_drain_checkpoint_threads(self, *, timeout_s: float) -> None: ...

        def _checkpoint_timeout_s(self) -> float: ...

        def _check_training_window(self, now: datetime) -> bool: ...

        def _coerce_lane(self, lane: Lane) -> Lane: ...

        def _pick_target(self, request: RoutedInferenceRequest) -> ComputeTarget: ...

        def _preempt_candidate_locked(self, lane: Lane) -> Lease | None: ...

        def _preempt_lease_or_rollback(
            self,
            preempt_lease: Lease,
            lane: Lane,
            checkpoint_fn: Callable[[], None] | None,
        ) -> None: ...

        def _slot_available_locked(self, lane: Lane, *, preempt_lease: Lease | None) -> bool: ...

        def _vram_headroom_available_locked(self) -> bool: ...

        def _vram_preflight_locked(self, lane: Lane) -> None: ...

    def acquire(
        self,
        *,
        lane: Lane,
        request: RoutedInferenceRequest,
        caller_subsystem: str,
        checkpoint_fn: Callable[[], None] | None = None,
        project_id: str = "default",
    ) -> Lease:
        """Acquire a lane lease before invoking an inference adapter.

        Args:
            lane: Requested scheduler lane.
            request: Inference request to route.
            caller_subsystem: Name used in receipts and warnings.
            checkpoint_fn: Optional training checkpoint callback.
            project_id: Project isolation key recorded in the lease registry.

        Returns:
            Lease stamped with the selected compute target.

        Raises:
            WorkbenchSchedulerLaneFull: If no slot is available.
            WorkbenchSchedulerCapacityRetryExceeded: If reconciliation fails.
            VRAMOverCommit: If declared shares exceed headroom.
        """
        lane = self._coerce_lane(lane)
        if lane is Lane.TRAINING and not self._check_training_window(datetime.now(timezone.utc)):
            with self._state_lock:
                self._lane_state[Lane.TRAINING].queued_count += 1
            raise WorkbenchSchedulerOutsideTrainingWindow("training acquire queued outside allowed time window")
        for _attempt in range(self._capacity_retry_attempts + 1):
            preempt_lease: Lease | None = None
            with self._state_lock:
                self._vram_preflight_locked(lane)
                preempt_lease = self._preempt_candidate_locked(lane)
            if preempt_lease is not None:
                self._preempt_lease_or_rollback(preempt_lease, lane, checkpoint_fn)
            with self._state_lock:
                try:
                    self._reserve_slot_locked(lane, None, checkpoint_fn)
                except WorkbenchSchedulerLaneFull:
                    if preempt_lease is None:
                        logger.warning("scheduler lane %s full with no preemptable lease", lane.value)
                        raise
                    continue
                rust_lease_id: str | None = None
                try:
                    rust_lease_id = self._rust_bridge.register_lease_request(
                        lane=lane,
                        request=request,
                        caller_subsystem=caller_subsystem,
                        project_id=project_id,
                    )
                    target = self._pick_target(request)
                except Exception:
                    self._rollback_reservation_locked(lane, checkpoint_fn)
                    if rust_lease_id is not None:
                        self._rust_bridge.rollback_lease(rust_lease_id, "target-selection-failed")
                    raise
                lease = Lease(
                    lane=lane,
                    target=target,
                    caller_subsystem=caller_subsystem,
                    checkpoint_fn=checkpoint_fn,
                    lease_id=rust_lease_id,
                    acquired_at=time.monotonic(),
                    project_id=project_id,
                    workload_id=request.capability,
                )
                if not self._register_active_lease_locked(lease, lane):
                    self._rust_bridge.rollback_lease(rust_lease_id, "capacity-changed")
                    continue
            try:
                self._persist_lease_register(lease)
            except Exception:
                with self._state_lock:
                    self._active_leases.pop(lease.lease_id, None)
                    self._rollback_reservation_locked(lane, checkpoint_fn)
                self._rust_bridge.rollback_lease(rust_lease_id, "lease-registry-failed")
                raise
            return lease
        raise WorkbenchSchedulerCapacityRetryExceeded(
            f"lane {lane.value} capacity changed during routing after {self._capacity_retry_attempts} retries"
        )

    def _reserve_slot_locked(
        self,
        lane: Lane,
        preempt_lease: Lease | None,
        checkpoint_fn: Callable[[], None] | None,
    ) -> None:
        if not self._slot_available_locked(lane, preempt_lease=preempt_lease):
            raise WorkbenchSchedulerLaneFull(f"lane {lane.value} has no available scheduler slot")
        self._active_count[lane] += 1
        self._lane_state[lane].active_count = self._active_count[lane]
        if lane is Lane.TRAINING:
            self._lane_state[lane].active_checkpoint = checkpoint_fn

    def _register_active_lease_locked(self, lease: Lease, lane: Lane) -> bool:
        if self._active_count[lane] <= self._lane_capacity[lane] and self._vram_headroom_available_locked():
            self._active_leases[lease.lease_id] = lease
            return True
        self._active_count[lane] = max(0, self._active_count[lane] - 1)
        self._lane_state[lane].active_count = self._active_count[lane]
        if lane is Lane.TRAINING and self._active_count[lane] == 0:
            self._lane_state[lane].active_checkpoint = None
        return False

    def release(
        self,
        lease: Lease,
        *,
        tokens_in: int = 0,
        tokens_out: int = 0,
        outcome: str = "ok",
    ) -> None:
        """Release a lease and emit a usage receipt outside the state lock.

        Args:
            lease: Lease returned from acquire.
            tokens_in: Input token count for telemetry.
            tokens_out: Output token count for telemetry.
            outcome: Release outcome: ok, error, or preempted.
        """
        with self._state_lock:
            active = self._active_leases.get(lease.lease_id)
            if active is None:
                return
            self._rust_bridge.record_receipt(
                lease_id=active.lease_id,
                outcome=outcome,
                rollback_performed=outcome in {"error", "preempted"},
            )
            removed = self._active_leases.pop(active.lease_id, None)
            if removed is None:
                return
            self._active_count[removed.lane] = max(0, self._active_count[removed.lane] - 1)
            self._lane_state[removed.lane].active_count = self._active_count[removed.lane]
            if removed.lane is Lane.TRAINING and self._active_count[removed.lane] == 0:
                self._lane_state[removed.lane].active_checkpoint = None
        self._emit_lane_usage_receipt(
            active,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            duration_s=max(0.0, time.monotonic() - active.acquired_at),
            outcome=outcome,
        )
        self._persist_lease_release(active)

    def release_all_leases(self, *, outcome: str = "preempted") -> None:
        """Release all active leases, used by process-exit cleanup hooks.

        Args:
            outcome: Receipt outcome to use for released leases.
        """
        self._cancel_and_drain_checkpoint_threads(timeout_s=self._checkpoint_timeout_s())
        with self._state_lock:
            leases = list(self._active_leases.values())
        for lease in leases:
            self.release(lease, outcome=outcome)

    def queue_depth_snapshot(self) -> dict[str, int | str]:
        """Return live active, queued, and capacity counters across scheduler lanes.

        Returns:
            Snapshot containing active, queued, depth, and capacity counts.
        """
        with self._state_lock:
            rust_snapshot = self._rust_bridge.snapshot()
            return {
                "active_count": sum(state.active_count for state in self._lane_state.values()),
                "queued_count": sum(state.queued_count for state in self._lane_state.values()),
                "queue_depth": sum(state.active_count + state.queued_count for state in self._lane_state.values()),
                "queue_capacity": sum(state.capacity for state in self._lane_state.values()),
                "rust_active_count": rust_snapshot.active_count,
                "rust_receipt_count": rust_snapshot.receipt_count,
                "rust_authority": rust_snapshot.authority,
            }

    def _emit_lane_usage_receipt(
        self,
        lease: Lease,
        *,
        tokens_in: int,
        tokens_out: int,
        duration_s: float,
        outcome: str,
    ) -> None:
        """Emit a lane usage receipt to the configured sink.

        Args:
            lease: Released lease.
            tokens_in: Input token count.
            tokens_out: Output token count.
            duration_s: Lease duration in seconds.
            outcome: Release outcome.
        """
        if self.receipt_sink is None:
            return

        scheduler_module = sys.modules.get("vetinari.runtime.workbench_scheduler")
        cost_calculator = getattr(scheduler_module, "calculate_resource_cost", calculate_resource_cost)
        cost = cost_calculator(
            model=lease.target.model,
            target_compute=lease.target.compute,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            duration_s=duration_s,
        )
        receipt = LaneUsageReceipt(
            lane=lease.lane,
            caller_subsystem=lease.caller_subsystem,
            target_compute=lease.target.compute,
            target_model=lease.target.model,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            duration_s=duration_s,
            outcome=outcome,
            lease_id=lease.lease_id,
            project_id=lease.project_id,
            gpu_hours=cost.gpu_hours,
            total_cost_usd=cost.total_cost_usd,
            cost_breakdown=cost.to_dict(),
        )
        self.receipt_sink(receipt)

    def _persist_lease_register(self, lease: Lease) -> None:
        if self._lease_registry is None:
            return
        self._lease_registry.register(
            project_id=lease.project_id,
            lease_id=lease.lease_id,
            lane=lease.lane.value,
            workload_id=lease.workload_id or lease.target.model,
            target_compute=lease.target.compute,
            target_model=lease.target.model,
        )

    def _persist_lease_release(self, lease: Lease) -> None:
        if self._lease_registry is None:
            return
        self._lease_registry.release(project_id=lease.project_id, lease_id=lease.lease_id)

    def _rollback_reservation(self, lane: Lane, checkpoint_fn: Callable[[], None] | None) -> None:
        with self._state_lock:
            self._rollback_reservation_locked(lane, checkpoint_fn)

    def _rollback_reservation_locked(self, lane: Lane, checkpoint_fn: Callable[[], None] | None) -> None:
        self._active_count[lane] = max(0, self._active_count[lane] - 1)
        self._lane_state[lane].active_count = self._active_count[lane]
        if lane is Lane.TRAINING and self._lane_state[lane].active_checkpoint is checkpoint_fn:
            self._lane_state[lane].active_checkpoint = None


__all__ = ["LeaseTracker", "WorkbenchSchedulerLeaseMixin"]
