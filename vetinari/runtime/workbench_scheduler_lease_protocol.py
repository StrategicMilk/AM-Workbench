"""Protocol contract for hosts composing the workbench scheduler lease mixin."""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from vetinari.typing_support import MixinProtocol

if TYPE_CHECKING:
    from vetinari.inference import ComputeTarget, RoutedInferenceRequest
    from vetinari.runtime.workbench_scheduler_bridge import RustSchedulerBridge
    from vetinari.runtime.workbench_scheduler_types import Lane, LaneUsageReceipt, Lease, _LaneState
    from vetinari.workbench.resource_cockpit.lease_registry import PersistentLeaseRegistry


@runtime_checkable
class WorkbenchSchedulerLeaseHost(MixinProtocol, Protocol):
    """Host attributes required by ``WorkbenchSchedulerLeaseMixin``."""

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

    def _checkpoint_timeout_s(self) -> float: ...

    def _check_training_window(self, now: Any) -> bool: ...

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


__all__ = ["WorkbenchSchedulerLeaseHost"]
