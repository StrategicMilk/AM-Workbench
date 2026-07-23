"""Training safety validation — pre/post training checks.

Provides two validator classes:
- PreTrainingValidator: runs safety checks before starting a training run,
  saves a checkpoint of the current model metadata, validates training data
  quality, disk space, and optionally GPU temperature.
- PostTrainingValidator: compares benchmark scores after training completes
  and recommends whether to deploy, rollback, or retry.
"""

from __future__ import annotations

import json
import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from vetinari.constants import get_user_dir

logger = logging.getLogger(__name__)


# Minimum free disk space required before training begins (20 GB in bytes)
MIN_FREE_DISK_BYTES = 20 * 1024**3

# Maximum safe GPU temperature in degrees Celsius
MAX_GPU_TEMP_CELSIUS = 85


class PreTrainingValidator:
    """Runs safety checks before a training run begins.

    Validates that a checkpoint exists, training data is present, disk space
    is sufficient, and the GPU is within safe operating temperature.
    """

    def __init__(self, checkpoint_dir: Path | None = None) -> None:
        """Initialise the pre-training validator.

        Args:
            checkpoint_dir: Directory for storing checkpoint metadata JSON files.
                Defaults to ``~/.vetinari/checkpoints/``.
        """
        self.checkpoint_dir: Path = checkpoint_dir or (get_user_dir() / "checkpoints")
        self.checkpoint_dir.mkdir(parents=True, exist_ok=True)

    def run_checks(self) -> tuple[bool, list[str]]:
        """Run all pre-training safety checks.

        Checks performed in order:
        1. Save a checkpoint of the current model metadata.
        2. Validate that JSONL training data files exist and are non-empty.
        3. Verify at least 20 GB of free disk space is available.
        4. If pynvml is available, verify GPU temperature is at or below 85 °C.

        Returns:
            A tuple of ``(passed, issues)`` where ``passed`` is True only when
            every check succeeds, and ``issues`` is a list of human-readable
            problem descriptions (empty when all checks pass).
        """
        issues: list[str] = []

        # Check 1: save checkpoint
        checkpoint_path = self.save_checkpoint()
        if checkpoint_path is None:
            issues.append("Failed to save pre-training checkpoint — aborting for safety")
        else:
            logger.info("Pre-training checkpoint saved to %s", checkpoint_path)

        # Check 2: training data quality
        data_ok, data_issue = self._check_training_data()
        if not data_ok:
            issues.append(data_issue)

        # Check 3: disk space
        disk_ok, disk_issue = self._check_disk_space()
        if not disk_ok:
            issues.append(disk_issue)

        # Check 4: GPU temperature (optional — skipped if pynvml not installed)
        gpu_ok, gpu_issue = self._check_gpu_temperature()
        if not gpu_ok:
            issues.append(gpu_issue)

        passed = len(issues) == 0
        if passed:
            logger.info("All pre-training checks passed")
        else:
            logger.warning("Pre-training checks failed: %d issue(s)", len(issues))
            for issue in issues:
                logger.warning("  - %s", issue)

        return passed, issues

    def save_checkpoint(self, model_path: str | None = None) -> Path | None:
        """Save checkpoint metadata for the current model to disk.

        Writes a JSON file containing the model path, timestamp, and a basic
        config snapshot. Does not copy or modify the actual model files — those
        remain under the local models directory.

        Args:
            model_path: Optional path to the model being checkpointed. When
                omitted the checkpoint records the absence of a specific path.

        Returns:
            Path to the written checkpoint JSON file, or None if writing failed.
        """
        timestamp = datetime.now(timezone.utc).isoformat()
        checkpoint_name = f"checkpoint_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.json"
        checkpoint_path = self.checkpoint_dir / checkpoint_name

        metadata: dict[str, Any] = {
            "timestamp": timestamp,
            "model_path": model_path,
            "checkpoint_type": "pre_training",
            "config": {
                "checkpoint_dir": str(self.checkpoint_dir),
                "min_free_disk_bytes": MIN_FREE_DISK_BYTES,
                "max_gpu_temp_celsius": MAX_GPU_TEMP_CELSIUS,
            },
        }

        try:
            with checkpoint_path.open("w", encoding="utf-8") as fh:
                json.dump(metadata, fh, indent=2)
            logger.info("Checkpoint metadata written to %s", checkpoint_path)
            return checkpoint_path
        except OSError as exc:
            logger.error("Failed to write checkpoint to %s: %s", checkpoint_path, exc)
            return None

    def get_latest_checkpoint(self) -> dict[str, Any] | None:
        """Return the metadata from the most recently saved checkpoint.

        Scans the checkpoint directory for JSON files, orders them by name
        (which encodes the timestamp), and reads the most recent one.

        Returns:
            Parsed checkpoint metadata dictionary, or None if no checkpoints
            exist or all files are unreadable.
        """
        checkpoint_files = sorted(self.checkpoint_dir.glob("checkpoint_*.json"), reverse=True)
        if not checkpoint_files:
            logger.debug("No checkpoints found in %s", self.checkpoint_dir)
            return None

        latest = checkpoint_files[0]
        try:
            with latest.open(encoding="utf-8") as fh:
                data: dict[str, Any] = json.load(fh)
            logger.debug("Latest checkpoint: %s", latest)
            return data
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Could not read checkpoint %s: %s", latest, exc)
            return None

    # ── Private helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _check_training_data() -> tuple[bool, str]:
        """Verify training data JSONL files exist and contain at least one record.

        Returns:
            Tuple of ``(ok, issue_message)``. ``issue_message`` is empty when ok.
        """
        data_dir = get_user_dir() / "training_data"
        if not data_dir.exists():
            return False, f"Training data directory not found: {data_dir}"

        jsonl_files = list(data_dir.glob("*.jsonl"))
        if not jsonl_files:
            return False, f"No JSONL training data files found in {data_dir}"

        # Check that at least one file has actual content
        non_empty = [f for f in jsonl_files if f.stat().st_size > 0]
        if not non_empty:
            return False, f"All {len(jsonl_files)} JSONL file(s) in {data_dir} are empty"

        logger.debug(
            "Training data check passed: %d non-empty JSONL file(s) found",
            len(non_empty),
        )
        return True, ""

    @staticmethod
    def _check_disk_space() -> tuple[bool, str]:
        """Check that at least 20 GB of free disk space is available.

        Returns:
            Tuple of ``(ok, issue_message)``.
        """
        try:
            usage = shutil.disk_usage(str(get_user_dir()))
        except OSError as exc:
            logger.warning("Caught OSError in except block: %s", exc)
            return False, f"Could not determine disk usage: {exc}"

        free_gb = usage.free / (1024**3)
        if usage.free < MIN_FREE_DISK_BYTES:
            return (
                False,
                f"Insufficient disk space: {free_gb:.1f} GB free, need at least 20 GB",
            )

        logger.debug("Disk space check passed: %.1f GB free", free_gb)
        return True, ""

    @staticmethod
    def _check_gpu_temperature() -> tuple[bool, str]:
        """Check GPU temperature via pynvml, if available.

        Returns:
            Tuple of ``(ok, issue_message)``. Returns ``(True, "")`` when
            pynvml is not installed (check is skipped gracefully).
        """
        try:
            import pynvml
        except ImportError:
            logger.debug("pynvml not available; skipping GPU temperature check")
            return True, ""

        try:
            pynvml.nvmlInit()
            device_count = pynvml.nvmlDeviceGetCount()
            hot_gpus: list[str] = []

            for idx in range(device_count):
                handle = pynvml.nvmlDeviceGetHandleByIndex(idx)
                temp = pynvml.nvmlDeviceGetTemperature(handle, pynvml.NVML_TEMPERATURE_GPU)
                logger.debug("GPU %d temperature: %d°C", idx, temp)
                if temp > MAX_GPU_TEMP_CELSIUS:
                    hot_gpus.append(f"GPU {idx}: {temp}°C")

            pynvml.nvmlShutdown()

            if hot_gpus:
                return (
                    False,
                    f"GPU temperature exceeds {MAX_GPU_TEMP_CELSIUS}°C — "
                    f"cool down before training: {', '.join(hot_gpus)}",
                )

            logger.debug("GPU temperature check passed (%d GPU(s) checked)", device_count)
            return True, ""

        except Exception as exc:
            # pynvml available but query failed — warn but don't block training
            logger.warning("GPU temperature check encountered an error: %s", exc)
            return True, ""


class PostTrainingValidator:
    """Validates training results by comparing benchmark scores before and after.

    Fails when any benchmark metric regresses beyond the allowed threshold, or
    when the target metric did not improve.
    """

    def __init__(self, regression_threshold: float = 0.05) -> None:
        """Initialise the post-training validator.

        Args:
            regression_threshold: Maximum tolerated drop on any single benchmark
                score before validation fails. Default is 0.05 (5 % regression).
        """
        self.regression_threshold = regression_threshold

    def validate(
        self,
        old_scores: dict[str, float],
        new_scores: dict[str, float],
        target_metric: str = "quality",
    ) -> tuple[bool, str]:
        """Compare benchmark scores from before and after training.

        Fails if the new model is worse than ``regression_threshold`` on ANY
        shared benchmark, or if the ``target_metric`` did not improve.

        Args:
            old_scores: Mapping of benchmark name to score for the baseline model.
            new_scores: Mapping of benchmark name to score for the new model.
            target_metric: The primary metric that must show improvement.
                Defaults to ``"quality"``.

        Returns:
            Tuple of ``(passed, reason)`` where ``reason`` describes the outcome
            in human-readable form.
        """
        regressions: list[str] = []
        missing_metrics: list[str] = []

        # Check every benchmark that exists in both score sets
        for metric, old_val in old_scores.items():
            new_val = new_scores.get(metric)
            if new_val is None:
                missing_metrics.append(metric)
                continue
            drop = old_val - new_val
            if drop > self.regression_threshold:
                regressions.append(
                    f"{metric}: {old_val:.4f} → {new_val:.4f} (dropped {drop:.4f})",
                )

        if missing_metrics:
            reason = "Candidate metrics missing after training: " + ", ".join(sorted(missing_metrics))
            logger.warning("Post-training validation failed â€” %s", reason)
            return False, reason

        if regressions:
            reason = "Regression detected on: " + "; ".join(regressions)
            logger.warning("Post-training validation failed — %s", reason)
            return False, reason

        # Check that the target metric improved
        old_target = old_scores.get(target_metric)
        new_target = new_scores.get(target_metric)

        if old_target is None or new_target is None:
            missing_side = []
            if old_target is None:
                missing_side.append("baseline")
            if new_target is None:
                missing_side.append("candidate")
            reason = f"Target metric '{target_metric}' missing from {' and '.join(missing_side)} scores"
            logger.warning("Post-training validation failed - %s", reason)
            return False, reason

        if old_target is not None and new_target is not None and new_target <= old_target:
            reason = f"Target metric '{target_metric}' did not improve: {old_target:.4f} → {new_target:.4f}"
            logger.warning("Post-training validation failed — %s", reason)
            return False, reason

        improvement = (
            f"{target_metric}: {old_target:.4f} → {new_target:.4f}"
            if old_target is not None and new_target is not None
            else "target metric not present in both score sets"
        )
        reason = f"All benchmarks within threshold. {improvement}"
        logger.info("Post-training validation passed — %s", reason)
        return True, reason

    def recommend_action(self, passed: bool, old_model: str, new_model: str) -> str:
        """Recommend the next action based on post-training validation results.

        Args:
            passed: Whether ``validate()`` returned True.
            old_model: Identifier of the baseline model (used in log messages).
            new_model: Identifier of the newly trained model.

        Returns:
            One of ``"deploy"``, ``"rollback"``, or ``"retry"``.
        """
        if passed:
            logger.info(
                "Recommendation: deploy new model '%s' (replaces '%s')",
                new_model,
                old_model,
            )
            return "deploy"

        # Decide between rollback and retry based on how bad the regression is.
        # A conservative heuristic: if threshold is at or above 10%, we've tried
        # a generous window and should rollback; otherwise suggest a retry with
        # tighter training settings.
        if self.regression_threshold >= 0.10:
            logger.warning(
                "Recommendation: rollback to '%s' — new model '%s' failed wide threshold",
                old_model,
                new_model,
            )
            return "rollback"

        logger.info(
            "Recommendation: retry training for '%s' — regression within narrow threshold, "
            "may recover with adjusted hyperparameters",
            new_model,
        )
        return "retry"
