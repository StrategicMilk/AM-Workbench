"""Atomic JSON persistence adapters for workbench eval records."""

from __future__ import annotations

from pathlib import Path

from vetinari.learning import atomic_writers
from vetinari.workbench.evals import EvalResult


def write_eval_records_jsonl(path: Path, records: list[EvalResult]) -> None:
    """Write eval records through the canonical atomic JSONL helper.

    Args:
        path: Target JSONL path.
        records: Eval records to serialize.
    """
    atomic_writers.write_jsonl_atomic(path, records)
