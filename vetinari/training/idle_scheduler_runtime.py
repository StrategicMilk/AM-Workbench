"""Runtime mixins for :mod:`vetinari.training.idle_scheduler`."""

from __future__ import annotations

import logging
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from vetinari.guards import require_subsystem
from vetinari.training.idle_maintenance import (
    consolidate_memory as _consolidate_memory_helper,
)
from vetinari.training.idle_maintenance import (
    sweep_outputs_scratch as _sweep_outputs_scratch_helper,
)
from vetinari.training.idle_scheduler_types import (
    MIN_FREE_VRAM_GB,
    MIN_TRAINING_RECORDS,
    POLL_INTERVAL_SECONDS,
    IdleTrainingJob,
    _require_module,
    get_outputs_scratch_ttl_days,
)
from vetinari.types import StatusEnum

logger = logging.getLogger("vetinari.training.idle_scheduler")
_IDLE_TRAINING_ARCHIVE_SCORE = 0.7

if TYPE_CHECKING:
    from vetinari.training.idle_scheduler import IdleDetector


class _TrainingSchedulerRuntimeMixin:
    """Internal execution behavior for ``TrainingScheduler``.

    The public scheduler class remains in ``idle_scheduler.py`` for import
    compatibility while the longer private runtime method groups live here.
    """

    if TYPE_CHECKING:
        _current_job: IdleTrainingJob | None
        _idle_detector: IdleDetector
        _lock: threading.Lock
        _paused: bool
        _shutdown_event: threading.Event
        _vram_manager: object | None

        def _start_idle_cycle_with_rust_receipt(self) -> None: ...

    def _run_loop(self) -> None:
        """Main background loop that drives training cycles.

        Polls every :data:`POLL_INTERVAL_SECONDS` seconds.  On each tick:

        1. Check whether the system is idle.
        2. Check whether the scheduler is paused.
        3. Check whether a job is already running.
        4. Verify all preconditions via :meth:`_can_train`.
        5. Execute a training cycle via :meth:`_execute_training_cycle`.
        """
        while not self._shutdown_event.is_set():
            try:
                self._shutdown_event.wait(timeout=POLL_INTERVAL_SECONDS)
                if self._shutdown_event.is_set():
                    break

                with self._lock:
                    paused = self._paused
                    already_running = (
                        self._current_job is not None and self._current_job.status == StatusEnum.RUNNING.value
                    )

                if not self._idle_detector.idle:
                    logger.debug("TrainingScheduler: system is not idle, skipping cycle")
                    continue

                if paused:
                    logger.debug("TrainingScheduler: scheduler is paused, skipping cycle")
                    continue

                if already_running:
                    logger.debug("TrainingScheduler: job already running, skipping cycle")
                    continue

                if not self._can_train():
                    logger.info("TrainingScheduler: preconditions not met, skipping cycle")
                    continue

                self._start_idle_cycle_with_rust_receipt()

            except Exception:
                logger.exception("TrainingScheduler._run_loop: unexpected error during cycle")

    def _can_train(self) -> bool:
        """Check whether all preconditions for a training cycle are satisfied.

        Evaluates:
        - Sufficient free VRAM if a VRAMManager is present.
        - No training job already running.
        - Sufficient training records.

        Returns:
            ``True`` if all checks pass, ``False`` otherwise.
        """
        with require_subsystem("idle_scheduler", "training_preconditions"):
            result = self._can_train_checked()
        return result

    def _can_train_checked(self) -> bool:
        can = True

        if self._vram_manager is not None:
            try:
                free_gb: float
                get_free_vram_gb: Any = getattr(self._vram_manager, "get_free_vram_gb", None)
                free_vram_gb: Any = getattr(self._vram_manager, "free_vram_gb", None)
                if callable(get_free_vram_gb):
                    free_gb = float(str(get_free_vram_gb()))
                elif free_vram_gb is not None:
                    free_gb = float(str(free_vram_gb))
                else:
                    logger.warning(
                        "_can_train: VRAMManager has no known VRAM attribute, skipping VRAM check",
                    )
                    free_gb = MIN_FREE_VRAM_GB

                if free_gb < MIN_FREE_VRAM_GB:
                    logger.info(
                        "_can_train: insufficient free VRAM (%.1f GB available, need %.1f GB)",
                        free_gb,
                        MIN_FREE_VRAM_GB,
                    )
                    can = False
            except Exception:
                logger.exception("_can_train: error querying VRAMManager, skipping VRAM check")
                raise

        with self._lock:
            if self._current_job is not None and self._current_job.status == StatusEnum.RUNNING.value:
                logger.info("_can_train: a training job is already running (%s)", self._current_job.job_id)
                can = False

        record_count = self._count_training_records()
        if record_count < MIN_TRAINING_RECORDS:
            logger.info(
                "_can_train: insufficient training records (%d available, need %d)",
                record_count,
                MIN_TRAINING_RECORDS,
            )
            can = False

        try:
            from vetinari.training.pipeline import TrainingPipeline

            pipeline = TrainingPipeline()
            resolved = pipeline._resolve_base_model("auto")
            if not resolved or resolved == "auto":
                logger.info("_can_train: no model available for training")
                can = False
        except Exception:
            logger.warning("_can_train: model availability check skipped")
            raise

        return can

    @staticmethod
    def _count_training_records() -> int:
        """Return the number of available training records.

        Returns:
            Count of available training records, or 0 on failure.
        """
        try:
            _require_module("vetinari.learning.training_data")
            from vetinari.learning.training_data import get_training_collector

            collector = get_training_collector()
            stats = collector.get_stats()
            return stats.get("total", 0)
        except ModuleNotFoundError:
            logger.debug("_count_training_records: training_data module not available")
            return 0
        except Exception:
            logger.warning("_count_training_records: could not count records", exc_info=True)
            return 0

    def _activity_for_cycle(self, job: IdleTrainingJob | None) -> str | None:
        return self._get_next_curriculum_activity() if job is None else job.activity_description

    def _handle_missing_activity(self) -> bool:
        try:
            from vetinari.learning.meta_optimizer import LearningPhase, MetaOptimizer

            if MetaOptimizer().detect_phase() == LearningPhase.COLLAPSE_RISK:
                logger.warning(
                    "_execute_training_cycle: MetaOptimizer detected COLLAPSE_RISK"
                    " - halting scheduler to prevent further quality degradation"
                )
                self._shutdown_event.set()
                return True
        except Exception:
            logger.warning(
                "_execute_training_cycle: could not check MetaOptimizer phase"
                " - proceeding with normal skip (scheduler continues)"
            )
        logger.info("_execute_training_cycle: no curriculum activity available, skipping")
        return True

    def _ensure_training_job(self, job: IdleTrainingJob | None, activity_description: str) -> IdleTrainingJob:
        if job is not None:
            return job
        job = IdleTrainingJob(
            job_id=uuid.uuid4().hex,
            status="running",
            activity_description=activity_description,
            started_at=datetime.now(timezone.utc).isoformat(),
            progress=0.0,
        )
        with self._lock:
            self._current_job = job
        return job

    def _mark_training_job_failed(self, job: IdleTrainingJob) -> None:
        with self._lock:
            if self._current_job is not None and self._current_job.job_id == job.job_id:
                self._current_job.status = "failed"

    def _mark_training_job_completed(self, job: IdleTrainingJob) -> None:
        with self._lock:
            if (
                self._current_job is not None
                and self._current_job.job_id == job.job_id
                and self._current_job.status == StatusEnum.RUNNING.value
            ):
                self._current_job.status = "completed"
                self._current_job.progress = 1.0
        logger.info("TrainingScheduler: completed training cycle job=%s", job.job_id)

    @staticmethod
    def _record_training_cycle_roi() -> None:
        try:
            _require_module("vetinari.learning.meta_optimizer")
            from vetinari.learning.meta_optimizer import MetaOptimizer

            MetaOptimizer().record_cycle(strategy_name="training", quality_gain=0.0, success=True)
        except ModuleNotFoundError:
            logger.debug("MetaOptimizer not available - skipping cycle record")
        except Exception:
            logger.warning(
                "Could not record training cycle in MetaOptimizer - ROI tracking will be incomplete",
                exc_info=True,
            )

    @staticmethod
    def _update_archive_scores() -> None:
        try:
            _require_module("vetinari.learning.improvement_archive")
            from vetinari.learning.improvement_archive import get_improvement_archive
            from vetinari.types import AgentType

            archive = get_improvement_archive()
            for agent_type in AgentType:
                top = archive.get_best_configs(agent_type.value, limit=1)
                if top:
                    archive.update_score(top[0].config_id, _IDLE_TRAINING_ARCHIVE_SCORE)
        except ModuleNotFoundError:
            logger.debug("ImprovementArchive not available - config score update skipped")
        except Exception:
            logger.warning(
                "Could not update config scores in ImprovementArchive - archive quality signals will be stale",
                exc_info=True,
            )

    def _execute_training_cycle(self, job: IdleTrainingJob | None = None) -> None:
        """Execute one idle-time training cycle.

        Retrieves the next activity from the curriculum for idle cycles, or
        runs a caller-provided manual job.
        """
        activity_description = self._activity_for_cycle(job)
        if activity_description is None:
            self._handle_missing_activity()
            return

        job = self._ensure_training_job(job, activity_description)

        logger.info(
            "TrainingScheduler: starting training cycle job=%s activity=%r",
            job.job_id,
            job.activity_description,
        )

        try:
            if job.task_type:
                self._run_targeted_training(job)
            else:
                self._run_activity(job)
        except Exception:
            logger.exception(
                "TrainingScheduler: training cycle failed for job=%s",
                job.job_id,
            )
            self._mark_training_job_failed(job)
        else:
            self._mark_training_job_completed(job)
            self._record_training_cycle_roi()
            self._update_archive_scores()

        self._consolidate_memory()

    def _consolidate_memory(self) -> None:
        """Run memory consolidation during idle time."""
        _consolidate_memory_helper(logger)

        deleted_outputs = self._sweep_outputs_scratch()
        if deleted_outputs > 0:
            logger.info("TrainingScheduler: swept %d stale outputs scratch artifact(s)", deleted_outputs)

    @staticmethod
    def _sweep_outputs_scratch(
        outputs_root: Path | None = None,
        ttl_days: int | None = None,
    ) -> int:
        """Remove stale files and empty directories from outputs scratch storage."""
        resolved_ttl_days = get_outputs_scratch_ttl_days() if ttl_days is None else ttl_days
        return _sweep_outputs_scratch_helper(outputs_root, resolved_ttl_days)

    def _get_next_curriculum_activity(self) -> str | None:
        """Retrieve the next activity description from the curriculum module.

        Returns:
            Activity description string, or ``None`` if none is available.
        """
        try:
            _require_module("vetinari.training.curriculum")
            from vetinari.training.curriculum import TrainingCurriculum

            curriculum = TrainingCurriculum()
            activity = curriculum.next_activity()
            if activity is None:
                logger.debug("_get_next_curriculum_activity: curriculum returned no activity")
                return self._fallback_activity_from_meta_optimizer()
            if isinstance(activity, str):
                return activity
            return str(getattr(activity, "description", activity))
        except ModuleNotFoundError:
            logger.warning(
                "_get_next_curriculum_activity: vetinari.training.curriculum not available yet",
            )
            return self._fallback_activity_from_meta_optimizer()
        except Exception:
            logger.exception("_get_next_curriculum_activity: error fetching curriculum activity")
            return None

    @staticmethod
    def _fallback_activity_from_meta_optimizer() -> str | None:
        """Ask the MetaOptimizer for the highest-ROI strategy when curriculum is unavailable.

        Returns:
            Activity description derived from the strategy suggestion, or None if
            the MetaOptimizer is unavailable or recommends halting.
        """
        try:
            _require_module("vetinari.learning.meta_optimizer")
            from vetinari.learning.meta_optimizer import MetaOptimizer

            suggestion = MetaOptimizer().suggest_next_strategy()
            if suggestion is None:
                logger.warning(
                    "_fallback_activity_from_meta_optimizer: MetaOptimizer recommends halting - collapse risk detected"
                )
                return None
            logger.info("_fallback_activity_from_meta_optimizer: MetaOptimizer suggests strategy=%s", suggestion)
            return f"MetaOptimizer-suggested activity: {suggestion}"
        except ModuleNotFoundError:
            logger.debug("MetaOptimizer not available - no fallback activity")
            return None
        except Exception:
            logger.warning(
                "Could not get MetaOptimizer strategy suggestion - idle cycle will be skipped",
                exc_info=True,
            )
            return None

    def _run_targeted_training(self, job: IdleTrainingJob) -> None:
        """Execute a manual training job for its requested task type.

        Args:
            job: The manual training job with a non-empty ``task_type``.

        Raises:
            RuntimeError: If training prerequisites are unavailable or the
                targeted training run fails.
        """
        if not job.task_type:
            raise RuntimeError("targeted training requires a task_type")

        from vetinari.training.pipeline import TrainingPipeline

        pipeline = TrainingPipeline()
        requirements = pipeline.check_requirements()
        if not requirements.get("ready_for_training"):
            raise RuntimeError(f"training requirements unavailable for task_type={job.task_type!r}")

        run = pipeline.run(base_model="auto", task_type=job.task_type)
        if not run.success:
            raise RuntimeError(f"targeted training failed for task_type={job.task_type!r}: {getattr(run, 'error', '')}")

        with self._lock:
            if self._current_job is not None and self._current_job.job_id == job.job_id:
                self._current_job.progress = 1.0

    def _run_activity(self, job: IdleTrainingJob) -> None:
        """Execute the training activity for the given job.

        Args:
            job: The :class:`TrainingJob` describing the work to perform.
        """
        try:
            _require_module("vetinari.training.curriculum")
            from vetinari.training.curriculum import TrainingCurriculum

            curriculum = TrainingCurriculum()
            if hasattr(curriculum, "run_activity"):
                curriculum.run_activity(job.activity_description, job_id=job.job_id)
                with self._lock:
                    if self._current_job is not None and self._current_job.job_id == job.job_id:
                        self._current_job.progress = 1.0
                return
        except ModuleNotFoundError:
            logger.warning(
                "_run_activity: vetinari.training.curriculum not available, logging activity only",
            )
        except Exception:
            logger.exception("_run_activity: error running curriculum activity, logging only")

        logger.info(
            "_run_activity: [TRAINING CYCLE] job=%s activity=%r (curriculum module pending)",
            job.job_id,
            job.activity_description,
        )
        with self._lock:
            if self._current_job is not None and self._current_job.job_id == job.job_id:
                self._current_job.progress = 1.0
