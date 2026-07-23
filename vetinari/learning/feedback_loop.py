"""Feedback Loop - Vetinari Self-Improvement Subsystem.

Closes the learning loop: execution outcomes → model performance updates.
Feeds quality scores back into ModelPerformance table and DynamicModelRouter.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone

from vetinari.boundary_guards import account_evidence_drop
from vetinari.learning.feedback_loop_replay import FeedbackLoopReplayMixin
from vetinari.learning.feedback_store import FeedbackStore, FeedbackStoreCorrupt, get_feedback_store
from vetinari.memory.plan_pruning import PLAN_RETENTION_DAYS

logger = logging.getLogger(__name__)

FEEDBACK_LOOP_LIFECYCLE = {
    "store_id": "feedback_loop_model_performance_signals",
    "retention_days": PLAN_RETENTION_DAYS,
    "redaction_applied": True,
    "deletion_mechanism": "MemoryStore.prune_old_plans and FeedbackStore retention controls",
    "owner_ref": "docs/security/data-inventory.json#feedback_loop_model_performance_signals",
}


class FeedbackLoop(FeedbackLoopReplayMixin):
    """Closes the feedback loop between execution outcomes and model selection.

    After each task completes, this component:
    1. Fetches the quality score for the task
    2. Updates ModelPerformance in the memory store (running EMA)
    3. Updates the DynamicModelRouter performance cache
    4. Optionally updates the Ponder scoring system
    """

    EMA_ALPHA = 0.2  # Exponential moving average weight — 0.3 was too reactive to single outliers

    DRIFT_REMEDIATION_THRESHOLD = 3  # Consecutive drifts before triggering model scout

    def __init__(
        self,
        store: FeedbackStore | None = None,
        *,
        rehydrate_drift_counts: bool | None = None,
    ):
        self._memory = None
        self._router = None
        explicit_store = store is not None
        self._store = store if explicit_store else get_feedback_store()
        self._consecutive_drift_counts: dict[str, int] = {}  # task_type → consecutive drift count
        if rehydrate_drift_counts is None:
            rehydrate_drift_counts = explicit_store
        self._consecutive_drift_counts = self._store.get_all_drift_counts() if rehydrate_drift_counts else {}
        self._drift_lock = threading.Lock()  # Protects _consecutive_drift_counts

    _MAX_INIT_RETRIES = 2  # Number of retry attempts for lazy init

    def _get_memory(self):
        if self._memory is None:
            for attempt in range(1, self._MAX_INIT_RETRIES + 1):
                try:
                    from vetinari.memory import get_memory_store

                    self._memory = get_memory_store()
                    break
                except Exception:
                    logger.warning(
                        "Failed to initialize memory store for feedback loop (attempt %d/%d)",
                        attempt,
                        self._MAX_INIT_RETRIES,
                        exc_info=True,
                    )
        return self._memory

    def _get_router(self):
        if self._router is None:
            for attempt in range(1, self._MAX_INIT_RETRIES + 1):
                try:
                    from vetinari.models.dynamic_model_router import get_model_router

                    self._router = get_model_router()
                    break
                except Exception:
                    logger.warning(
                        "Failed to initialize model router for feedback loop (attempt %d/%d)",
                        attempt,
                        self._MAX_INIT_RETRIES,
                        exc_info=True,
                    )
        return self._router

    def record_outcome(
        self,
        task_id: str,
        model_id: str,
        task_type: str,
        quality_score: float,
        latency_ms: int | None = 0,
        cost_usd: float = 0.0,
        success: bool = True,
        benchmark_result: dict | None = None,
        episode_id: str | None = None,
        *,
        confidence: float | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
    ) -> None:
        """Record a task outcome and propagate it to all performance tracking systems.

        Args:
                    task_id: The task identifier.
                    model_id: The model used.
                    task_type: Type of task (coding, research, etc.).
                    quality_score: Overall quality score 0.0-1.0.
                    latency_ms: Execution latency in milliseconds.
                    cost_usd: Estimated cost in USD.
                    success: Whether the task succeeded.
                    benchmark_result: Optional benchmark data dict with keys:
                        - pass_rate (float): 0.0-1.0 fraction of benchmark cases passed
                        - task_type (str): benchmark task category
                        - suite_name (str): benchmark suite identifier
                        - n_trials (int): number of benchmark trials run
                        - avg_score (float): average benchmark score
                    episode_id: Canonical episode id to attach candidate feedback to.
                    confidence: Typed inference confidence used for reward shaping.
                    input_tokens: Exact prompt token count used for reward shaping.
                    output_tokens: Exact completion token count used for reward shaping.

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        logger.debug(
            "[FeedbackLoop] Recording outcome: task=%s model=%s type=%s quality=%.2f success=%s",
            task_id,
            model_id,
            task_type,
            quality_score,
            success,
        )

        operational_latency_ms = latency_ms or 0

        # 1. Update memory store ModelPerformance
        self._update_memory_performance(model_id, task_type, quality_score, operational_latency_ms, success)

        # 2. Update DynamicModelRouter performance cache
        self._update_router_cache(model_id, task_type, quality_score, operational_latency_ms, success)

        # 3. Update SubtaskMemory outcome with quality score
        self._update_subtask_quality(task_id, quality_score, success)

        # 4. Update Thompson Sampling arms
        self._update_thompson_arms(
            model_id,
            task_type,
            quality_score,
            success,
            confidence=confidence,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            latency_ms=latency_ms,
        )

        # 5. If benchmark_result provided, feed it into model selection and learning
        if benchmark_result:
            self._process_benchmark_result(model_id, task_type, benchmark_result)

        self._process_quality_drift(task_type, model_id, quality_score)
        self._record_implicit_feedback(task_id, model_id, task_type, quality_score, success)
        self._record_episode_feedback(
            episode_id=episode_id or task_id,
            task_id=task_id,
            model_id=model_id,
            task_type=task_type,
            quality_score=quality_score,
            success=success,
            user_feedback=(benchmark_result or {}).get("user_feedback") if benchmark_result else None,
        )

    def _process_quality_drift(self, task_type: str, model_id: str, quality_score: float) -> None:
        """Feed quality score into drift detection and remediation."""
        try:
            from vetinari.analytics.quality_drift import get_drift_ensemble

            result = get_drift_ensemble().observe(quality_score)
            if result.is_drift:
                logger.warning(
                    "Quality drift detected for task_type=%s — flagging for curriculum review (%d/3 detectors: %s)",
                    task_type,
                    result.votes,
                    ", ".join(result.detectors_triggered),
                )
                self._handle_drift_remediation(task_type, model_id)
            else:
                # Reset consecutive drift counter on non-drift observation
                with self._drift_lock:
                    self._consecutive_drift_counts.pop(task_type, None)
                    self._store.set_drift_count(task_type, 0)
        except FeedbackStoreCorrupt:
            raise
        except Exception:
            # Drift detection is advisory; failures must not interrupt the feedback loop
            logger.warning(
                "Drift ensemble update skipped — drift detection unavailable, proceeding without drift signal",
                exc_info=True,
            )

    @staticmethod
    def _record_implicit_feedback(
        task_id: str,
        model_id: str,
        task_type: str,
        quality_score: float,
        success: bool,
    ) -> None:
        """Feed task outcome into the implicit feedback collector."""
        try:
            from vetinari.learning.implicit_feedback import get_implicit_feedback_collector
            from vetinari.types import FeedbackAction

            action = FeedbackAction.ACCEPTED if success and quality_score >= 0.5 else FeedbackAction.REGENERATED
            get_implicit_feedback_collector().record(
                task_id=task_id,
                model_id=model_id,
                task_type=task_type,
                action=action,
                inspector_score=quality_score,
            )
        except FeedbackStoreCorrupt:
            raise
        except Exception:
            account_evidence_drop(task_id, "feedback_loop", logger=logger)
            logger.error("Implicit feedback recording failed for task %s", task_id, exc_info=True)
            raise

    def record_benchmark_outcome(
        self,
        model_id: str,
        benchmark_result: dict,
    ) -> None:
        """Record a standalone benchmark outcome (not tied to a specific task).

        Extracts pass_rate and task_type from the benchmark result and updates
        all performance tracking systems with benchmark-weighted signals.

        Args:
            model_id: The model that was benchmarked.
            benchmark_result: Dict with keys:
                - pass_rate (float): 0.0-1.0 fraction of cases passed
                - task_type (str): benchmark task category
                - suite_name (str): benchmark suite identifier
                - n_trials (int): number of benchmark trials run
                - avg_score (float): average benchmark score
        """
        task_type = benchmark_result.get("task_type", "general")
        pass_rate = benchmark_result.get("pass_rate", 0.0)

        logger.debug(
            "[FeedbackLoop] Recording benchmark outcome: model=%s type=%s pass_rate=%.2f",
            model_id,
            task_type,
            pass_rate,
        )

        self._process_benchmark_result(model_id, task_type, benchmark_result)

    def _process_benchmark_result(self, model_id: str, task_type: str, benchmark_result: dict) -> None:
        """Process a benchmark result and propagate to all learning subsystems."""
        pass_rate = float(benchmark_result.get("pass_rate", 0.0))
        n_trials = int(benchmark_result.get("n_trials", 1))
        avg_score = float(benchmark_result.get("avg_score", pass_rate))
        suite_name = benchmark_result.get("suite_name", "unknown")
        bench_task_type = benchmark_result.get("task_type", task_type)

        # Update Thompson Sampling with 3x weight (benchmarks are more reliable)
        try:
            from vetinari.learning.model_selector import get_thompson_selector

            get_thompson_selector().update_from_benchmark(
                model_id=model_id,
                pass_rate=pass_rate,
                n_trials=n_trials,
                task_type=bench_task_type,
            )
        except Exception as e:
            logger.warning("Thompson benchmark update failed: %s", e)

        # Update memory store with benchmark-informed performance
        try:
            mem = self._get_memory()
            if mem:
                existing = mem.get_model_performance(model_id, bench_task_type) or {}
                old_rate = existing.get("success_rate", 0.7)
                # Benchmark signals get higher EMA weight
                benchmark_ema = 0.5
                new_rate = (1 - benchmark_ema) * old_rate + benchmark_ema * pass_rate
                mem.update_model_performance(
                    model_id,
                    bench_task_type,
                    {
                        "success_rate": round(new_rate, 4),
                        "benchmark_pass_rate": round(pass_rate, 4),
                        "benchmark_avg_score": round(avg_score, 4),
                        "benchmark_suite": suite_name,
                        "total_uses": existing.get("total_uses", 0),
                    },
                )
        except Exception as e:
            logger.warning("Memory benchmark update failed: %s", e)

    def _handle_drift_remediation(self, task_type: str, model_id: str) -> None:
        """Respond to detected quality drift with escalating remediation.

        Tracks consecutive drift detections per task_type. On each drift:
        1. Increases calibration frequency via the drift ensemble
        2. After DRIFT_REMEDIATION_THRESHOLD consecutive drifts, triggers
           the model scout to search for better-performing models.

        Args:
            task_type: The task type experiencing drift.
            model_id: The model that produced the drifting output.
        """
        # Lock order: FeedbackLoop drift lock first, then FeedbackStore's write lock.
        with self._drift_lock:
            count = self._consecutive_drift_counts.get(task_type, 0) + 1
            self._consecutive_drift_counts[task_type] = count
            self._store.set_drift_count(task_type, count)

        # Increase calibration frequency so drift detection becomes more sensitive
        try:
            from vetinari.analytics.quality_drift import get_drift_ensemble

            ensemble = get_drift_ensemble()
            if hasattr(ensemble, "increase_sensitivity"):
                ensemble.increase_sensitivity(task_type)
        except Exception:
            logger.warning(
                "Drift ensemble unavailable for %s — skipping sensitivity increase",
                task_type,
            )

        # After persistent drift, trigger model scout to find alternatives
        if count >= self.DRIFT_REMEDIATION_THRESHOLD:
            logger.warning(
                "Persistent quality drift for task_type=%s (%d consecutive) — triggering model scout",
                task_type,
                count,
            )
            try:
                from vetinari.models.model_scout import ModelFreshnessChecker

                checker = ModelFreshnessChecker()
                upgrades = checker.check_for_upgrades()
                if upgrades:
                    logger.info(
                        "Model scout found %d upgrade candidate(s) for drifting task_type=%s",
                        len(upgrades),
                        task_type,
                    )
            except Exception as exc:
                logger.warning(
                    "Model scout trigger failed for task_type=%s — manual review recommended: %s",
                    task_type,
                    exc,
                )

    def _update_memory_performance(
        self,
        model_id: str,
        task_type: str,
        quality: float,
        latency_ms: int,
        success: bool,
    ) -> None:
        """Update the ModelPerformance running averages in the memory store."""
        mem = self._get_memory()
        if mem is None:
            return
        try:
            existing = mem.get_model_performance(model_id, task_type) or {}
            old_rate = existing.get("success_rate", 0.7)
            old_latency = existing.get("avg_latency", latency_ms or 1000)
            old_uses = existing.get("total_uses", 0)

            # Exponential moving average update — blend success and quality into
            # a single signal to avoid double-counting
            signal = 0.5 * (1.0 if success else 0.0) + 0.5 * quality
            new_rate = (1 - self.EMA_ALPHA) * old_rate + self.EMA_ALPHA * signal
            # Guard against zero/None latency — use existing average when no
            # meaningful measurement is available (prevents 0ms propagation)
            effective_latency = latency_ms if latency_ms and latency_ms > 0 else old_latency
            new_latency = (1 - self.EMA_ALPHA) * old_latency + self.EMA_ALPHA * effective_latency

            # Pass dict-form update (new signature)
            mem.update_model_performance(
                model_id,
                task_type,
                {
                    "success_rate": round(new_rate, 4),
                    "avg_latency": max(1, int(new_latency)),
                    "total_uses": old_uses + 1,
                },
            )
        except Exception as e:
            logger.warning("Memory performance update failed: %s", e)

    def _update_router_cache(
        self,
        model_id: str,
        task_type: str,
        quality: float,
        latency_ms: int,
        success: bool,
    ) -> None:
        """Update the DynamicModelRouter's in-memory performance cache."""
        router = self._get_router()
        if router is None:
            return
        try:
            cache_key = f"{model_id}:{task_type}"
            existing = router.get_performance_cache(cache_key)
            old_rate = existing.get("success_rate", 0.7)
            old_latency = existing.get("avg_latency_ms", latency_ms or 1000)

            signal = 0.5 * float(success) + 0.5 * quality
            new_rate = (1 - self.EMA_ALPHA) * old_rate + self.EMA_ALPHA * signal
            effective_latency = latency_ms if latency_ms and latency_ms > 0 else old_latency
            new_latency = (1 - self.EMA_ALPHA) * old_latency + self.EMA_ALPHA * effective_latency

            router.update_performance_cache(
                cache_key,
                {
                    "success_rate": round(new_rate, 4),
                    "avg_latency_ms": max(1, int(new_latency)),
                    "quality_score": round(quality, 4),
                    "last_updated": datetime.now(timezone.utc).isoformat(),
                },
            )
        except Exception as e:
            logger.warning("Router cache update failed: %s", e)

    def _update_subtask_quality(self, task_id: str, quality: float, success: bool) -> None:
        """Annotate the SubtaskMemory record with quality score."""
        mem = self._get_memory()
        if mem is None:
            return
        try:
            mem.update_subtask_quality(task_id, quality_score=quality, succeeded=success)
        except Exception as e:
            logger.warning("Subtask quality update failed: %s", e)

    @staticmethod
    def _update_thompson_arms(
        model_id: str,
        task_type: str,
        quality: float,
        success: bool,
        *,
        confidence: float | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        latency_ms: int | None = None,
    ) -> None:
        """Update Thompson Sampling arm for this model+task_type pair."""
        try:
            from vetinari.learning.model_selector import get_thompson_selector

            get_thompson_selector().update(
                model_id,
                task_type,
                quality,
                success,
                confidence=confidence,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                latency_ms=latency_ms,
            )
        except Exception as e:
            logger.warning("Thompson arm update failed: %s", e)

    @staticmethod
    def _record_episode_feedback(
        *,
        episode_id: str,
        task_id: str,
        model_id: str,
        task_type: str,
        quality_score: float,
        success: bool,
        user_feedback: str | None = None,
    ) -> None:
        """Attach helpful/harmful candidate feedback to the created episode."""
        try:
            from vetinari.memory.episodic import EpisodeFeedback, EpisodeFeedbackLabel, UnifiedEpisodicMemory

            label = EpisodeFeedbackLabel.HELPFUL if success and quality_score >= 0.5 else EpisodeFeedbackLabel.HARMFUL
            UnifiedEpisodicMemory().add_feedback(
                episode_id,
                EpisodeFeedback(
                    label=label,
                    source="feedback_loop",
                    task_id=task_id,
                    model_id=model_id,
                    task_type=task_type,
                    inspector_score=quality_score,
                    user_feedback=user_feedback,
                    metadata={"authority": "candidate_evidence_only"},
                ),
            )
        except Exception:
            account_evidence_drop(task_id, "feedback_loop", logger=logger)
            logger.error("Episode feedback metadata failed for task %s", task_id, exc_info=True)
            raise


# Singleton
_feedback_loop: FeedbackLoop | None = None
_feedback_loop_lock = threading.Lock()


def get_feedback_loop() -> FeedbackLoop:
    """Return the singleton FeedbackLoop instance (thread-safe).

    On first construction the instance replays any persisted feedback from
    ``outputs/feedback/feedback.jsonl`` so that thumbs-up/down signals
    survive process restarts and bootstrap the learning subsystems.

    Returns:
        The shared FeedbackLoop instance.
    """
    global _feedback_loop
    if _feedback_loop is None:
        with _feedback_loop_lock:
            if _feedback_loop is None:
                _feedback_loop = FeedbackLoop(rehydrate_drift_counts=True)
                # Replay persisted feedback so learning signals survive restarts.
                try:
                    from vetinari.constants import _PROJECT_ROOT

                    _feedback_path = _PROJECT_ROOT / "outputs" / "feedback" / "feedback.jsonl"
                    _feedback_loop.load_feedback_jsonl(_feedback_path)
                except Exception:
                    logger.warning("Feedback JSONL replay skipped (non-fatal)", exc_info=True)
    return _feedback_loop
