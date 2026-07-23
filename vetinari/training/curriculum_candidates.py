"""Candidate builders for training curriculum scheduling."""

from __future__ import annotations

from dataclasses import asdict

from .curriculum_types import (
    BENCHMARK_STALENESS_DAYS,
    DEFAULT_DURATION_MINUTES,
    DEFAULT_VRAM_GB,
    DEFECT_THRESHOLD,
    DISTILLATION_QUALITY_THRESHOLD,
    MIN_REASONING_EPISODES,
    MIN_RLEF_TRACES,
    WEAK_SKILL_THRESHOLD,
    TrainingActivity,
    TrainingActivityType,
    _require_module,
    logger,
)


class CurriculumCandidateError(RuntimeError):
    """Raised when a candidate signal source fails instead of proving absence."""


class CurriculumCandidateMixin:
    """Build candidate training activities from available system signals."""

    @staticmethod
    def _candidate_weak_skill() -> TrainingActivity | None:
        """Return a fine-tune activity for the weakest skill, or None.

        Returns:
            A FINE_TUNE_WEAK_SKILL activity if a skill scores below the
            threshold, otherwise None.
        """
        try:
            _require_module("vetinari.learning.model_selector")
            from vetinari.learning.model_selector import get_skill_rankings

            rankings = get_skill_rankings()
        except ModuleNotFoundError:
            logger.debug("model_selector not available; skipping weak-skill candidate")
            return None
        except Exception as exc:
            raise CurriculumCandidateError("failed to get skill rankings") from exc

        if not rankings:
            return None

        try:
            worst = min(rankings, key=lambda r: r.get("score", 1.0))
            score = worst.get("score", 1.0)
            task_type = worst.get("task_type", "unknown")
        except (TypeError, KeyError) as exc:
            raise CurriculumCandidateError("unexpected skill rankings format") from exc

        if score >= WEAK_SKILL_THRESHOLD:
            return None

        return TrainingActivity(
            type=TrainingActivityType.FINE_TUNE_WEAK_SKILL,
            description=f"Fine-tune weak skill: {task_type} (score={score:.2f})",
            hypothesis=(
                f"Targeted QLoRA fine-tuning on {task_type} examples will raise "
                f"skill score from {score:.2f} toward {WEAK_SKILL_THRESHOLD}."
            ),
            metric=f"skill_score.{task_type}",
            baseline=score,
            target=WEAK_SKILL_THRESHOLD,
            rollback_plan="Revert to previous adapter checkpoint; re-run benchmarks to confirm.",
            estimated_duration_minutes=120,
            estimated_vram_gb=16.0,
            priority=0.9,
            metadata={"task_type": task_type, "all_rankings": rankings},
        )

    @staticmethod
    def _candidate_defect_pattern() -> TrainingActivity | None:
        """Return a fine-tune activity for the top recurring defect, or None.

        Returns:
            A FINE_TUNE_WEAK_SKILL activity targeting the dominant defect
            pattern if it exceeds the threshold, otherwise None.
        """
        try:
            _require_module("vetinari.kaizen.improvement_log")
            from vetinari.kaizen.improvement_log import ImprovementLog

            log = ImprovementLog()
            top = log.get_top_defect_pattern()
        except ModuleNotFoundError:
            logger.debug("kaizen improvement_log not available; skipping defect-pattern candidate")
            return None
        except Exception as exc:
            raise CurriculumCandidateError("failed to get top defect pattern") from exc

        if top is None:
            return None

        try:
            count = top.get("count", 0)
            pattern = top.get("pattern", "unknown")
        except (TypeError, AttributeError) as exc:
            raise CurriculumCandidateError("unexpected defect pattern format") from exc

        if count <= DEFECT_THRESHOLD:
            return None

        return TrainingActivity(
            type=TrainingActivityType.FINE_TUNE_WEAK_SKILL,
            description=f"Address defect pattern: {pattern} ({count} occurrences)",
            hypothesis=(
                f"Training on corrected examples for defect '{pattern}' will reduce "
                f"recurrence rate. Pattern exceeds threshold of {DEFECT_THRESHOLD}."
            ),
            metric=f"defect_rate.{pattern}",
            baseline=float(count),
            target=float(DEFECT_THRESHOLD - 1),
            rollback_plan="Revert adapter; monitor defect rate over next 24h to confirm regression.",
            estimated_duration_minutes=90,
            estimated_vram_gb=16.0,
            priority=0.85,
            metadata={"defect_pattern": pattern, "occurrence_count": count},
        )

    @staticmethod
    def _candidate_self_play() -> TrainingActivity | None:
        """Return a self-play reasoning activity if enough episodes exist.

        Returns:
            A SELF_PLAY_REASONING activity if episode count meets the
            minimum threshold, otherwise None.
        """
        try:
            _require_module("vetinari.learning.training_data")
            from vetinari.learning.training_data import get_training_collector

            collector = get_training_collector()
            episode_count = collector.count_reasoning_episodes()
        except ModuleNotFoundError:
            logger.debug("training_data collector not available; skipping self-play candidate")
            return None
        except Exception as exc:
            raise CurriculumCandidateError("failed to count reasoning episodes") from exc

        if episode_count < MIN_REASONING_EPISODES:
            return None

        return TrainingActivity(
            type=TrainingActivityType.SELF_PLAY_REASONING,
            description=f"Self-play reasoning training on {episode_count} episodes",
            hypothesis=(
                "Training on accumulated reasoning episode traces will improve "
                "multi-step planning accuracy and reduce chain-of-thought errors."
            ),
            metric="reasoning_episode_count",
            baseline=float(episode_count),
            target=float(MIN_REASONING_EPISODES),
            rollback_plan="Revert to pre-training checkpoint; compare benchmark suite scores.",
            estimated_duration_minutes=180,
            estimated_vram_gb=20.0,
            priority=0.7,
            metadata={"episode_count": episode_count},
        )

    @staticmethod
    def _candidate_external_data() -> TrainingActivity | None:
        """Return an external data training activity if datasets are available.

        Returns:
            An EXTERNAL_DATA_TRAINING activity when external datasets are
            registered, otherwise None.
        """
        try:
            _require_module("vetinari.training.external_data")
            from vetinari.training.external_data import ExternalDataManager

            manager = ExternalDataManager()
            datasets = manager.get_available_datasets()
        except ModuleNotFoundError:
            logger.debug("ExternalDataManager not available; skipping external-data candidate")
            return None
        except Exception as exc:
            raise CurriculumCandidateError("failed to list external datasets") from exc

        if not datasets:
            return None

        dataset_names = [getattr(d, "name", "unknown") for d in datasets] if isinstance(datasets, list) else []
        dataset_keys = {(getattr(d, "name", None), getattr(d, "domain", None)) for d in datasets}
        dataset_specs = [
            spec
            for specs in manager.DATASET_CATALOG.values()
            for spec in specs
            if (spec.name, spec.domain) in dataset_keys
        ]
        if not dataset_specs:
            raise CurriculumCandidateError("external datasets listed without matching training specs")

        return TrainingActivity(
            type=TrainingActivityType.EXTERNAL_DATA_TRAINING,
            description=f"Train on {len(dataset_names)} external dataset(s): {', '.join(dataset_names[:3])}",
            hypothesis=(
                "Incorporating curated external data will broaden generalisation "
                "without degrading task-specific performance."
            ),
            metric="external_training_dataset_count",
            baseline=float(len(dataset_specs)),
            target=float(len(dataset_specs)),
            rollback_plan="Revert adapter; re-run held-out evaluation set to confirm regression.",
            estimated_duration_minutes=240,
            estimated_vram_gb=18.0,
            priority=0.6,
            metadata={
                "dataset_names": dataset_names,
                "dataset_count": len(dataset_specs),
                "external_dataset_specs": [asdict(spec) for spec in dataset_specs],
            },
        )

    @staticmethod
    def _candidate_prompt_evolution() -> TrainingActivity | None:
        """Return a prompt evolution activity if A/B tests are pending.

        Returns:
            A PROMPT_EVOLUTION activity when the prompt evolver has
            unresolved A/B tests, otherwise None.
        """
        try:
            _require_module("vetinari.learning.prompt_evolver")
            from vetinari.learning.prompt_evolver import PromptEvolver

            evolver = PromptEvolver()
            pending = evolver.get_pending_ab_tests()
        except ModuleNotFoundError:
            logger.debug("prompt_evolver not available; skipping prompt-evolution candidate")
            return None
        except Exception as exc:
            raise CurriculumCandidateError("failed to get pending A/B tests") from exc

        if not pending:
            return None

        return TrainingActivity(
            type=TrainingActivityType.PROMPT_EVOLUTION,
            description=f"Resolve {len(pending)} pending prompt A/B test(s)",
            hypothesis=(
                "Running prompt evolution trials on pending A/B tests will identify "
                "higher-performing prompt variants and improve output quality."
            ),
            metric="prompt_win_rate",
            baseline=0.5,
            target=0.65,
            rollback_plan="Revert to control prompt variants; A/B framework records all candidates.",
            estimated_duration_minutes=60,
            estimated_vram_gb=8.0,
            priority=0.5,
            metadata={"pending_test_count": len(pending)},
        )

    @staticmethod
    def _candidate_benchmark_practice() -> TrainingActivity | None:
        """Return a benchmark practice activity if benchmarks are stale.

        Returns:
            A BENCHMARK_PRACTICE activity when the last benchmark run
            exceeds the staleness threshold, otherwise None.
        """
        try:
            _require_module("vetinari.training.pipeline")
            from vetinari.training.pipeline import BenchmarkTracker

            tracker = BenchmarkTracker()
            staleness_days = tracker.days_since_last_run()
        except ModuleNotFoundError:
            logger.debug("BenchmarkTracker not available; skipping benchmark-practice candidate")
            return None
        except Exception as exc:
            raise CurriculumCandidateError("failed to check benchmark staleness") from exc

        if staleness_days < BENCHMARK_STALENESS_DAYS:
            return None

        return TrainingActivity(
            type=TrainingActivityType.BENCHMARK_PRACTICE,
            description=f"Run benchmark suite (last run {staleness_days} days ago)",
            hypothesis=(
                "Regular benchmark practice surfaces capability drift early and "
                "provides fresh baseline data for subsequent training decisions."
            ),
            metric="benchmark_staleness_days",
            baseline=float(staleness_days),
            target=0.0,
            rollback_plan="No model changes; benchmark data is append-only.",
            estimated_duration_minutes=45,
            estimated_vram_gb=6.0,
            priority=0.4,
            metadata={"staleness_days": staleness_days},
        )

    @staticmethod
    def _candidate_distillation() -> TrainingActivity | None:
        """Return a distillation activity if quality cloud outputs are available.

        Returns:
            A DISTILLATION activity when cloud model outputs meet the
            quality threshold, otherwise None.
        """
        try:
            _require_module("vetinari.training.pipeline")
            from vetinari.training.pipeline import CloudOutputStore

            store = CloudOutputStore()
            outputs = store.get_high_quality_outputs(min_score=DISTILLATION_QUALITY_THRESHOLD)
        except ModuleNotFoundError:
            logger.debug("CloudOutputStore not available; skipping distillation candidate")
            return None
        except Exception as exc:
            raise CurriculumCandidateError("failed to get cloud outputs") from exc

        if not outputs:
            return None

        average_score = sum(float(output.get("score", 0.0)) for output in outputs) / len(outputs)

        return TrainingActivity(
            type=TrainingActivityType.DISTILLATION,
            description=f"Distill from {len(outputs)} high-quality cloud model output(s)",
            hypothesis=(
                f"Knowledge distillation from cloud outputs (score >= {DISTILLATION_QUALITY_THRESHOLD}) "
                "will transfer strong reasoning capabilities to the local model."
            ),
            metric="cloud_output_quality_score",
            baseline=average_score,
            target=DISTILLATION_QUALITY_THRESHOLD,
            rollback_plan="Revert adapter; verify local benchmark does not degrade vs. pre-distillation.",
            estimated_duration_minutes=150,
            estimated_vram_gb=20.0,
            priority=0.65,
            metadata={"output_count": len(outputs), "average_cloud_output_score": average_score},
        )

    @staticmethod
    def _candidate_rlef() -> TrainingActivity | None:
        """Return an RLEF code-execution activity if enough traces exist.

        Reinforcement Learning from Execution Feedback uses sandbox
        code-execution outcomes (pass/fail, runtime errors, test results) as
        a reward signal to fine-tune the model on coding tasks.

        Returns:
            An RLEF_CODE_EXECUTION activity when execution traces meet the
            minimum threshold, otherwise None.
        """
        try:
            _require_module("vetinari.learning.training_data")
            from vetinari.learning.training_data import get_training_collector

            collector = get_training_collector()
            trace_count = collector.count_execution_traces()
        except ModuleNotFoundError:
            logger.debug("training_data collector not available; skipping RLEF candidate")
            return None
        except AttributeError:
            logger.debug("count_execution_traces not implemented; skipping RLEF candidate")
            return None
        except Exception as exc:
            raise CurriculumCandidateError("failed to count execution traces") from exc

        if trace_count < MIN_RLEF_TRACES:
            return None

        return TrainingActivity(
            type=TrainingActivityType.RLEF_CODE_EXECUTION,
            description=f"RLEF training on {trace_count} code-execution traces",
            hypothesis=(
                "Using sandbox execution outcomes (pass/fail, runtime errors) "
                "as reward signal will improve code generation correctness."
            ),
            metric="execution_trace_count",
            baseline=float(trace_count),
            target=float(MIN_RLEF_TRACES),
            rollback_plan="Revert adapter; compare pass@1 on held-out coding problems.",
            estimated_duration_minutes=150,
            estimated_vram_gb=18.0,
            priority=0.75,
            metadata={"trace_count": trace_count},
        )

    @staticmethod
    def _default_activity() -> TrainingActivity:
        """Return a calibration benchmark activity when no candidates apply.

        Returns:
            A BENCHMARK_PRACTICE activity that establishes calibration baselines.
        """
        return TrainingActivity(
            type=TrainingActivityType.BENCHMARK_PRACTICE,
            description="Run calibration benchmarks to establish performance baselines",
            hypothesis=(
                "Running the full calibration benchmark suite will establish baseline "
                "metrics needed to identify future training priorities."
            ),
            metric="benchmark_suite_score",
            baseline=0.0,
            target=0.0,
            rollback_plan="No model changes made; safe to run at any time.",
            estimated_duration_minutes=DEFAULT_DURATION_MINUTES,
            estimated_vram_gb=DEFAULT_VRAM_GB,
            priority=0.3,
            metadata={"reason": "no_higher_priority_candidates"},
        )
