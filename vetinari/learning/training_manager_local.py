"""Local fine-tuning helpers for the TrainingManager public API."""

from __future__ import annotations

import json
import logging
import shutil
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from vetinari.boundary_guards import assert_dependency_success
from vetinari.types import AgentType

if TYPE_CHECKING:
    from vetinari.learning.training_manager import TrainingDataset, TrainingManager, TrainingResult

logger = logging.getLogger(__name__)


def _record_local_job(
    manager: TrainingManager,
    job_id: str,
    model_id: str,
    created_at: str,
    status: str,
    progress: float,
    result: TrainingResult | None = None,
) -> None:
    """Persist one local-training job state through the manager registry."""
    from vetinari.learning.training_manager import TrainingJob

    manager._upsert_job(
        TrainingJob(
            job_id=job_id,
            status=status,
            provider="local",
            model_id=model_id,
            created_at=created_at,
            progress=progress,
            result=result,
        )
    )


def _failed_local_training_result(
    manager: TrainingManager,
    job_id: str,
    model_id: str,
    created_at: str,
    start: float,
    error: str,
) -> TrainingResult:
    """Create and persist a failed local-training result."""
    from vetinari.learning.training_manager import TrainingResult

    result = TrainingResult(
        success=False,
        model_path=None,
        metrics={},
        duration_seconds=round(time.monotonic() - start, 2),
        error=error,
    )
    _record_local_job(manager, job_id, model_id, created_at, "failed", 1.0, result)
    return result


def _validate_local_dataset(
    manager: TrainingManager,
    job_id: str,
    model_id: str,
    created_at: str,
    start: float,
    dataset: TrainingDataset,
    method: str,
    hparams: dict[str, Any] | None = None,
) -> TrainingResult | None:
    """Return a failed result when the dataset is too small to train."""
    from vetinari.learning.training_manager import _MIN_QLORA_RECORDS

    hparams = hparams or {}
    configured_min = hparams.get("min_training_records") or hparams.get("minimum_training_records")
    min_records = (
        int(configured_min) if configured_min is not None else (_MIN_QLORA_RECORDS if method == "qlora" else 50)
    )
    if len(dataset.records) >= min_records:
        return None
    return _failed_local_training_result(
        manager,
        job_id,
        model_id,
        created_at,
        start,
        (
            f"Dataset too small: {len(dataset.records)} records "
            f"(minimum {min_records} required for {method}). "
            "Collect more execution data or lower min_score."
        ),
    )


def _load_ready_training_pipeline(
    manager: TrainingManager,
    job_id: str,
    model_id: str,
    created_at: str,
    start: float,
) -> tuple[Any | None, TrainingResult | None]:
    """Load the training pipeline and verify optional dependencies."""
    try:
        from vetinari.training.pipeline import TrainingPipeline

        pipeline = TrainingPipeline()
        reqs = pipeline.check_requirements()
    except (ImportError, ValueError) as exc:
        duration = round(time.monotonic() - start, 2)
        logger.error(
            "Training pipeline module not importable - trl/peft/transformers may be missing; "
            "training aborted after %.2fs",
            duration,
            exc_info=True,
        )
        assert_dependency_success(False, dependency_id="training_pipeline")
        raise AssertionError("unreachable: assert_dependency_success must raise") from exc

    if reqs.get("ready_for_training", False):
        return pipeline, None
    missing = [lib for lib, avail in reqs.get("libraries", {}).items() if not avail]
    result = _failed_local_training_result(
        manager,
        job_id,
        model_id,
        created_at,
        start,
        (
            "Training libraries not installed. Missing: "
            + ", ".join(missing)
            + ". Install with: pip install trl peft bitsandbytes transformers"
        ),
    )
    return None, result


def _dominant_dataset_task_type(dataset: TrainingDataset) -> str | None:
    """Return the most common task type recorded in dataset stats."""
    breakdown = dataset.stats.get("task_type_breakdown")
    if not isinstance(breakdown, dict) or not breakdown:
        return None
    return str(max(breakdown, key=lambda key: breakdown[key]))


def _record_to_jsonable_dict(record: object) -> dict[str, Any]:
    """Return a JSON-serializable mapping for a dataset record."""
    if hasattr(record, "to_dict"):
        value = record.to_dict()
    elif isinstance(record, dict):
        value = record
    else:
        value = vars(record)
    if not isinstance(value, dict):
        raise TypeError("training dataset record must serialize to a dictionary")
    return value


def _serialize_training_dataset(dataset: TrainingDataset) -> tuple[str | None, str]:
    """Write validated dataset records to a temporary JSONL file."""
    dataset_dir = tempfile.mkdtemp(prefix="vetinari_train_")
    dataset_jsonl_path = Path(dataset_dir) / "dataset.jsonl"
    dataset_path: str | None = str(dataset_jsonl_path)
    try:
        with dataset_jsonl_path.open("w", encoding="utf-8") as handle:
            for record in dataset.records:
                handle.write(json.dumps(_record_to_jsonable_dict(record), ensure_ascii=False) + "\n")
        logger.info(
            "[TrainingManager] Serialized %d validated records to %s",
            len(dataset.records),
            dataset_path,
        )
    except (AttributeError, OSError, TypeError, ValueError) as exc:
        logger.warning(
            "[TrainingManager] Could not serialize dataset to JSONL (%s) - pipeline will re-curate",
            exc,
        )
        dataset_path = None
    return dataset_path, dataset_dir


def _record_agent_training_result(model_id: str, method: str, run: Any) -> None:
    """Best-effort agent-level training history update."""
    try:
        from vetinari.training.agent_trainer import AgentTrainer

        AgentTrainer().record_training(
            agent_type=AgentType.WORKER.value,
            model_path=model_id,
            metrics={
                "method": method,
                "success": run.success,
                "training_examples": run.training_examples,
                "eval_score": run.eval_score,
                "eval_status": run.eval_status,
                "eval_evidence_path": run.eval_evidence_path,
            },
        )
    except Exception:
        logger.warning("[TrainingManager] Could not record training to agent history", exc_info=True)


def _training_result_from_pipeline_run(run: Any, duration: float) -> TrainingResult:
    """Convert a pipeline run object to the public TrainingResult shape."""
    from vetinari.learning.training_manager import TrainingResult

    return TrainingResult(
        success=run.success,
        model_path=run.output_model_path,
        metrics={
            "training_examples": run.training_examples,
            "eval_score": run.eval_score,
            "eval_status": run.eval_status,
            "eval_evidence_path": run.eval_evidence_path,
            "eval_holdout_examples": run.eval_holdout_examples,
        },
        duration_seconds=duration,
        error=None if run.success else "Training pipeline reported failure",
    )


def _run_local_pipeline(
    manager: TrainingManager,
    job_id: str,
    model_id: str,
    created_at: str,
    start: float,
    pipeline: Any,
    task_type: str | None,
    hparams: dict[str, Any],
    dataset_path: str | None,
) -> Any | TrainingResult:
    """Execute the local training pipeline and convert runtime failures."""
    try:
        return pipeline.run(
            base_model=model_id,
            task_type=task_type,
            min_score=float(hparams.get("min_score", hparams.get("quality_min_score", 0.8))),
            epochs=hparams.get("num_train_epochs", 3),
            dataset_path=dataset_path,
        )
    except Exception as exc:
        logger.exception("[TrainingManager] Training pipeline failed for %s", model_id)
        return _failed_local_training_result(manager, job_id, model_id, created_at, start, str(exc))


def _train_local_impl(
    manager: TrainingManager,
    model_id: str,
    dataset: TrainingDataset,
    method: str = "qlora",
    config: dict[str, Any] | None = None,
) -> TrainingResult:
    """Run the local fine-tuning workflow behind TrainingManager.train_local."""
    start = time.monotonic()
    job_id = f"local-{int(start * 1000)}"
    created_at = datetime.now(timezone.utc).isoformat()
    _record_local_job(manager, job_id, model_id, created_at, "running", 0.0)

    hparams = manager.get_training_config(method)
    if config:
        hparams.update(config)

    failure = _validate_local_dataset(manager, job_id, model_id, created_at, start, dataset, method, hparams)
    if failure is not None:
        return failure

    pipeline, failure = _load_ready_training_pipeline(manager, job_id, model_id, created_at, start)
    if failure is not None:
        return failure

    task_type = _dominant_dataset_task_type(dataset)
    dataset_path, dataset_dir = _serialize_training_dataset(dataset)
    logger.info("[TrainingManager] Starting local %s training for %s", method, model_id)
    try:
        run = _run_local_pipeline(
            manager, job_id, model_id, created_at, start, pipeline, task_type, hparams, dataset_path
        )
    finally:
        shutil.rmtree(dataset_dir, ignore_errors=True)

    if isinstance(run, _training_result_type()):
        return run

    duration = round(time.monotonic() - start, 2)
    _record_agent_training_result(model_id, method, run)
    result = _training_result_from_pipeline_run(run, duration)
    _record_local_job(manager, job_id, model_id, created_at, "completed" if result.success else "failed", 1.0, result)
    return result


def _training_result_type() -> type[TrainingResult]:
    """Return the runtime TrainingResult type without importing during module load."""
    from vetinari.learning.training_manager import TrainingResult

    return TrainingResult
