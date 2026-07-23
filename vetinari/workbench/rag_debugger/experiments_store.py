"""Rotating, redacted RAG debugger experiment JSONL store."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from vetinari.workbench.cost.jsonl_rotator import JsonlAppendResult, RotatingJsonlStore
from vetinari.workbench.cost.token_cost_split import PricingConfigError, load_rotation_settings

_ROTATION_KEY = "experiments_jsonl"
logger = logging.getLogger(__name__)


def append_experiment_record(
    path: str | Path,
    record: Mapping[str, Any],
    *,
    max_bytes: int = 1_048_576,
    max_lines: int = 10_000,
    schema_version: int | str = "1.0",
    kind: str = "rag_debugger_experiment",
) -> JsonlAppendResult:
    """Append a RAG experiment record through the shared redacting rotator.

    Args:
        path: JSONL ledger path.
        record: Experiment payload to redact and append.
        max_bytes: Maximum active ledger size before rotation.
        max_lines: Maximum active ledger line count before rotation.
        schema_version: JSONL envelope schema version.
        kind: JSONL envelope kind.

    Returns:
        The append result from the rotating store.
    """
    envelope = {
        "schema_version": schema_version,
        "kind": kind,
        "payload": dict(record),
    }
    backup_count = 10
    try:
        rotation = load_rotation_settings(
            _ROTATION_KEY,
            default_max_bytes=max_bytes,
            default_max_lines=max_lines,
            default_backup_count=backup_count,
        )
        max_bytes = rotation.max_bytes
        max_lines = rotation.max_lines
        backup_count = rotation.backup_count
    except PricingConfigError as exc:
        logger.warning("RAG experiment rotation config unavailable; using caller defaults: %s", exc)
    return RotatingJsonlStore(path, max_bytes=max_bytes, max_lines=max_lines, backup_count=backup_count).append(
        envelope
    )


__all__ = ["append_experiment_record"]
