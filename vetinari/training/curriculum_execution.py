"""Execution helpers for selected training curriculum activities."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .curriculum_types import TrainingActivity, TrainingActivityType, _require_module, logger


class CurriculumExecutionMixin:
    """Run selected training activities through the appropriate pipeline."""

    if TYPE_CHECKING:
        next_activity: Any

    def run_activity(self, description: str, job_id: str | None = None) -> None:
        """Execute a training activity selected by the curriculum.

        Re-evaluates :meth:`next_activity` to get the full
        :class:`TrainingActivity` object, then delegates to the appropriate
        pipeline stage based on the activity type.

        Args:
            description: Human-readable description of the activity (used for
                logging; the actual activity is determined by next_activity).
            job_id: Optional job identifier for tracking.
        """
        activity = self.next_activity()
        logger.info(
            "run_activity: job=%s type=%s description=%s",
            job_id,
            activity.type.value,
            activity.description,
        )

        if activity.type in (
            TrainingActivityType.FINE_TUNE_WEAK_SKILL,
            TrainingActivityType.EXTERNAL_DATA_TRAINING,
            TrainingActivityType.RLEF_CODE_EXECUTION,
            TrainingActivityType.SELF_PLAY_REASONING,
        ):
            self._run_fine_tune(activity)
        elif activity.type == TrainingActivityType.DISTILLATION:
            self._run_distillation(activity)
        elif activity.type in (
            TrainingActivityType.BENCHMARK_PRACTICE,
            TrainingActivityType.PROMPT_EVOLUTION,
        ):
            logger.info(
                "run_activity: non-training activity %s - no model changes",
                activity.type.value,
            )
            if activity.type == TrainingActivityType.BENCHMARK_PRACTICE:
                try:
                    _require_module("vetinari.training.pipeline")
                    from vetinari.training.pipeline import BenchmarkTracker

                    BenchmarkTracker().record_run()
                    logger.info("run_activity: recorded benchmark run timestamp")
                except ModuleNotFoundError:
                    logger.debug("BenchmarkTracker not available - run timestamp not recorded")
                except Exception:
                    logger.warning(
                        "Could not record benchmark run timestamp - staleness detection may re-trigger too soon",
                        exc_info=True,
                    )
        else:
            logger.warning("run_activity: unknown activity type %s; skipping", activity.type.value)

    @staticmethod
    def _run_fine_tune(activity: TrainingActivity) -> None:
        """Execute a QLoRA fine-tuning run via the training pipeline.

        Args:
            activity: The training activity with metadata including task_type.
        """
        try:
            _require_module("vetinari.training.pipeline")
            _require_module("vetinari.training.external_data")
            from vetinari.training.external_data import DatasetSpec, ExternalDataManager
            from vetinari.training.pipeline import TrainingPipeline

            pipeline = TrainingPipeline()
            reqs = pipeline.check_requirements()
            if not reqs.get("ready_for_training", False):
                logger.warning("_run_fine_tune: training libraries not installed; skipping")
                return

            dataset_path = None
            if activity.type == TrainingActivityType.EXTERNAL_DATA_TRAINING:
                spec_payloads = activity.metadata.get("external_dataset_specs")
                if not isinstance(spec_payloads, list) or not spec_payloads:
                    raise RuntimeError("external-data activity lacks dataset specs")
                external_specs = [DatasetSpec(**payload) for payload in spec_payloads if isinstance(payload, dict)]
                if not external_specs:
                    raise RuntimeError("external-data activity has no valid dataset specs")
                manager = ExternalDataManager()
                dataset_path = str(manager.create_mixed_dataset(None, external_specs, ratio=0.0))

            task_type = activity.metadata.get("task_type") or activity.type.value
            run = pipeline.run(
                base_model="auto",
                task_type=task_type,
                min_score=0.8,
                dataset_path=dataset_path,
            )
            logger.info(
                "_run_fine_tune: completed - success=%s output=%s",
                run.success,
                run.output_model_path,
            )
        except ModuleNotFoundError:
            logger.warning("_run_fine_tune: training pipeline not available")
        except Exception:
            logger.exception("_run_fine_tune: unexpected error during training")

    @staticmethod
    def _run_distillation(activity: TrainingActivity) -> None:
        """Run knowledge distillation from high-quality outputs.

        Args:
            activity: The training activity with distillation metadata.
        """
        try:
            _require_module("vetinari.training.pipeline")
            import tempfile
            from pathlib import Path

            from vetinari.training.pipeline import (
                ContextDistillationDatasetBuilder,
                TrainingPipeline,
            )

            builder = ContextDistillationDatasetBuilder()
            _distill_dir = Path(tempfile.mkdtemp(prefix="vetinari_distill_"))
            dataset_info = builder.build_dataset(output_path=str(_distill_dir / "distillation.jsonl"))
            if not dataset_info or dataset_info.num_examples == 0:
                logger.info("_run_distillation: no distillation data available; skipping")
                return

            pipeline = TrainingPipeline()
            reqs = pipeline.check_requirements()
            if not reqs.get("ready_for_training", False):
                logger.warning("_run_distillation: training libraries not installed; skipping")
                return

            run = pipeline.run(
                base_model="auto", min_score=0.85, output_base_dir=str(Path(dataset_info.output_path).parent)
            )
            logger.info(
                "_run_distillation: completed - success=%s output=%s",
                run.success,
                run.output_model_path,
            )
        except ModuleNotFoundError:
            logger.warning("_run_distillation: pipeline modules not available")
        except Exception:
            logger.exception("_run_distillation: unexpected error during distillation")

        try:
            _require_module("vetinari.training.synthetic_data")
            from vetinari.training.synthetic_data import StrategyDistiller

            distiller = StrategyDistiller()
            strategies = distiller.distill_strategies(min_score=0.8)
            if strategies:
                stored = distiller.store_strategies(strategies)
                logger.info(
                    "_run_distillation: stored %d/%d distilled strategies as synthetic episodes",
                    stored,
                    len(strategies),
                )
        except ModuleNotFoundError:
            logger.debug("StrategyDistiller not available - strategy extraction skipped")
        except Exception:
            logger.warning(
                "Could not run strategy distillation after training - strategies will not be stored",
                exc_info=True,
            )
