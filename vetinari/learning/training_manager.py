"""Vetinari Training Manager.

=========================

Unified interface for managing fine-tuning workflows:

- Prepares training datasets from collected execution records
- Provides recommended hyperparameter configurations
- Submits local (Unsloth/LoRA) training jobs and records cloud-training
  requests as failed jobs until a concrete provider integration is wired
- Tracks job status and recommends retraining when quality degrades

All heavy optional dependencies (unsloth, huggingface_hub) are handled
gracefully — if they are not installed, methods return structured error
results with installation instructions rather than raising ImportError.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .training_manager_cloud import TrainingManagerCloudMixin
from .training_manager_jobs import TrainingManagerJobRegistryMixin, _resolve_training_jobs_path
from .training_manager_local import _train_local_impl
from .training_manager_retraining import TrainingManagerRetrainingMixin

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class TrainingDataset:
    """Prepared dataset ready for a training run."""

    records: list[dict[str, Any]]
    format: str  # "sft", "dpo", "hf", "ranking"
    stats: dict[str, Any]  # count, avg_score, task_type_breakdown

    def __repr__(self) -> str:
        return f"TrainingDataset(format={self.format!r}, records={len(self.records)})"


@dataclass
class TrainingResult:
    """Result of a completed (or failed) training run."""

    success: bool
    model_path: str | None
    metrics: dict[str, Any]  # loss, eval_loss, etc.
    duration_seconds: float
    error: str | None = None

    def __repr__(self) -> str:
        return (
            f"TrainingResult(success={self.success!r}, model_path={self.model_path!r}, "
            f"duration_seconds={self.duration_seconds!r})"
        )


@dataclass
class TrainingJob:
    """A tracked training job (local or cloud)."""

    job_id: str
    status: str  # StatusEnum: pending, running, completed, failed
    provider: str
    model_id: str
    created_at: str
    progress: float = 0.0
    result: TrainingResult | None = None

    def __repr__(self) -> str:
        return (
            f"TrainingJob(job_id={self.job_id!r}, status={self.status!r}, "
            f"provider={self.provider!r}, model_id={self.model_id!r}, "
            f"progress={self.progress!r})"
        )


@dataclass(frozen=True, slots=True)
class RetrainingRecommendation:
    """Whether a model/task combination warrants retraining."""

    model_id: str
    task_type: str
    current_avg_quality: float
    baseline_quality: float
    degradation: float  # fractional — 0.15 means 15 %
    recommended: bool
    reason: str
    recommended_method: str = "qlora"
    recommended_min_score: float = 0.85

    def __repr__(self) -> str:
        return (
            f"RetrainingRecommendation(model_id={self.model_id!r}, "
            f"task_type={self.task_type!r}, recommended={self.recommended!r}, "
            f"degradation={self.degradation!r})"
        )


# ---------------------------------------------------------------------------
# TrainingManager
# ---------------------------------------------------------------------------

# Degradation threshold that triggers a retraining recommendation
_RETRAIN_DEGRADATION_THRESHOLD = 0.15
# Minimum baseline quality to compare against
_BASELINE_QUALITY = 0.80
# Minimum records required for a valid local LoRA run
_MIN_QLORA_RECORDS = 100


class TrainingManager(TrainingManagerJobRegistryMixin, TrainingManagerRetrainingMixin, TrainingManagerCloudMixin):
    """Unified training workflow coordinator.

    Uses :func:`get_training_collector` internally to access accumulated
    execution records without requiring a path argument on every call.
    """

    def __init__(self, data_path: str | None = None, jobs_path: str | Path | None = None) -> None:
        """Initialize the manager and reload durable training job state.

        Args:
            data_path: Optional training data JSONL path used for collector
                isolation and default job-registry placement.
            jobs_path: Optional explicit JSON registry path for tests or
                operator-managed storage.
        """
        self._data_path = data_path
        self._jobs_path = _resolve_training_jobs_path(data_path, jobs_path)
        self._jobs_lock = threading.RLock()
        self._jobs: dict[str, TrainingJob] = self._load_jobs()

    # ------------------------------------------------------------------
    # Internal helper
    # ------------------------------------------------------------------

    def _get_collector(self):
        from vetinari.learning.training_data import TrainingDataCollector, get_training_collector

        if self._data_path:
            # Use a dedicated instance so different data_paths don't share state
            if not hasattr(self, "_collector_instance"):
                self._collector_instance = TrainingDataCollector(output_path=self._data_path)
            return self._collector_instance
        return get_training_collector()

    # ------------------------------------------------------------------
    # Dataset preparation
    # ------------------------------------------------------------------

    def prepare_training_data(
        self,
        min_score: float = 0.8,
        format: str = "sft",
        task_type: str | None = None,
    ) -> TrainingDataset:
        """Prepare a training dataset using the requested format.

        Parameters
        ----------
        min_score:
            Minimum quality score for records to include.
        format:
            One of ``"sft"``, ``"dpo"``, ``"hf"``, or ``"ranking"``.
        task_type:
            Optional filter by task type (e.g. ``"coding"``).

        Returns:
        -------
        TrainingDataset
            Populated with records, format tag, and summary statistics.

        Args:
            min_score: The min score.
            format: The format.
            task_type: The task type.
        """
        collector = self._get_collector()

        if format == "hf":
            records = collector.export_hf_dataset(min_score=min_score, task_type=task_type)
        elif format == "dpo":
            records = collector.export_dpo_dataset(task_type=task_type)
        elif format == "ranking":
            records = collector.export_ranking_dataset()
        else:
            # default: sft
            records = collector.export_sft_dataset(min_score=min_score, task_type=task_type)

        # Build stats
        count = len(records)
        avg_score = 0.0
        task_type_breakdown: dict[str, int] = {}

        if format == "sft":
            scores = [r.get("score", 0.0) for r in records if isinstance(r.get("score"), (int, float))]
            avg_score = round(sum(scores) / len(scores), 3) if scores else 0.0
            for r in records:
                tt = r.get("task_type", "unknown")
                task_type_breakdown[tt] = task_type_breakdown.get(tt, 0) + 1
        elif format == "hf":
            # HF format has no score field; use collector stats for approximation
            try:
                raw_stats = collector.get_stats()
                avg_score = raw_stats.get("avg_score", 0.0)
            except Exception:
                logger.warning("Could not retrieve HF-format collector stats; avg_score remains 0.0", exc_info=True)

        stats: dict[str, Any] = {
            "count": count,
            "avg_score": avg_score,
            "task_type_breakdown": task_type_breakdown,
        }
        return TrainingDataset(records=records, format=format, stats=stats)

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    def get_training_config(self, method: str = "qlora") -> dict[str, Any]:
        """Return recommended hyperparameters for a training method.

        Parameters
        ----------
        method:
            ``"qlora"`` for QLoRA/Unsloth or ``"full"`` for full fine-tuning.

        Returns:
        -------
        dict
            Hyperparameter dictionary ready to pass to a training framework.
        """
        if method == "full":
            return {
                "method": "full",
                "lr": 1e-5,
                "warmup_ratio": 0.1,
                "weight_decay": 0.01,
                "num_train_epochs": 3,
                "per_device_train_batch_size": 4,
                "gradient_accumulation_steps": 4,
            }
        # qlora with DoRA (default) — DoRA outperforms standard LoRA at small ranks
        return {
            "method": "qlora",
            "lr": 2e-4,
            "lora_rank": 16,
            "lora_alpha": 32,
            "lora_dropout": 0,
            "target_modules": "all_linear",
            "use_dora": True,
            "num_train_epochs": 3,
            "per_device_train_batch_size": 2,
            "gradient_accumulation_steps": 4,
            "load_in_4bit": True,
        }

    # ------------------------------------------------------------------
    # Local training
    # ------------------------------------------------------------------

    def train_local(
        self,
        model_id: str,
        dataset: TrainingDataset,
        method: str = "qlora",
        config: dict[str, Any] | None = None,
    ) -> TrainingResult:
        """Attempt a local fine-tuning run using Unsloth/LoRA.

        If ``unsloth`` is not installed, returns a failed result with
        installation instructions — it does NOT raise an ImportError.

        Parameters
        ----------
        model_id:
            HuggingFace model identifier or local path.
        dataset:
            Prepared dataset from :meth:`prepare_training_data`.
        method:
            ``"qlora"`` (default) or ``"full"``.
        config:
            Override hyperparameters (merged with :meth:`get_training_config`).

        Args:
            model_id: The model id.
            dataset: The dataset.
            method: The method.
            config: The config.

        Returns:
            The TrainingResult result.
        """
        return _train_local_impl(self, model_id, dataset, method, config)

    # ------------------------------------------------------------------
    # Cloud training
    # ------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------


_manager: TrainingManager | None = None
_manager_lock = threading.Lock()


def get_training_manager(data_path: str | None = None) -> TrainingManager:
    """Return the global TrainingManager singleton.

    When an explicit ``data_path`` is provided and the singleton has not yet
    been created, the new instance uses that path.  Once the singleton exists,
    passing a different explicit ``data_path`` raises ``ValueError`` — callers
    that genuinely need a different path must instantiate ``TrainingManager``
    directly.  Passing ``data_path=None`` always returns the existing singleton
    without raising.

    Args:
        data_path: Optional path to the training data directory.  Only the
            first non-``None`` call's value takes effect; a later call with a
            *different* non-``None`` value raises ``ValueError``.

    Returns:
        The shared TrainingManager instance for this process.

    Raises:
        ValueError: If the singleton was already created with a different
            ``data_path`` and the caller passes a new non-``None`` value.
    """
    global _manager
    if _manager is None:
        with _manager_lock:
            if _manager is None:
                _manager = TrainingManager(data_path=data_path)
    elif data_path is not None and _manager._data_path != data_path:
        raise ValueError(
            f"get_training_manager: singleton already initialized with data_path={_manager._data_path!r}; "
            f"refusing to re-init with data_path={data_path!r}. "
            "Instantiate TrainingManager directly if a different path is required."
        )
    return _manager
