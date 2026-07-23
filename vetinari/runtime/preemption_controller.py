"""Extracted WorkbenchScheduler preemptioncontroller responsibilities."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from vetinari.runtime.workbench_scheduler_checkpoint import (
    cancel_and_drain_checkpoint_threads,
    run_checkpoint_with_timeout,
)
from vetinari.runtime.workbench_scheduler_signals import install_signal_handlers
from vetinari.runtime.workbench_scheduler_types import Lane, Lease, WorkbenchSchedulerConfigError


class PreemptionController:
    """Named collaborator marker for WorkbenchScheduler preemptioncontroller responsibilities."""


class WorkbenchSchedulerPreemptionMixin:
    """Mixin containing WorkbenchScheduler preemptioncontroller behavior."""

    if TYPE_CHECKING:
        _active_leases: Any
        _checkpoint_threads: Any
        _checkpoint_threads_lock: Any
        _config: Any
        release: Any

    def _preempt_lease_or_rollback(
        self,
        preempt_lease: Lease,
        lane: Lane,
        checkpoint_fn: Callable[[], None] | None,
    ) -> None:
        try:
            self._run_checkpoint_with_timeout(
                checkpoint_fn=preempt_lease.checkpoint_fn,
                timeout_s=self._checkpoint_timeout_s(),
                lane=preempt_lease.lane,
                caller=preempt_lease.caller_subsystem,
            )
            self.release(preempt_lease, outcome="preempted")
        except Exception:
            raise

    def _run_checkpoint_with_timeout(
        self,
        *,
        checkpoint_fn: Callable[[], None] | None,
        timeout_s: float,
        lane: Lane | None,
        caller: str,
    ) -> None:
        """Run a checkpoint callback and fail closed on timeout."""
        run_checkpoint_with_timeout(
            checkpoint_fn=checkpoint_fn,
            timeout_s=timeout_s,
            lane=lane,
            caller=caller,
            checkpoint_threads=self._checkpoint_threads,
            checkpoint_threads_lock=self._checkpoint_threads_lock,
        )

    def _cancel_and_drain_checkpoint_threads(self, *, timeout_s: float) -> None:
        cancel_and_drain_checkpoint_threads(
            checkpoint_threads=self._checkpoint_threads,
            checkpoint_threads_lock=self._checkpoint_threads_lock,
            timeout_s=timeout_s,
        )

    def _install_signal_handlers(self) -> None:
        """Install SIGINT, SIGTERM, and atexit release hooks once per process."""
        install_signal_handlers(self)

    def _checkpoint_timeout_s(self) -> float:
        value = float(self._config.get("preemption", {}).get("checkpoint_timeout_s", 30.0))
        if value < 0:
            raise WorkbenchSchedulerConfigError("preemption.checkpoint_timeout_s must be non-negative")
        return value

    def _preempt_candidate_locked(self, lane: Lane) -> Lease | None:
        if lane is not Lane.INTERACTIVE:
            return None
        training = self._config.get("lanes", {}).get("training", {})
        if not bool(training.get("preempt_on_interactive", True)):
            return None
        for lease in self._active_leases.values():
            if lease.lane is Lane.TRAINING and lease.checkpoint_fn is not None:
                return lease
        return None


__all__ = ["PreemptionController", "WorkbenchSchedulerPreemptionMixin"]
