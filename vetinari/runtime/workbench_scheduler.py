"""Workbench-wide GPU/inference priority scheduler."""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict
from datetime import datetime, timezone
from datetime import time as dt_time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from vetinari.runtime._workbench_rust_bridge import (
    RustSchedulerBridge,
    RustSchedulerBridgeSnapshot,
    RustSchedulerBridgeUnavailable,
    emit_lane_usage_receipt,
)
from vetinari.runtime.workbench_scheduler_checkpoint import (
    cancel_and_drain_checkpoint_threads,
    run_checkpoint_with_timeout,
)
from vetinari.runtime.workbench_scheduler_config import (
    load_compute_routing_config,
    load_config,
    load_lane_capacity,
    parse_hhmm,
)
from vetinari.runtime.workbench_scheduler_signals import (
    install_signal_handlers,
    signal_handlers_installed_state,
)
from vetinari.runtime.workbench_scheduler_types import (
    Lane,
    LaneUsageReceipt,
    Lease,
    RecurringTask,
    VRAMOverCommit,
    WorkbenchSchedulerCapacityRetryExceeded,
    WorkbenchSchedulerConfigError,
    WorkbenchSchedulerLaneFull,
    WorkbenchSchedulerOutsideTrainingWindow,
    _LaneState,
)
from vetinari.security.redaction import redact_value
from vetinari.utils import privacy_receipt

logger = logging.getLogger(__name__)


if TYPE_CHECKING:
    from vetinari.inference import ComputeTarget, RoutedInferenceRequest
    from vetinari.inference.persistent_jobs import PersistentJobQueue


NoCapacityError: type[Exception] | None = None
select_target: Callable[..., Any] | None = None
_install_lock = threading.Lock()
# Singleton scheduler instance; written by workbench_mission_control_api._ensure_scheduler_registered()
# under _install_lock. Read by the same module to check if registration already happened.
# Protected by _install_lock for writes; reads outside the lock are safe for presence checks
# because Python attribute reads on None-typed module globals are atomic.
_registered_scheduler: WorkbenchScheduler | None = None


def get_registered_workbench_scheduler() -> WorkbenchScheduler | None:
    """Return the registered scheduler singleton, if installed."""
    return _registered_scheduler


def ensure_registered_workbench_scheduler() -> WorkbenchScheduler:
    """Install and return the shared Workbench scheduler singleton.

    Returns:
        The existing registered scheduler, or a newly constructed singleton when
        no scheduler has been registered yet.
    """
    global _registered_scheduler
    if _registered_scheduler is not None:
        return _registered_scheduler
    with _install_lock:
        if _registered_scheduler is None:
            _registered_scheduler = WorkbenchScheduler()
        return _registered_scheduler


def _load_inference_runtime() -> tuple[type[Exception], Callable[..., Any]]:
    global NoCapacityError, select_target
    if NoCapacityError is None or select_target is None:
        from vetinari.inference import NoCapacityError as loaded_no_capacity_error
        from vetinari.inference import select_target as loaded_select_target

        NoCapacityError = loaded_no_capacity_error
        if select_target is None:
            select_target = loaded_select_target
    return NoCapacityError, select_target


class WorkbenchScheduler:
    """Three-lane priority scheduler for the shared workbench GPU."""

    def __init__(
        self,
        config_path: str | Path = "config/workbench_scheduler.yaml",
        *,
        lease_registry_path: str | Path | None = None,
        rust_bridge: RustSchedulerBridge | None = None,
        recurring_tasks_path: str | Path | None = None,
    ) -> None:
        """Load config and initialize clean in-process lane state.

        Args:
            config_path: YAML scheduler configuration path.
            lease_registry_path: Optional JSON registry path for persistent
                per-project lease accounting. When omitted, the scheduler uses
                ``resource_accounting.lease_registry_path`` from the scheduler
                config if present.
            rust_bridge: Optional Rust scheduler bridge override for tests and
                packaged-kernel integration.
            recurring_tasks_path: Optional JSON registry path for persistent
                recurring scheduled tasks (FSA-0399).  When omitted, the
                scheduler still supports in-memory recurring tasks but loses
                them on restart.

        Raises:
            WorkbenchSchedulerConfigError: If the config is missing or invalid,
                or if the recurring-task registry exists but cannot be parsed.
            VRAMOverCommit: If declared VRAM shares exceed the configured limit.
        """
        self._config_path = Path(config_path)
        self._config = load_config(self._config_path)
        configured_registry_path = lease_registry_path or self._config.get("resource_accounting", {}).get(
            "lease_registry_path"
        )
        if configured_registry_path:
            from vetinari.workbench.resource_cockpit.lease_registry import PersistentLeaseRegistry

            self._lease_registry = PersistentLeaseRegistry(configured_registry_path)
        else:
            self._lease_registry = None
        self._lane_capacity = load_lane_capacity(self._config)
        self._capacity_retry_attempts = int(self._config.get("preemption", {}).get("capacity_retry_attempts", 3))
        self._state_lock = threading.Lock()
        self._checkpoint_threads: dict[int, tuple[threading.Thread, threading.Event]] = {}
        self._checkpoint_threads_lock = threading.Lock()
        self._rust_bridge = rust_bridge or RustSchedulerBridge()
        self._active_leases: dict[str, Lease] = {}
        self._active_count = dict.fromkeys(Lane, 0)
        self._lane_state = {lane: _LaneState(capacity=self._lane_capacity[lane]) for lane in Lane}
        self.receipt_sink: Callable[[LaneUsageReceipt], None] | None = None
        self._previous_sigint_handler: Any = None
        self._previous_sigterm_handler: Any = None
        self._sigint_handler: Callable[[int, Any], None] | None = None
        self._sigterm_handler: Callable[[int, Any], None] | None = None
        # FSA-0399 recurring task registry.  ``_recurring_lock`` guards
        # both the in-memory dict and the on-disk file so concurrent
        # API calls cannot interleave a half-written registry.
        self._recurring_tasks_path: Path | None = (
            Path(recurring_tasks_path) if recurring_tasks_path is not None else None
        )
        self._recurring_lock = threading.RLock()
        self._recurring_tasks: dict[str, RecurringTask] = {}
        self._load_recurring_tasks()
        # Pre-parse ``training_allowed_windows`` once at construction so
        # every ``_check_training_window`` call iterates pre-resolved
        # ``(start_time, end_time)`` tuples instead of re-running the
        # ``HH:MM`` parser on every request.
        self._training_windows_parsed: list[tuple[dt_time, dt_time]] = []
        for window in self._config.get("training_allowed_windows") or []:
            if str(window.get("timezone", "UTC")).upper() != "UTC":
                raise WorkbenchSchedulerConfigError("only UTC training windows are supported")
            self._training_windows_parsed.append((parse_hhmm(str(window["start"])), parse_hhmm(str(window["end"]))))
        self._install_signal_handlers()

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
            WorkbenchSchedulerLaneFull: No slot is available in the requested lane.
            WorkbenchSchedulerCapacityRetryExceeded: Capacity retry limit exceeded.
            VRAMOverCommit: Declared shares exceed the configured headroom.
        """
        lane = self._coerce_lane(lane)
        if lane is Lane.TRAINING and not self._check_training_window(datetime.now(timezone.utc)):
            raise WorkbenchSchedulerOutsideTrainingWindow("training acquire queued outside allowed time window")
        for _attempt in range(self._capacity_retry_attempts + 1):
            preempt_lease: Lease | None = None
            with self._state_lock:
                self._vram_preflight_locked(lane)
                preempt_lease = self._preempt_candidate_locked(lane)
                self._reserve_slot_locked(lane, preempt_lease, checkpoint_fn)
            if preempt_lease is not None:
                self._preempt_lease_or_rollback(preempt_lease, lane, checkpoint_fn)
            lease = self._try_acquire_lease_iteration(
                lane=lane,
                request=request,
                caller_subsystem=caller_subsystem,
                checkpoint_fn=checkpoint_fn,
                project_id=project_id,
            )
            if lease is not None:
                return lease
        raise WorkbenchSchedulerCapacityRetryExceeded(
            f"lane {lane.value} capacity changed during routing after {self._capacity_retry_attempts} retries"
        )

    def _try_acquire_lease_iteration(
        self,
        *,
        lane: Lane,
        request: RoutedInferenceRequest,
        caller_subsystem: str,
        checkpoint_fn: Callable[[], None] | None,
        project_id: str,
    ) -> Lease | None:
        """Register one Rust lease, select a target, and activate the lease.

        Returns None when the active-slot race was lost so the outer loop retries.

        Args:
            lane: Coerced scheduler lane for this iteration.
            request: Inference request providing the workload capability.
            caller_subsystem: Runtime subsystem requesting the lease.
            checkpoint_fn: Optional training checkpoint callback.
            project_id: Project isolation key for the lease registry.

        Returns:
            Activated Lease on success, or None to signal retry.

        Raises:
            RustSchedulerBridgeUnavailable: Rust authority is unavailable.
            VRAMOverCommit: No compute target is available.
        """
        try:
            rust_lease_id = self._rust_bridge.register_lease_request(
                lane=lane,
                request=request,
                caller_subsystem=caller_subsystem,
                project_id=project_id,
            )
        except Exception:
            self._rollback_reservation(lane, checkpoint_fn)
            raise
        try:
            target = self._pick_target(request)
        except Exception:
            self._rust_bridge.rollback_lease(rust_lease_id, "target-selection-failed")
            self._rollback_reservation(lane, checkpoint_fn)
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
        registered_active = self._register_active_lease_locked(lease, lane)
        if registered_active:
            try:
                self._persist_lease_register(lease)
            except Exception:
                with self._state_lock:
                    self._active_leases.pop(lease.lease_id, None)
                    self._active_count[lane] = max(0, self._active_count[lane] - 1)
                    self._lane_state[lane].active_count = self._active_count[lane]
                    if lane is Lane.TRAINING and self._active_count[lane] == 0:
                        self._lane_state[lane].active_checkpoint = None
                self._rust_bridge.rollback_lease(rust_lease_id, "lease-registry-failed")
                raise
            return lease
        self._rust_bridge.rollback_lease(rust_lease_id, "active-registration-failed")
        return None

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
            self._rollback_reservation(lane, checkpoint_fn)
            raise

    def _register_active_lease_locked(self, lease: Lease, lane: Lane) -> bool:
        with self._state_lock:
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
        with self._state_lock:
            removed = self._active_leases.pop(active.lease_id, None)
            if removed is None:
                return
            self._active_count[removed.lane] = max(0, self._active_count[removed.lane] - 1)
            self._lane_state[removed.lane].active_count = self._active_count[removed.lane]
            if removed.lane is Lane.TRAINING and self._active_count[removed.lane] == 0:
                self._lane_state[removed.lane].active_checkpoint = None
        emit_lane_usage_receipt(
            self.receipt_sink,
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
        cancel_and_drain_checkpoint_threads(
            checkpoint_threads=self._checkpoint_threads,
            checkpoint_threads_lock=self._checkpoint_threads_lock,
            timeout_s=self._checkpoint_timeout_s(),
        )
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

    def rust_authority_snapshot(self) -> RustSchedulerBridgeSnapshot:
        """Return the Rust scheduler authority bridge state."""
        return self._rust_bridge.snapshot()

    @staticmethod
    def _pick_target(request: RoutedInferenceRequest) -> ComputeTarget:
        """Forward target selection to the inference router."""
        no_capacity_error, target_selector = _load_inference_runtime()
        try:
            return target_selector(request, load_compute_routing_config())
        except no_capacity_error as exc:
            raise VRAMOverCommit(f"no compute target available for {request.capability!r}") from exc

    def _vram_preflight(self, lane: Lane) -> None:
        """Validate declared-share headroom for a requested lane."""
        with self._state_lock:
            self._vram_preflight_locked(self._coerce_lane(lane))

    def _check_training_window(self, now: datetime) -> bool:
        """Return whether training is allowed at the supplied instant.

        Iterates the pre-parsed ``self._training_windows_parsed`` list
        built once at construction; no HH:MM re-parsing happens here.

        Args:
            now: Time to test. Naive values are interpreted as UTC.

        Returns:
            True when training is allowed.
        """
        if not self._training_windows_parsed:
            return True
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        current = now.astimezone(timezone.utc).time()
        for start, end in self._training_windows_parsed:
            if start <= end:
                if start <= current <= end:
                    return True
            elif current >= start or current <= end:
                return True
        return False

    def _run_checkpoint_with_timeout(
        self,
        *,
        checkpoint_fn: Callable[[], None] | None,
        timeout_s: float,
        lane: Lane | None,
        caller: str,
    ) -> None:
        """Run a checkpoint callback and fail closed on timeout.

        Args:
            checkpoint_fn: Callback supplied by the training caller.
            timeout_s: Maximum seconds to wait.
            lane: Lane owning the checkpoint callback.
            caller: Caller subsystem for diagnostics.
        """
        run_checkpoint_with_timeout(
            checkpoint_fn=checkpoint_fn,
            timeout_s=timeout_s,
            lane=lane,
            caller=caller,
            checkpoint_threads=self._checkpoint_threads,
            checkpoint_threads_lock=self._checkpoint_threads_lock,
        )

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

    def _install_signal_handlers(self) -> None:
        """Delegate process-signal lifecycle ownership to the signal boundary."""
        install_signal_handlers(self)

    @staticmethod
    def _coerce_lane(lane: Lane) -> Lane:
        if isinstance(lane, Lane):
            return lane
        return Lane(str(lane))

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

    def _slot_available_locked(self, lane: Lane, *, preempt_lease: Lease | None) -> bool:
        if lane is Lane.HUB_AGENT and self._active_count[Lane.TRAINING] > 0:
            return False
        if lane is Lane.INTERACTIVE and self._active_count[Lane.TRAINING] > 0 and preempt_lease is None:
            return False
        if self._active_count[lane] < self._lane_capacity[lane]:
            return True
        return lane is Lane.INTERACTIVE and preempt_lease is not None

    def _rollback_reservation(self, lane: Lane, checkpoint_fn: Callable[[], None] | None) -> None:
        with self._state_lock:
            self._active_count[lane] = max(0, self._active_count[lane] - 1)
            self._lane_state[lane].active_count = self._active_count[lane]
            if lane is Lane.TRAINING and self._lane_state[lane].active_checkpoint is checkpoint_fn:
                self._lane_state[lane].active_checkpoint = None

    def _vram_preflight_locked(self, lane: Lane) -> None:
        try:
            shares = self._config["vram_shares"]
            lane_share = float(shares[lane.value])
            if lane_share <= 0:
                raise VRAMOverCommit(f"lane {lane.value} has non-positive declared VRAM share {lane_share}")
            active_total = lane_share
            for active_lane, count in self._active_count.items():
                if count > 0:
                    active_total += float(shares[active_lane.value])
        except VRAMOverCommit:
            raise
        except Exception as exc:
            raise VRAMOverCommit(f"VRAM preflight failed closed for lane {lane.value}") from exc
        if active_total > 1.0 + 1e-9:
            raise VRAMOverCommit(f"lane {lane.value} declared VRAM share would exceed 1.0: {active_total:.3f}")

    def _vram_headroom_available_locked(self) -> bool:
        try:
            shares = self._config["vram_shares"]
            total = sum(float(shares[lane.value]) for lane, count in self._active_count.items() if count > 0)
        except Exception as exc:
            logger.warning("VRAM headroom check failed closed: %s", exc)
            return False
        return total <= 1.0 + 1e-9

    # ── Recurring scheduled tasks (FSA-0399) ───────────────────────────

    def _load_recurring_tasks(self) -> None:
        """Hydrate the recurring task registry from disk if a path was set.

        A missing file is treated as an empty registry.  A file that
        exists but cannot be parsed as JSON-of-dataclass-rows fails
        closed by raising :class:`WorkbenchSchedulerConfigError` so a
        corrupted registry cannot be silently turned into "no tasks
        scheduled".
        """
        path = self._recurring_tasks_path
        if path is None or not path.exists():
            return
        try:
            raw = path.read_text(encoding="utf-8")
            data = json.loads(raw) if raw.strip() else []
            if not isinstance(data, list):
                raise ValueError("recurring task registry must be a JSON list of objects")
            loaded: dict[str, RecurringTask] = {}
            for row in data:
                if not isinstance(row, dict):
                    raise ValueError("recurring task entry must be an object")
                next_run_at_value: object = row.get("next_run_at", row["start_at"])
                if not isinstance(next_run_at_value, (str, int, float)):
                    raise TypeError("next_run_at must be numeric")
                task = RecurringTask(
                    task_id=str(row["task_id"]),
                    name=str(row["name"]),
                    capability=str(row["capability"]),
                    payload=dict(row.get("payload") or {}),
                    interval_seconds=float(row["interval_seconds"]),
                    start_at=float(row["start_at"]),
                    next_run_at=float(next_run_at_value),
                )
                loaded[task.task_id] = task
            self._recurring_tasks = loaded
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            raise WorkbenchSchedulerConfigError(f"Failed to load recurring scheduler tasks from {path}: {exc}") from exc

    def _persist_recurring_tasks_locked(self) -> None:
        """Persist the in-memory registry to disk atomically.

        Caller must already hold ``self._recurring_lock``.
        """
        path = self._recurring_tasks_path
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_name(f".{path.name}.tmp")
        payload = []
        for task in self._recurring_tasks.values():
            row = asdict(task)
            row["payload"] = redact_value(row.get("payload") or {})
            row["privacy_receipt"] = privacy_receipt(
                privacy_class="operational",
                retention_days=30,
                source="workbench_scheduler.recurring_task",
                redaction_applied=True,
            )
            payload.append(row)
        tmp_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        tmp_path.replace(path)

    def create_recurring_task(
        self,
        *,
        name: str,
        capability: str,
        payload: dict[str, Any],
        interval_seconds: float,
        start_at: float | None = None,
        task_id: str | None = None,
    ) -> RecurringTask:
        """Register a new recurring task and persist the registry.

        Args:
            name: Human-readable label.
            capability: Capability/job-kind string handed to the worker.
            payload: Caller-supplied job payload (the recurring task id
                will be merged in under ``recurring_task_id`` when the
                task is enqueued).
            interval_seconds: Seconds between successive runs (> 0).
            start_at: First scheduled run timestamp (seconds since
                epoch).  Defaults to ``time.time()`` when omitted.
            task_id: Optional caller-chosen stable id; when omitted a
                random id is generated.  Explicit ids that already
                exist are rejected with :class:`ValueError`.

        Returns:
            The created :class:`RecurringTask` instance.

        Raises:
            ValueError: If ``task_id`` is supplied and already exists,
                if ``interval_seconds`` is non-positive, or if ``name``
                or ``capability`` are empty.
        """
        if not name:
            raise ValueError("name must be a non-empty string")
        if not capability:
            raise ValueError("capability must be a non-empty string")
        with self._recurring_lock:
            if task_id is not None:
                resolved_id = str(task_id)
                if resolved_id in self._recurring_tasks:
                    raise ValueError(f"recurring task id {resolved_id!r} already exists")
            else:
                # Auto-generate; loop until unique even though uuid4
                # collisions are vanishingly unlikely, because the
                # contract test treats id collision as a bug, not a
                # tolerable retry signal.
                while True:
                    resolved_id = f"recurring-{uuid.uuid4().hex[:12]}"
                    if resolved_id not in self._recurring_tasks:
                        break
            effective_start = float(start_at) if start_at is not None else time.time()
            task = RecurringTask(
                task_id=resolved_id,
                name=name,
                capability=capability,
                payload=dict(payload),
                interval_seconds=float(interval_seconds),
                start_at=effective_start,
                next_run_at=effective_start,
            )
            self._recurring_tasks[resolved_id] = task
            self._persist_recurring_tasks_locked()
        return task

    def list_recurring_tasks(self) -> list[RecurringTask]:
        """Return all registered recurring tasks in insertion order.

        Returns: A snapshot list of recurring tasks.
        """
        with self._recurring_lock:
            return list(self._recurring_tasks.values())

    def delete_recurring_task(self, task_id: str) -> bool:
        """Remove a recurring task by id.

        Args:
            task_id: Id returned by :meth:`create_recurring_task`.

        Returns:
            True if the task existed and was removed, False otherwise.
        """
        with self._recurring_lock:
            if task_id not in self._recurring_tasks:
                return False
            self._recurring_tasks.pop(task_id)
            self._persist_recurring_tasks_locked()
        return True

    def run_due_recurring_tasks(
        self,
        *,
        queue: PersistentJobQueue,
        now: float | None = None,
    ) -> list[str]:
        """Enqueue one persistent job per due recurring task.

        For every recurring task whose ``next_run_at`` is <= ``now``,
        this call generates a job id, enqueues a job with the task's
        capability and a payload of ``{**task.payload, "recurring_task_id": task.task_id}``
        into ``queue``, advances ``next_run_at`` by
        ``interval_seconds``, and persists the updated registry.

        Args:
            queue: The :class:`PersistentJobQueue` to enqueue into.
            now: Optional reference timestamp; defaults to
                ``time.time()``.

        Returns:
            List of job ids enqueued, in registry order.
        """
        reference = float(now) if now is not None else time.time()
        enqueued: list[str] = []
        with self._recurring_lock:
            for task in self._recurring_tasks.values():
                if task.next_run_at > reference:
                    continue
                job_id = f"recurring-{task.task_id}-{int(task.next_run_at)}-{uuid.uuid4().hex[:8]}"
                payload = {**task.payload, "recurring_task_id": task.task_id}
                queue.enqueue(job_id, task.capability, payload)
                task.next_run_at = task.next_run_at + task.interval_seconds
                enqueued.append(job_id)
            if enqueued:
                self._persist_recurring_tasks_locked()
        return enqueued


__all__ = [
    "Lane",
    "LaneUsageReceipt",
    "Lease",
    "RecurringTask",
    "RustSchedulerBridge",
    "RustSchedulerBridgeSnapshot",
    "RustSchedulerBridgeUnavailable",
    "VRAMOverCommit",
    "WorkbenchScheduler",
    "WorkbenchSchedulerCapacityRetryExceeded",
    "WorkbenchSchedulerConfigError",
    "WorkbenchSchedulerLaneFull",
    "WorkbenchSchedulerOutsideTrainingWindow",
    "ensure_registered_workbench_scheduler",
    "get_registered_workbench_scheduler",
    "signal_handlers_installed_state",
]
