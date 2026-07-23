"""Atomic JSON persistence adapters for method-library records."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from vetinari.learning import atomic_writers


def write_method_records_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    """Write method records through the canonical atomic JSONL helper.

    Args:
        path: Target JSONL path.
        records: Method-library rows to serialize.
    """
    atomic_writers.write_jsonl_atomic(path, records)
