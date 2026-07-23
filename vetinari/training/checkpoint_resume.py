"""Fail-closed training checkpoint resume helpers."""

from __future__ import annotations

import json
import logging
import os
from contextlib import suppress
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class CheckpointResumeError(ValueError):
    """Raised when checkpoint state cannot be trusted."""


class TrainingCheckpointStatus(str, Enum):
    """Canonical durable training checkpoint lifecycle states."""

    RUNNING = "running"
    INTERRUPTED = "interrupted"
    COMPLETED = "completed"
    FAILED = "failed"

    @classmethod
    def coerce(cls, value: str | TrainingCheckpointStatus) -> TrainingCheckpointStatus:
        """Support coerce behavior for Vetinari callers.

        Returns:
            Value produced for the caller.

        Raises:
            CheckpointResumeError: Propagated when validation, persistence, or execution fails.
        """
        if isinstance(value, cls):
            return value
        try:
            return cls(value)
        except ValueError as exc:
            allowed = ", ".join(status.value for status in cls)
            raise CheckpointResumeError(f"unsupported checkpoint status {value!r}; expected one of: {allowed}") from exc


@dataclass(frozen=True, slots=True)
class TrainingCheckpoint:
    """Durable training checkpoint state."""

    run_id: str
    status: TrainingCheckpointStatus
    step: int
    output_dir: str
    updated_at_utc: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    metadata: dict[str, Any] = field(default_factory=dict)

    def __repr__(self) -> str:
        return (
            "TrainingCheckpoint("
            f"run_id={self.run_id!r}, status={self.status.value!r}, "
            f"step={self.step}, output_dir={self.output_dir!r})"
        )

    def __post_init__(self) -> None:
        if not self.run_id.strip():
            raise CheckpointResumeError("run_id is required")
        object.__setattr__(self, "status", TrainingCheckpointStatus.coerce(self.status))
        if self.step < 0:
            raise CheckpointResumeError("step must be non-negative")
        if not self.output_dir.strip():
            raise CheckpointResumeError("output_dir is required")


@dataclass(frozen=True, slots=True)
class ResumeDecision:
    """Resume decision derived from checkpoint state."""

    can_resume: bool
    next_step: int
    blockers: tuple[str, ...]
    checkpoint: TrainingCheckpoint | None

    def __repr__(self) -> str:
        return (
            f"ResumeDecision(can_resume={self.can_resume!r}, next_step={self.next_step}, blockers={len(self.blockers)})"
        )


def write_training_checkpoint_atomic(path: str | Path, checkpoint: TrainingCheckpoint) -> Path:
    """Write checkpoint JSON atomically, preserving the previous good file on failure.

    Args:
        path: Target checkpoint JSON path.
        checkpoint: Validated checkpoint payload to persist.

    Returns:
        The target path that was written.

    Raises:
        OSError: If the temporary checkpoint cannot be written, fsynced, or
            atomically promoted.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(f".{target.name}.tmp")
    payload = asdict(checkpoint)
    data = json.dumps(payload, sort_keys=True, indent=2) + "\n"
    try:
        with temp.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, target)
    except OSError:
        with suppress(FileNotFoundError):
            temp.unlink()
        raise
    return target


def read_training_checkpoint(path: str | Path) -> TrainingCheckpoint:
    """Read and validate checkpoint JSON.

    Returns:
        The parsed checkpoint.

    Raises:
        CheckpointResumeError: if the file is missing, corrupt, or fails schema
            validation.
    """
    target = Path(path)
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise CheckpointResumeError(f"checkpoint missing: {target}") from exc
    except json.JSONDecodeError as exc:
        raise CheckpointResumeError(f"checkpoint corrupt: {target}") from exc
    if not isinstance(raw, dict):
        raise CheckpointResumeError("checkpoint payload must be an object")
    try:
        return TrainingCheckpoint(
            run_id=str(raw["run_id"]),
            status=str(raw["status"]),
            step=int(raw["step"]),
            output_dir=str(raw["output_dir"]),
            updated_at_utc=str(raw.get("updated_at_utc") or datetime.now(timezone.utc).isoformat()),
            metadata=dict(raw.get("metadata") or {}),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise CheckpointResumeError("checkpoint missing required fields") from exc


def decide_training_resume(path: str | Path, *, expected_run_id: str | None = None) -> ResumeDecision:
    """Return a fail-closed resume decision for a training run.

    Returns:
        A resume decision with blockers when checkpoint state cannot be trusted.
    """
    try:
        checkpoint = read_training_checkpoint(path)
    except CheckpointResumeError as exc:
        logger.warning("Training checkpoint is not resumable: %s", path, exc_info=True)
        return ResumeDecision(False, 0, (str(exc),), None)
    blockers: list[str] = []
    if expected_run_id is not None and checkpoint.run_id != expected_run_id:
        blockers.append("checkpoint run_id does not match requested run")
    if checkpoint.status is TrainingCheckpointStatus.COMPLETED:
        blockers.append("checkpoint is already completed")
    if checkpoint.status is TrainingCheckpointStatus.FAILED:
        blockers.append("checkpoint is failed and requires operator review")
    if blockers:
        return ResumeDecision(False, checkpoint.step, tuple(blockers), checkpoint)
    return ResumeDecision(True, checkpoint.step + 1, (), checkpoint)


__all__ = [
    "CheckpointResumeError",
    "ResumeDecision",
    "TrainingCheckpoint",
    "TrainingCheckpointStatus",
    "decide_training_resume",
    "read_training_checkpoint",
    "write_training_checkpoint_atomic",
]
