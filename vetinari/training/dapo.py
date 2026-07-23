"""DAPO training stage orchestrator.

Orchestrates training stages using the reward system defined in dapo_rewards.py
and the stage execution functions in dapo_stages.py.
Re-exports all public types for backward compatibility.

Reward tiers (additive, sum to 1.0):
  Tier 1: Execution pass/fail     — 0.10 (free, instant)
  Tier 2: Test pass rate           — 0.25 (free, ~seconds)
  Tier 3: Static analysis score    — 0.15 (free, instant)
  Tier 4: Heuristic pre-screen     — 0.20 (~ms, cheap ML)
  Tier 5: LLM-as-judge score      — 0.20 (expensive, tokens)
  Efficiency bonus (no rework)     — 0.10
"""

from __future__ import annotations

import json as _json
import logging
from pathlib import Path
from typing import Any

from vetinari.training.dapo_rewards import (
    TIER_EFFICIENCY_WEIGHT,
    TIER_EXECUTION_WEIGHT,
    TIER_HEURISTIC_WEIGHT,
    TIER_LLM_JUDGE_WEIGHT,
    TIER_STATIC_WEIGHT,
    TIER_TEST_WEIGHT,
    DapoExecutionResult,
    DapoTrainingResult,
    RewardBreakdown,
    StageResult,
    compute_dapo_reward,
)
from vetinari.training.dapo_stages import run_dapo_stage, run_sft_stage, run_simpo_stage
from vetinari.types import StatusEnum

logger = logging.getLogger(__name__)

DEFAULT_MAX_EVAL_LOSS = 5.0
DEFAULT_MAX_TRAIN_LOSS = 10.0
DEFAULT_LOSS_REGRESSION_MULTIPLIER = 3.0
DEFAULT_LOSS_REGRESSION_FLOOR_DELTA = 0.5

__all__ = [
    "TIER_EFFICIENCY_WEIGHT",
    "TIER_EXECUTION_WEIGHT",
    "TIER_HEURISTIC_WEIGHT",
    "TIER_LLM_JUDGE_WEIGHT",
    "TIER_STATIC_WEIGHT",
    "TIER_TEST_WEIGHT",
    "DapoExecutionResult",
    "DapoTrainingResult",
    "RewardBreakdown",
    "StageResult",
    "TrainingStageOrchestrator",
    "compute_dapo_reward",
]


class TrainingStageOrchestrator:
    """Manages the SFT -> SimPO -> DAPO training pipeline with validation gates.

    Runs stages sequentially with inter-stage validation to catch
    regressions early. Each stage produces a model checkpoint that
    feeds into the next.
    """

    STAGES = ["sft", "simpo", "dapo"]

    def run_pipeline(
        self,
        base_model: str,
        dataset_path: Path,
        config: dict[str, Any] | None = None,
        cpu_tier: Any | None = None,
        release_timeout_s: float | None = None,
    ) -> DapoTrainingResult:
        """Run the full training pipeline with inter-stage validation.

        Args:
                    base_model: Path or identifier of the base model.
                    dataset_path: Path to the training dataset.
                    config: Optional stage-specific configuration overrides.
                    cpu_tier: Optional CPU tier implementing request_release/release_finished.
                    release_timeout_s: Optional timeout for CPU-tier release.

        Returns:
                    TrainingResult with success/failure and stage details.

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        config = config or {}
        cpu_tier = cpu_tier or config.get("cpu_tier")
        timeout_s = float(release_timeout_s if release_timeout_s is not None else config.get("release_timeout_s", 30.0))
        release_ok = False
        if cpu_tier is not None:
            release_ok = bool(cpu_tier.request_release(reason="training_start", timeout_s=timeout_s))
            if not release_ok:
                raise RuntimeError("CPU tier did not drain within timeout - DAPO training aborted")
        try:
            return self._run_pipeline_body(base_model, dataset_path, config)
        finally:
            if release_ok:
                cpu_tier.release_finished()

    def _run_pipeline_body(
        self,
        base_model: str,
        dataset_path: Path,
        config: dict[str, Any],
    ) -> DapoTrainingResult:
        """Run DAPO stages after any CPU-tier memory handoff has completed."""
        current_model = base_model
        stage_results: list[StageResult] = []

        for stage in self.STAGES:
            # Pre-stage validation
            if not self._validate_stage_readiness(stage, current_model, dataset_path):
                logger.warning("Stage %s prerequisites not met — skipping", stage)
                stage_results.append(
                    StageResult(
                        success=stage != "dapo",
                        stage_name=stage,
                        output_model=current_model,
                        metrics={StatusEnum.SKIPPED.value: True, "reason": "prerequisites_not_met"},
                    )
                )
                if stage == "dapo":
                    return DapoTrainingResult(
                        success=False,
                        stage_failed=stage,
                        error="dapo_skipped:prerequisites_not_met",
                        stage_results=stage_results,
                    )
                continue

            # Run stage
            result = self._run_stage(stage, current_model, dataset_path, config)
            stage_results.append(result)

            if stage == "dapo" and result.metrics.get(StatusEnum.SKIPPED.value):
                reason = str(result.metrics.get("reason") or "dapo_skipped")
                logger.error("DAPO stage was skipped: %s", reason)
                return DapoTrainingResult(
                    success=False,
                    stage_failed=stage,
                    error=f"dapo_skipped:{reason}",
                    stage_results=stage_results,
                )
            if not result.success:
                logger.error("Stage %s failed: %s", stage, result.error)
                return DapoTrainingResult(
                    success=False,
                    stage_failed=stage,
                    error=result.error,
                    stage_results=stage_results,
                )

            # Post-stage validation
            if result.output_model and not self._validate_stage_output(
                stage,
                current_model,
                result.output_model,
                loss_limits=self._stage_loss_limits(stage, config),
            ):
                logger.warning("Stage %s output failed validation — reverting", stage)
                return DapoTrainingResult(
                    success=False,
                    stage_failed=stage,
                    error="regression_detected",
                    stage_results=stage_results,
                )

            if result.output_model:
                current_model = result.output_model

        return DapoTrainingResult(
            success=True,
            final_model=current_model,
            stage_results=stage_results,
        )

    @staticmethod
    def _validate_stage_readiness(
        stage: str,
        model: str,
        dataset_path: Path,
    ) -> bool:
        """Check if a stage's prerequisites are met.

        Args:
            stage: Stage name (sft, simpo, dapo).
            model: Current model path.
            dataset_path: Training data path.

        Returns:
            True if ready to proceed.
        """
        if stage == "sft":
            if not bool(model):
                return False
            # Defect 207: require dataset_path to exist, not just be truthy
            if isinstance(dataset_path, Path):
                return dataset_path.exists()
            elif dataset_path:
                return Path(dataset_path).exists()
            return False

        if stage == "simpo":
            # SimPO needs SFT output and a dataset to curate preference pairs from
            # (defect 207: also check dataset_path exists).
            if not bool(model):
                return False
            # Defect 207: require dataset_path to exist, not just be truthy
            if isinstance(dataset_path, Path):
                return dataset_path.exists()
            elif dataset_path:
                return Path(dataset_path).exists()
            return False

        if stage == "dapo":
            # DAPO needs group data (min 5 groups of K=4); in cold start, skip DAPO
            # (defect 207: also check dataset_path exists).
            if not bool(model):
                return False
            # Defect 207: require dataset_path to exist, not just be truthy
            if isinstance(dataset_path, Path):
                return dataset_path.exists()
            elif dataset_path:
                return Path(dataset_path).exists()
            return False

        return True

    @staticmethod
    def _run_stage(
        stage: str,
        model: str,
        dataset_path: Path,
        config: dict[str, Any],
    ) -> StageResult:
        """Dispatch a single training stage to its execution function.

        Args:
            stage: Stage name.
            model: Input model path.
            dataset_path: Training data path.
            config: Configuration overrides.

        Returns:
            StageResult with output model path.
        """
        logger.info("Running training stage: %s (model=%s)", stage, model)

        try:
            if stage == "sft":
                return run_sft_stage(model, dataset_path, config)
            if stage == "simpo":
                return run_simpo_stage(model, dataset_path, config)
            if stage == "dapo":
                return run_dapo_stage(model, dataset_path, config)
            return StageResult(
                success=False,
                stage_name=stage,
                error=f"Unknown stage: {stage}",
            )
        except Exception as exc:
            logger.exception("Stage %s raised exception", stage)
            return StageResult(
                success=False,
                stage_name=stage,
                error=str(exc),
            )

    @staticmethod
    def _stage_loss_limits(stage: str, config: dict[str, Any]) -> dict[str, float]:
        """Return configured absolute and relative loss limits for stage validation."""
        validation_config = config.get("validation", {})
        if not isinstance(validation_config, dict):
            validation_config = {}
        stage_config = validation_config.get(stage, {})
        if not isinstance(stage_config, dict):
            stage_config = {}

        def value(name: str, default: float) -> float:
            raw = stage_config.get(name, validation_config.get(name, default))
            return float(raw)

        return {
            "max_eval_loss": value("max_eval_loss", DEFAULT_MAX_EVAL_LOSS),
            "max_train_loss": value("max_train_loss", DEFAULT_MAX_TRAIN_LOSS),
            "loss_regression_multiplier": value(
                "loss_regression_multiplier",
                DEFAULT_LOSS_REGRESSION_MULTIPLIER,
            ),
            "loss_regression_floor_delta": value(
                "loss_regression_floor_delta",
                DEFAULT_LOSS_REGRESSION_FLOOR_DELTA,
            ),
        }

    @staticmethod
    def _calibrated_loss_limit(
        log_history: list[Any],
        metric_name: str,
        configured_limit: float,
        *,
        regression_multiplier: float,
        floor_delta: float,
    ) -> float:
        """Tighten an absolute loss limit using previous logged loss values."""
        previous_values = [
            float(entry[metric_name])
            for entry in log_history[:-1]
            if isinstance(entry, dict) and isinstance(entry.get(metric_name), int | float)
        ]
        if not previous_values:
            return configured_limit
        baseline = previous_values[-1]
        calibrated = max(baseline * regression_multiplier, baseline + floor_delta)
        return min(configured_limit, calibrated)

    @staticmethod
    def _validate_stage_output(
        stage: str,
        input_model: str,
        output_model: str,
        loss_limits: dict[str, float] | None = None,
    ) -> bool:
        """Validate that stage output did not regress model quality.

        Checks that the output model path exists and, if a trainer state
        file was written, that the final eval loss is not catastrophic.

        Args:
            stage: Stage name (sft, simpo, dapo).
            input_model: Model path before stage.
            output_model: Model path after stage.
            loss_limits: Optional per-stage eval-loss regression limits.

        Returns:
            True if the output passes validation checks.
        """
        loss_limits = loss_limits or TrainingStageOrchestrator._stage_loss_limits(stage, {})

        if output_model == input_model:
            # Stage was skipped or produced no change — trivially valid
            return True

        output_path = Path(output_model)
        if not output_path.exists():
            logger.warning(
                "Stage %s output path does not exist: %s",
                stage,
                output_model,
            )
            return False

        # Check for trainer_state.json with eval_loss
        state_file = output_path / "trainer_state.json"
        if not state_file.exists():
            # Also check parent dir (adapter may be in a subdirectory)
            state_file = output_path.parent / "trainer_state.json"

        if state_file.exists():
            try:
                state = _json.loads(state_file.read_text(encoding="utf-8"))
                log_history = state.get("log_history", [])
                if log_history:
                    last_entry = log_history[-1]
                    eval_loss = last_entry.get("eval_loss")
                    max_eval_loss = TrainingStageOrchestrator._calibrated_loss_limit(
                        log_history,
                        "eval_loss",
                        loss_limits["max_eval_loss"],
                        regression_multiplier=loss_limits["loss_regression_multiplier"],
                        floor_delta=loss_limits["loss_regression_floor_delta"],
                    )
                    if eval_loss is not None and eval_loss > max_eval_loss:
                        logger.warning(
                            "Stage %s: eval_loss %.2f exceeds catastrophic threshold %.2f",
                            stage,
                            eval_loss,
                            max_eval_loss,
                        )
                        return False
                    train_loss = last_entry.get("loss")
                    max_train_loss = TrainingStageOrchestrator._calibrated_loss_limit(
                        log_history,
                        "loss",
                        loss_limits["max_train_loss"],
                        regression_multiplier=loss_limits["loss_regression_multiplier"],
                        floor_delta=loss_limits["loss_regression_floor_delta"],
                    )
                    if train_loss is not None and train_loss > max_train_loss:
                        logger.warning(
                            "Stage %s: train_loss %.2f exceeds catastrophic threshold %.2f",
                            stage,
                            train_loss,
                            max_train_loss,
                        )
                        return False
            except _json.JSONDecodeError as exc:
                # Corrupt trainer_state.json means the training run is suspect —
                # fail validation rather than silently accepting bad state (defect 16 fix).
                logger.warning(
                    "Stage %s: trainer_state.json is corrupt (%s) — stage validation failed",
                    stage,
                    exc,
                )
                return False
            except (OSError, KeyError):
                logger.warning("Stage %s: could not read trainer state; assuming valid", stage)

        logger.info("Stage %s: validation passed for %s", stage, output_model)
        return True

    def _run_simpo(
        self,
        model: str,
        dataset_path: Path,
        config: dict[str, Any],
    ) -> StageResult:
        """Run the SimPO alignment stage directly.

        Convenience wrapper around ``_run_stage("simpo", ...)`` for
        programmatic and test access.

        Args:
            model: Input model path or identifier.
            dataset_path: Path to the DPO preference dataset.
            config: Stage configuration overrides.

        Returns:
            StageResult from the SimPO training stage.
        """
        return self._run_stage("simpo", model, dataset_path, config)

    def _run_dapo(
        self,
        model: str,
        dataset_path: Path,
        config: dict[str, Any],
    ) -> StageResult:
        """Run the DAPO RL stage directly.

        Convenience wrapper around ``_run_stage("dapo", ...)`` for
        programmatic and test access.

        Args:
            model: Input model path or identifier.
            dataset_path: Path to the ranking dataset.
            config: Stage configuration overrides.

        Returns:
            StageResult from the DAPO training stage.
        """
        return self._run_stage("dapo", model, dataset_path, config)
