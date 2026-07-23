"""Side-effect helpers for training pipeline runs.

This module owns small persistence and bookkeeping steps that happen around
the central training workflow: counting JSONL data, replay-buffer ingestion,
run-record persistence, receipt emission, and improvement-archive recording.
"""

from __future__ import annotations

import dataclasses
import json
import logging
from pathlib import Path
from typing import Any

from vetinari.guards import require_subsystem
from vetinari.learning import atomic_writers
from vetinari.privacy.envelope import require_privacy_envelope
from vetinari.types import AgentType
from vetinari.utils import privacy_receipt

from .pipeline_core import TrainingRun

logger = logging.getLogger(__name__)


def _count_jsonl_records(dataset_path: str | Path) -> int:
    """Count records in a JSONL dataset without loading the file into memory."""
    with Path(dataset_path).open(encoding="utf-8") as handle:
        return sum(1 for _ in handle)


def _update_replay_buffer(replay_buffer: Any, *, dataset_path: str | Path, run_id: str) -> None:
    """Load dataset records into the replay buffer and persist the buffer."""
    replay_examples: list[dict[str, Any]] = []
    with Path(dataset_path).open(encoding="utf-8") as replay_handle:
        for line in replay_handle:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                loaded = json.loads(stripped)
            except json.JSONDecodeError:
                logger.warning("[TrainingPipeline] %s: Skipping malformed replay JSONL line", run_id)
                continue
            if isinstance(loaded, dict):
                _require_replay_privacy_evidence(loaded, run_id=run_id)
                replay_examples.append(loaded)
    if replay_examples:
        replay_buffer.add(replay_examples)
    replay_buffer.save()
    logger.info("[TrainingPipeline] %s: Training data added to replay buffer", run_id)


def _require_replay_privacy_evidence(record: dict[str, Any], *, run_id: str) -> None:
    """Validate privacy evidence before replay-buffer persistence."""
    if "_privacy_envelope" in record:
        require_privacy_envelope(record)
        return
    metadata = record.get("metadata")
    receipt = metadata.get("privacy_receipt") if isinstance(metadata, dict) else None
    if not isinstance(receipt, dict):
        raise ValueError(f"[TrainingPipeline] {run_id}: replay record lacks privacy receipt")
    if not (
        isinstance(metadata.get("source_dataset"), str)
        or isinstance(metadata.get("dataset_revision"), str)
        or isinstance(metadata.get("provenance"), dict)
    ):
        raise ValueError(f"[TrainingPipeline] {run_id}: replay record lacks provenance")
    privacy_receipt(
        privacy_class=str(receipt.get("privacy_class", "")),
        subject_id=receipt.get("subject_id"),
        retention_days=int(receipt.get("retention_days", 0)),
        source=str(receipt.get("source", "")),
        erasure_token=receipt.get("erasure_token"),
        redaction_applied=bool(receipt.get("redaction_applied", False)),
    )


def _record_improvement_archive(
    *,
    run: TrainingRun,
    run_id: str,
    base_model: str,
    task_key: str,
    epochs: int,
    backend: str,
    model_format: str,
    model_revision: str | None,
    deployed_path: str,
) -> None:
    """Register a deployed training config for future improvement branching."""
    from vetinari.learning.improvement_archive import get_improvement_archive

    archive = get_improvement_archive()
    config_id = archive.store(
        agent_type=AgentType.WORKER.value,
        config={
            "base_model": base_model,
            "task_type": task_key,
            "epochs": epochs,
            "backend": backend,
            "model_format": model_format,
            "model_revision": model_revision,
            "output_model_path": deployed_path,
            "model_manifest_path": run.model_manifest_path,
            "run_id": run_id,
        },
        quality_score=run.eval_score,
    )
    archive.update_score(config_id, run.eval_score)
    logger.info(
        "[TrainingPipeline] %s: Config %s registered in improvement archive",
        run_id,
        config_id,
    )


def _persist_run_record(*, run_dir: Path, run_id: str, run: TrainingRun) -> None:
    """Persist the training run record for audit and debugging."""
    with require_subsystem("training_finalization", "audit_evidence"):
        atomic_writers.write_json_atomic(run_dir / "run.json", dataclasses.asdict(run))


def _emit_training_step_receipt(
    *,
    run: TrainingRun,
    run_id: str,
    task_type: str | None,
    base_model: str,
    backend: str,
    epochs: int,
) -> None:
    """Emit the receipt that makes training progress visible to Control Center."""
    with require_subsystem("training_finalization", "audit_evidence"):
        from vetinari.receipts import record_training_step

        record_training_step(
            project_id=task_type or "training",
            run_id=run_id,
            base_model=base_model,
            algorithm=backend,
            epochs=epochs,
            training_examples=run.training_examples,
            success=run.success,
            eval_score=run.eval_score,
            error=run.error or "",
        )
