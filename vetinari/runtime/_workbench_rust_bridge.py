"""Rust scheduler authority bridge types and receipt emission for the Workbench Scheduler."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from vetinari.runtime.workbench_scheduler_types import (
    LaneUsageReceipt,
    Lease,
    WorkbenchSchedulerCapacityRetryExceeded,
)

if TYPE_CHECKING:
    from vetinari.inference import RoutedInferenceRequest
    from vetinari.runtime.workbench_scheduler_types import Lane

logger = logging.getLogger(__name__)


class RustSchedulerBridgeUnavailable(WorkbenchSchedulerCapacityRetryExceeded):
    """Raised when the Rust scheduler authority cannot admit or receipt work."""


@dataclass(frozen=True, slots=True)
class RustSchedulerBridgeSnapshot:
    """Operator-readable bridge state exported for tests and resource cockpit wiring."""

    authority: str
    active_count: int
    receipt_count: int
    restarted: bool

    def __repr__(self) -> str:
        return (
            "RustSchedulerBridgeSnapshot("
            f"authority={self.authority!r}, active_count={self.active_count}, "
            f"receipt_count={self.receipt_count}, restarted={self.restarted})"
        )


class RustSchedulerBridge:
    """Small Python boundary object for the Rust scheduler authority contract.

    The current pack keeps Python callers compatible while making the authority
    boundary explicit and fail-closed. Native bindings can replace this object
    without changing WorkbenchScheduler callers.
    """

    authority = "amw-kernel::scheduler"

    def __init__(self, *, available: bool = True) -> None:
        self._available = available
        self._lock = threading.Lock()
        self._active: dict[str, dict[str, str]] = {}
        self._receipts: list[dict[str, str]] = []
        self._sequence = 0
        self._restarted = False

    def register_lease_request(
        self,
        *,
        lane: Lane,
        request: RoutedInferenceRequest,
        caller_subsystem: str,
        project_id: str,
    ) -> str:
        """Return a Rust-authoritative lease id or fail closed.

        Args:
            lane: Scheduler lane being admitted.
            request: Routed inference request carrying the workload capability.
            caller_subsystem: Runtime subsystem requesting the lease.
            project_id: Project identifier associated with the workload.

        Returns:
            Stable Rust-authority lease id for later receipt or rollback.

        Raises:
            RustSchedulerBridgeUnavailable: If authority state is unavailable or the lease request is invalid.
        """
        self._require_available()
        workload_id = str(getattr(request, "capability", "") or "").strip()
        if not workload_id:
            raise RustSchedulerBridgeUnavailable("rust scheduler rejected workload without capability")
        if not caller_subsystem.strip():
            raise RustSchedulerBridgeUnavailable("rust scheduler rejected workload without caller")
        with self._lock:
            self._sequence += 1
            lease_id = f"rust-lease-{self._sequence}"
            self._active[lease_id] = {
                "lane": lane.value,
                "workload_id": workload_id,
                "caller_subsystem": caller_subsystem,
                "project_id": project_id,
            }
            return lease_id

    def record_receipt(self, *, lease_id: str, outcome: str, rollback_performed: bool = False) -> None:
        """Record the release receipt through the Rust authority boundary.

        Args:
            lease_id: Active lease id being released.
            outcome: Operator-readable completion outcome.
            rollback_performed: Whether release required rollback or cleanup.

        Raises:
            RustSchedulerBridgeUnavailable: If authority state is unavailable or the lease id is not active.
        """
        self._require_available()
        with self._lock:
            if lease_id not in self._active:
                raise RustSchedulerBridgeUnavailable(f"rust scheduler missing active lease: {lease_id}")
            self._active.pop(lease_id)
            self._receipts.append({
                "lease_id": lease_id,
                "outcome": outcome,
                "rollback_performed": str(bool(rollback_performed)).lower(),
            })

    def rollback_lease(self, lease_id: str, reason: str) -> None:
        """Rollback an admitted lease when Python-side target selection fails.

        Args:
            lease_id: Lease id to clear from active state.
            reason: Short rollback reason recorded in the receipt.
        """
        self._require_available()
        with self._lock:
            self._active.pop(lease_id, None)
            self._receipts.append({
                "lease_id": lease_id,
                "outcome": f"rollback:{reason}",
                "rollback_performed": "true",
            })

    def restart_from_receipts(self) -> RustSchedulerBridgeSnapshot:
        """Simulate damaged-state restart recovery from durable receipts.

        Returns:
            Snapshot after active leases are cleared and restart state is recorded.
        """
        self._require_available()
        with self._lock:
            self._active.clear()
            self._restarted = True
            return RustSchedulerBridgeSnapshot(
                authority=self.authority,
                active_count=0,
                receipt_count=len(self._receipts),
                restarted=True,
            )

    def snapshot(self) -> RustSchedulerBridgeSnapshot:
        """Return bridge state for wiring proof and Resource Cockpit overlay.

        Returns:
            Current Rust scheduler bridge counters and authority label.
        """
        with self._lock:
            return RustSchedulerBridgeSnapshot(
                authority=self.authority,
                active_count=len(self._active),
                receipt_count=len(self._receipts),
                restarted=self._restarted,
            )

    def _require_available(self) -> None:
        if not self._available:
            raise RustSchedulerBridgeUnavailable("rust scheduler authority unavailable")


def emit_lane_usage_receipt(
    receipt_sink: Callable[..., Any] | None,
    lease: Lease,
    *,
    tokens_in: int,
    tokens_out: int,
    duration_s: float,
    outcome: str,
) -> None:
    """Emit a lane usage receipt to the provided sink, computing resource cost.

    Args:
        receipt_sink: Callable that receives a LaneUsageReceipt, or None to skip.
        lease: Released lease carrying lane, target, and project context.
        tokens_in: Input token count for the lease duration.
        tokens_out: Output token count for the lease duration.
        duration_s: Lease duration in seconds.
        outcome: Operator-readable release outcome.
    """
    if receipt_sink is None:
        return
    from vetinari.workbench.resource_cockpit.cost_calculator import calculate_resource_cost

    cost = calculate_resource_cost(
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
    receipt_sink(receipt)


__all__ = [
    "RustSchedulerBridge",
    "RustSchedulerBridgeSnapshot",
    "RustSchedulerBridgeUnavailable",
    "emit_lane_usage_receipt",
]
