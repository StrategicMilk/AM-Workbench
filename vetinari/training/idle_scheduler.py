"""Idle detection and training scheduler for Vetinari.

Responsible for detecting when the system has no active user requests and
orchestrating idle-time training activities such as model fine-tuning and
curriculum-driven self-improvement cycles.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

from vetinari.runtime.workbench_scheduler import Lane, RustSchedulerBridge
from vetinari.training.idle_scheduler_runtime import _TrainingSchedulerRuntimeMixin
from vetinari.training.idle_scheduler_types import (
    MIN_FREE_VRAM_GB as _MIN_FREE_VRAM_GB,
)
from vetinari.training.idle_scheduler_types import (
    MIN_TRAINING_RECORDS as _MIN_TRAINING_RECORDS,
)
from vetinari.training.idle_scheduler_types import (
    OUTPUTS_SCRATCH_TTL_DAYS as _OUTPUTS_SCRATCH_TTL_DAYS,
)
from vetinari.training.idle_scheduler_types import (
    POLL_INTERVAL_SECONDS as _POLL_INTERVAL_SECONDS,
)
from vetinari.training.idle_scheduler_types import (
    IdleTrainingJob,
)
from vetinari.types import StatusEnum

logger = logging.getLogger(__name__)

# Re-export constants from the legacy module path.
POLL_INTERVAL_SECONDS = _POLL_INTERVAL_SECONDS
MIN_FREE_VRAM_GB = _MIN_FREE_VRAM_GB
MIN_TRAINING_RECORDS = _MIN_TRAINING_RECORDS
OUTPUTS_SCRATCH_TTL_DAYS = _OUTPUTS_SCRATCH_TTL_DAYS

# Alias for backward compatibility with callers importing from this module.
TrainingJob = IdleTrainingJob


class IdleDetector:
    """Detects when Vetinari has no active user requests.

    Records activity timestamps and exposes an ``idle`` property that
    returns ``True`` once no activity has been observed for at least
    ``min_idle_minutes``.  All mutable state is protected by a
    :class:`threading.Lock` so the detector is safe to call from multiple
    threads simultaneously.
    """

    def __init__(self, min_idle_minutes: int = 5) -> None:
        """Initialise the detector.

        Args:
            min_idle_minutes: Number of minutes of inactivity before the
                system is considered idle.  Must be a positive integer.
        """
        self._min_idle_minutes: int = min_idle_minutes
        self._last_activity: datetime = datetime.now(timezone.utc)
        self._was_idle: bool = False
        self._lock: threading.Lock = threading.Lock()

    def record_activity(self) -> None:
        """Record that user or agent activity just occurred.

        Updates the internal timestamp.  If the system was previously idle,
        logs the transition back to active so operators can observe the
        lifecycle.
        """
        with self._lock:
            was_idle = self._was_idle
            self._last_activity = datetime.now(timezone.utc)
            self._was_idle = False

        if was_idle:
            logger.info("IdleDetector: system transitioned from idle to active")

    @property
    def idle(self) -> bool:
        """Whether the system is currently idle.

        Returns:
            ``True`` if no activity has been recorded for at least
            ``min_idle_minutes``, ``False`` otherwise.
        """
        with self._lock:
            elapsed = (datetime.now(timezone.utc) - self._last_activity).total_seconds()
            is_idle = elapsed >= self._min_idle_minutes * 60
            if is_idle and not self._was_idle:
                self._was_idle = True
                logger.info(
                    "IdleDetector: system became idle after %.1f minutes of inactivity",
                    elapsed / 60,
                )
            return is_idle

    @property
    def idle_duration_minutes(self) -> float:
        """How long the system has been idle, in minutes.

        Returns:
            Minutes since the last recorded activity.  Returns ``0.0`` if
            the system is not currently idle.
        """
        with self._lock:
            elapsed_seconds = (datetime.now(timezone.utc) - self._last_activity).total_seconds()
            is_idle = elapsed_seconds >= self._min_idle_minutes * 60
            if not is_idle:
                return 0.0
            idle_seconds = max(0.0, elapsed_seconds - self._min_idle_minutes * 60)
            return idle_seconds / 60


class TrainingScheduler(_TrainingSchedulerRuntimeMixin):
    """Orchestrates idle-time training activities.

    Polls the system every :data:`POLL_INTERVAL_SECONDS` seconds.  When
    the :class:`IdleDetector` reports the system is idle and all
    preconditions pass, a training cycle is started.  User requests
    pre-empt training: call :meth:`pause_for_user_request` on incoming
    requests and :meth:`resume_after_user_request` when they finish.

    Example::

        detector = IdleDetector(min_idle_minutes=5)
        scheduler = TrainingScheduler(idle_detector=detector)
        scheduler.start()
        # …later…
        scheduler.stop()
    """

    def __init__(
        self,
        idle_detector: IdleDetector,
        vram_manager: object | None = None,
        rust_bridge: RustSchedulerBridge | None = None,
    ) -> None:
        """Initialise the scheduler.

        Args:
            idle_detector: Shared :class:`IdleDetector` instance used to
                decide when training is permitted.
            vram_manager: Optional VRAM manager object.  When provided, its
                ``free_vram_gb`` attribute (or ``get_free_vram_gb()`` method)
                is queried before starting a cycle.  When ``None`` the VRAM
                check is skipped.
            rust_bridge: Optional Rust scheduler bridge override for tests and
                packaged-kernel integration.
        """
        self._idle_detector: IdleDetector = idle_detector
        self._vram_manager: object | None = vram_manager
        self._rust_bridge = rust_bridge or RustSchedulerBridge()

        self._shutdown_event: threading.Event = threading.Event()
        self._paused: bool = False
        self._lock: threading.Lock = threading.Lock()

        self._current_job: IdleTrainingJob | None = None
        self._thread: threading.Thread | None = None
        self._training_threads: set[threading.Thread] = set()

        # History of all manually triggered and idle-time training jobs.
        # Each entry is a dict with job_id, activity_description, started_at.
        self._history: list[dict] = []
        self._training_lease_by_job: dict[str, str] = {}

    # ── Public control API ──────────────────────────────────────────────────

    def start(self) -> None:
        """Start the background scheduler daemon thread.

        The thread polls every :data:`POLL_INTERVAL_SECONDS` seconds and
        triggers training cycles when conditions are met.  Safe to call
        once; subsequent calls are no-ops if already running.
        """
        if self._thread is not None and self._thread.is_alive():
            logger.warning("TrainingScheduler.start() called but thread is already running")
            return

        self._shutdown_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            name="training-scheduler",
            daemon=True,
        )
        self._thread.start()
        logger.info("TrainingScheduler started (poll interval=%ds)", POLL_INTERVAL_SECONDS)

    def stop(self) -> None:
        """Signal the scheduler to shut down and wait for it to finish.

        Blocks until the background thread exits.  Safe to call even if
        the scheduler was never started.
        """
        self._shutdown_event.set()
        if self._thread is not None:
            self._thread.join()
            logger.info("TrainingScheduler stopped")
        self._thread = None
        self.join_training_workers(timeout=5.0)

    def pause_for_user_request(self) -> None:
        """Gracefully pause any current training job for an incoming request.

        Sets the internal paused flag and transitions a running job to
        "paused" status so the run loop can skip new training cycles while
        the user request is active.
        """
        with self._lock:
            self._paused = True
            if self._current_job is not None and self._current_job.status == StatusEnum.RUNNING.value:
                self._current_job.status = "paused"
                logger.info(
                    "TrainingScheduler: paused job %s for user request",
                    self._current_job.job_id,
                )

    def resume_after_user_request(self) -> None:
        """Resume a paused training job if the system is still idle.

        Only resumes if the :class:`IdleDetector` still reports idle;
        otherwise the paused job remains paused until the next poll cycle.
        """
        with self._lock:
            self._paused = False

        if not self._idle_detector.idle:
            logger.debug(
                "TrainingScheduler.resume_after_user_request: system no longer idle, not resuming",
            )
            return

        with self._lock:
            if self._current_job is not None and self._current_job.status == "paused":
                self._current_job.status = "running"
                logger.info(
                    "TrainingScheduler: resumed job %s after user request",
                    self._current_job.job_id,
                )

    def start_manual_cycle(
        self,
        activity_description: str = "Manual training cycle",
        task_type: str | None = None,
    ) -> str:
        """Trigger a manual training cycle immediately, bypassing idle detection.

        Creates a :class:`TrainingJob` with a ``"manual-"`` prefix, records it
        in :attr:`_history`, and runs :meth:`_execute_training_cycle` in a
        daemon thread so the caller is not blocked.

        If a training job is already running, returns the sentinel string
        ``"already_running"`` and does NOT append to history.

        Args:
            activity_description: Human-readable description of the activity to
                run.  Defaults to ``"Manual training cycle"`` when omitted.
            task_type: Optional task or skill type to train. When provided,
                execution is routed through the training pipeline for that
                task type instead of the generic curriculum path.

        Returns:
            A ``"manual-"``-prefixed hex job ID on success, or
            ``"already_running"`` when a job is already in flight.

        Raises:
            RustSchedulerBridgeUnavailable: If the Rust scheduler authority
                rejects or cannot record the training lease request.
        """
        with self._lock:
            if self._current_job is not None and self._current_job.status == StatusEnum.RUNNING.value:
                logger.info(
                    "start_manual_cycle: job %s already running — ignoring request",
                    self._current_job.job_id,
                )
                return "already_running"

        job_id = "manual-" + uuid.uuid4().hex
        job = IdleTrainingJob(
            job_id=job_id,
            status="running",
            activity_description=activity_description,
            started_at=datetime.now(timezone.utc).isoformat(),
            task_type=task_type,
            progress=0.0,
        )

        with self._lock:
            if self._current_job is not None and self._current_job.status == StatusEnum.RUNNING.value:
                logger.info(
                    "start_manual_cycle: job %s already running - ignoring request",
                    self._current_job.job_id,
                )
                return "already_running"
            self._current_job = job
            self._history.append({
                "job_id": job_id,
                "activity_description": activity_description,
                "task_type": task_type,
                "started_at": job.started_at,
            })

        try:
            rust_lease_id = self._rust_bridge.register_lease_request(
                lane=Lane.TRAINING,
                request=SimpleNamespace(capability=task_type or "idle-training"),
                caller_subsystem="training",
                project_id="default",
            )
        except Exception:
            with self._lock:
                if self._current_job is not None and self._current_job.job_id == job_id:
                    self._current_job = None
                self._history = [item for item in self._history if item.get("job_id") != job_id]
            raise

        with self._lock:
            self._training_lease_by_job[job_id] = rust_lease_id

        logger.info(
            "start_manual_cycle: started job=%s activity=%r",
            job_id,
            activity_description,
        )

        thread = threading.Thread(
            target=self._run_tracked_training_cycle,
            args=(job,),
            name=f"manual-training-{job_id[:8]}",
            daemon=True,
        )
        with self._lock:
            self._training_threads.add(thread)
        thread.start()
        return job_id

    def join_training_workers(self, timeout: float | None = None) -> None:
        """Wait for manually-triggered training workers to finish."""
        deadline = time.monotonic() + timeout if timeout is not None else None
        current = threading.current_thread()
        while True:
            with self._lock:
                threads = [thread for thread in self._training_threads if thread is not current]
            if not threads:
                return
            for thread in threads:
                if deadline is None:
                    thread.join()
                else:
                    remaining = max(0.0, deadline - time.monotonic())
                    thread.join(remaining)
            if deadline is not None and time.monotonic() >= deadline:
                return

    def _run_tracked_training_cycle(self, job: IdleTrainingJob) -> None:
        try:
            self._execute_training_cycle_with_rust_receipt(job)
        finally:
            current = threading.current_thread()
            with self._lock:
                self._training_threads.discard(current)

    def _start_idle_cycle_with_rust_receipt(self) -> None:
        """Start one idle-detected training cycle behind the Rust lease boundary."""
        activity_description = self._get_next_curriculum_activity()
        if activity_description is None:
            self._handle_missing_activity()
            return

        job_id = "idle-" + uuid.uuid4().hex
        job = IdleTrainingJob(
            job_id=job_id,
            status="running",
            activity_description=activity_description,
            started_at=datetime.now(timezone.utc).isoformat(),
            task_type=None,
            progress=0.0,
        )
        with self._lock:
            if self._current_job is not None and self._current_job.status == StatusEnum.RUNNING.value:
                return
            self._current_job = job
            self._history.append({
                "job_id": job_id,
                "activity_description": activity_description,
                "task_type": None,
                "started_at": job.started_at,
            })

        try:
            rust_lease_id = self._rust_bridge.register_lease_request(
                lane=Lane.TRAINING,
                request=SimpleNamespace(capability="idle-training"),
                caller_subsystem="training",
                project_id="default",
            )
        except Exception:
            with self._lock:
                if self._current_job is not None and self._current_job.job_id == job_id:
                    self._current_job = None
                self._history = [item for item in self._history if item.get("job_id") != job_id]
            raise

        with self._lock:
            self._training_lease_by_job[job_id] = rust_lease_id
        self._execute_training_cycle_with_rust_receipt(job)

    def _execute_training_cycle_with_rust_receipt(self, job: IdleTrainingJob) -> None:
        """Run training and receipt the Rust scheduler lease."""
        try:
            self._execute_training_cycle(job)
        finally:
            with self._lock:
                lease_id = self._training_lease_by_job.pop(job.job_id, "")
                status = self._current_job.status if self._current_job is not None else "unknown"
            if lease_id:
                self._rust_bridge.record_receipt(
                    lease_id=lease_id,
                    outcome="ok" if status == "completed" else "error",
                    rollback_performed=status != "completed",
                )

    def rust_authority_snapshot(self) -> object:
        """Return the Rust scheduler bridge snapshot used by training callers."""
        return self._rust_bridge.snapshot()

    # ── Properties ─────────────────────────────────────────────────────────

    @property
    def current_job(self) -> IdleTrainingJob | None:
        """The currently active :class:`TrainingJob`, or ``None``.

        Returns:
            The active job dataclass or ``None`` if no job is in flight.
        """
        with self._lock:
            return self._current_job

    @property
    def is_training(self) -> bool:
        """Whether a training job is currently running.

        Returns:
            ``True`` if a job exists and its status is ``"running"``.
        """
        with self._lock:
            return self._current_job is not None and self._current_job.status == StatusEnum.RUNNING.value


# ---------------------------------------------------------------------------
# Module-level scheduler registry
# ---------------------------------------------------------------------------
# The web layer (training_api.py) registers the scheduler singleton here once
# it is created, keeping the dependency arrow pointing inward:
#   web -> training  (correct)
# rather than the circular:
#   training -> web  (forbidden)

_registered_scheduler: TrainingScheduler | None = None
_registry_lock: threading.Lock = threading.Lock()

# ---------------------------------------------------------------------------
# Module-level get_training_scheduler singleton
# ---------------------------------------------------------------------------
# Exposes the canonical TrainingScheduler instance for non-web callers that
# need to inspect or control the scheduler without going through the web layer.

_scheduler_instance: TrainingScheduler | None = None
_scheduler_instance_lock: threading.Lock = threading.Lock()


def get_training_scheduler() -> TrainingScheduler:
    """Return the canonical TrainingScheduler singleton.

    Creates an :class:`IdleDetector` and a :class:`TrainingScheduler` on the
    first call using double-checked locking.  Subsequent calls return the same
    instance so all callers share one scheduler.

    Returns:
        The shared :class:`TrainingScheduler` instance.
    """
    global _scheduler_instance
    if _scheduler_instance is not None:
        return _scheduler_instance
    with _scheduler_instance_lock:
        if _scheduler_instance is not None:
            return _scheduler_instance
        detector = IdleDetector()
        _scheduler_instance = TrainingScheduler(idle_detector=detector)
        register_scheduler(_scheduler_instance)
    logger.debug("get_training_scheduler: created new singleton")
    return _scheduler_instance


def register_scheduler(scheduler: TrainingScheduler) -> None:
    """Register the application's TrainingScheduler singleton.

    Called once by the web layer (vetinari.web.training_api) after it creates
    the shared scheduler.  Subsequent calls with the same instance are no-ops;
    calls with a different instance replace the registration and log a warning.

    Args:
        scheduler: The TrainingScheduler instance to register.
    """
    global _registered_scheduler
    with _registry_lock:
        if _registered_scheduler is scheduler:
            return
        if _registered_scheduler is not None:
            logger.warning("register_scheduler: replacing existing scheduler registration")
        _registered_scheduler = scheduler
    logger.debug("register_scheduler: scheduler registered")


def get_idle_detector() -> IdleDetector | None:
    """Return the IdleDetector from the registered TrainingScheduler.

    Returns ``None`` if no scheduler has been registered yet.  This is used
    by the Flask ``before_request`` hook to notify the detector of user
    activity so training does not fire while the user is actively working.

    Returns:
        The shared IdleDetector instance, or None.
    """
    with _registry_lock:
        scheduler = _registered_scheduler
    if scheduler is None:
        logger.debug("get_idle_detector: no scheduler registered yet")
        return None
    return scheduler._idle_detector
