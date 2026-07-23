"""Atomic JSON persistence adapters for training records."""

from __future__ import annotations

from pathlib import Path

from vetinari.learning import atomic_writers
from vetinari.learning.training_record import TrainingRecord


def write_training_records_jsonl(path: Path, records: list[TrainingRecord]) -> None:
    """Write training records through the canonical atomic JSONL helper.

    Args:
        path: Target JSONL path.
        records: Training records to serialize.
    """
    atomic_writers.write_jsonl_atomic(path, [record.to_dict() for record in records])


def migrate_training_records_jsonl(path: Path) -> int:
    """Migrate training records to the current JSONL schema version."""
    return atomic_writers.migrate_jsonl_schema_version(path, current_version=1)
