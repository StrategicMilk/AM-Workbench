"""Training/eval split support for the fine-tuning pipeline.

This module handles the evaluation holdout step inside training: it reads the
candidate SFT JSONL data, writes deterministic train/eval JSONL files, and
records evidence that the holdout was either configured or unavailable.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from vetinari.learning import atomic_writers

from .eval_holdout_split import HoldoutSplitError, build_holdout_split
from .pipeline_core import TrainingRun, _set_training_run_field

logger = logging.getLogger(__name__)

_TRAINING_EVAL_HOLDOUT_FRACTION = 0.2  # Hold out 20 percent for SFT eval without starving training.


@dataclass(frozen=True, slots=True)
class _TrainingEvalSplit:
    """Prepared training/eval split details for one pipeline run."""

    training_dataset_path: str
    eval_dataset_path: str | None
    training_examples: int
    holdout_examples: int
    evidence_path: str
    status: str
    reason: str

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}("
            f"training_dataset_path={self.training_dataset_path!r}, "
            f"eval_dataset_path={self.eval_dataset_path!r}, "
            f"training_examples={self.training_examples!r}, "
            f"holdout_examples={self.holdout_examples!r}"
            ")"
        )


def _load_jsonl_objects(path: Path) -> list[dict[str, Any]]:
    """Load JSONL object rows for deterministic training/eval splitting."""
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            stripped = raw_line.strip()
            if not stripped:
                continue
            value = json.loads(stripped)
            if not isinstance(value, dict):
                raise ValueError(f"{path}:{line_number} must contain a JSON object")
            rows.append(value)
    return rows


def _record_identity(row: dict[str, Any], index: int) -> str:
    """Return a stable per-row id for holdout assignment."""
    for key in ("id", "record_id", "task_id", "trace_id"):
        value = row.get(key)
        if value:
            return str(value)
    return f"line:{index}"


def _write_training_eval_evidence(run_dir: Path, evidence: dict[str, Any]) -> str:
    """Persist eval-during-training evidence for a run."""
    evidence_path = run_dir / "training_eval_evidence.json"
    atomic_writers.write_json_atomic(evidence_path, evidence)
    return str(evidence_path)


def _prepare_training_eval_split(dataset_path: str, run_dir: Path, run_id: str) -> _TrainingEvalSplit:
    """Create deterministic train/eval JSONL files and evidence metadata."""
    source_path = Path(dataset_path)
    rows = _load_jsonl_objects(source_path)
    record_ids = [_record_identity(row, index) for index, row in enumerate(rows)]
    split = build_holdout_split(
        record_ids,
        holdout_fraction=_TRAINING_EVAL_HOLDOUT_FRACTION,
        seed=run_id,
    )
    train_ids = set(split.train_ids)
    holdout_ids = set(split.holdout_ids)
    training_rows = [row for index, row in enumerate(rows) if record_ids[index] in train_ids]
    holdout_rows = [row for index, row in enumerate(rows) if record_ids[index] in holdout_ids]
    if not training_rows or not holdout_rows:
        raise HoldoutSplitError("training and eval splits must both contain records")

    training_path = run_dir / "sft_train.jsonl"
    eval_path = run_dir / "sft_eval.jsonl"
    atomic_writers.write_jsonl_atomic(training_path, training_rows)
    atomic_writers.write_jsonl_atomic(eval_path, holdout_rows)
    evidence = {
        "schema_version": 1,
        "run_id": run_id,
        "status": "configured",
        "source_dataset_path": str(source_path),
        "training_dataset_path": str(training_path),
        "eval_dataset_path": str(eval_path),
        "training_examples": len(training_rows),
        "holdout_examples": len(holdout_rows),
        "holdout_fraction": _TRAINING_EVAL_HOLDOUT_FRACTION,
        "split_seed": split.seed,
        "sft_config": {
            "eval_strategy": "epoch",
            "load_best_model_at_end": True,
            "metric_for_best_model": "eval_loss",
            "greater_is_better": False,
        },
    }
    evidence_path = _write_training_eval_evidence(run_dir, evidence)
    return _TrainingEvalSplit(
        training_dataset_path=str(training_path),
        eval_dataset_path=str(eval_path),
        training_examples=len(training_rows),
        holdout_examples=len(holdout_rows),
        evidence_path=evidence_path,
        status="configured",
        reason="deterministic holdout configured for SFT eval-during-training",
    )


def _mark_training_eval_unavailable(
    run: TrainingRun,
    *,
    run_dir: Path,
    run_id: str,
    dataset_path: str | Path,
    reason: str,
) -> None:
    """Mark eval holdout as unavailable and persist evidence when possible."""
    _set_training_run_field(run, "eval_status", "unavailable")
    _set_training_run_field(run, "eval_reason", reason)
    try:
        _set_training_run_field(
            run,
            "eval_evidence_path",
            _write_training_eval_evidence(
                run_dir,
                {
                    "schema_version": 1,
                    "run_id": run_id,
                    "status": run.eval_status,
                    "reason": run.eval_reason,
                    "source_dataset_path": str(dataset_path),
                    "training_examples": run.training_examples,
                    "holdout_examples": 0,
                },
            ),
        )
    except OSError:
        logger.warning(
            "[TrainingPipeline] %s: Could not persist unavailable eval evidence",
            run_id,
            exc_info=True,
        )


def _configure_training_eval_holdout(
    run: TrainingRun,
    *,
    dataset_path: str,
    run_dir: Path,
    run_id: str,
) -> tuple[str, str | None]:
    """Prepare an eval holdout and update the run with split metadata.

    Returns:
        Tuple of the training dataset path and optional eval dataset path.
        When the holdout cannot be built, the original dataset path is
        returned with ``None`` for the eval path.
    """
    try:
        eval_split = _prepare_training_eval_split(dataset_path, run_dir, run_id)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        logger.warning("Exception handled by  configure training eval holdout fallback", exc_info=True)
        _mark_training_eval_unavailable(
            run,
            run_dir=run_dir,
            run_id=run_id,
            dataset_path=dataset_path,
            reason=f"eval holdout unavailable: {exc}",
        )
        return dataset_path, None

    _set_training_run_field(run, "training_examples", eval_split.training_examples)
    _set_training_run_field(run, "eval_holdout_examples", eval_split.holdout_examples)
    _set_training_run_field(run, "eval_status", eval_split.status)
    _set_training_run_field(run, "eval_reason", eval_split.reason)
    _set_training_run_field(run, "eval_evidence_path", eval_split.evidence_path)
    logger.info(
        "[TrainingPipeline] %s: SFT eval holdout configured train=%d eval=%d",
        run_id,
        run.training_examples,
        run.eval_holdout_examples,
    )
    return eval_split.training_dataset_path, eval_split.eval_dataset_path
