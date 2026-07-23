"""Training resource ledger with elapsed-time and GPU-hour cost fields."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from vetinari.workbench.cost.jsonl_rotator import JsonlAppendResult, RotatingJsonlStore
from vetinari.workbench.cost.token_cost_split import PricingConfigError, load_rotation_settings
from vetinari.workbench.resource_cockpit.cost_calculator import calculate_resource_cost

_ROTATION_KEY = "ledger_jsonl"
logger = logging.getLogger(__name__)


def append_training_ledger_entry(
    path: str | Path,
    *,
    project_id: str,
    job_id: str,
    model: str,
    elapsed_s: float,
    gpu_seconds: float,
    tokens_in: int = 0,
    tokens_out: int = 0,
    metadata: Mapping[str, Any] | None = None,
    max_bytes: int = 1_048_576,
    max_lines: int = 10_000,
) -> JsonlAppendResult:
    """Append a redacted training ledger row with monetized GPU cost.

    Returns:
        The rotating JSONL append result.
    """
    cost = calculate_resource_cost(
        model=model,
        target_compute="gpu" if gpu_seconds > 0 else "cpu",
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        duration_s=gpu_seconds,
    )
    row = {
        "schema_version": "1.0",
        "kind": "training_resource_ledger",
        "project_id": project_id,
        "job_id": job_id,
        "model": model,
        "elapsed_s": elapsed_s,
        "gpu_hours": round(max(0.0, float(gpu_seconds)) / 3600.0, 8),
        "cost": cost.to_dict(),
        "metadata": dict(metadata or {}),
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
        logger.warning("Training ledger rotation config unavailable; using caller defaults: %s", exc)
    return RotatingJsonlStore(path, max_bytes=max_bytes, max_lines=max_lines, backup_count=backup_count).append(row)


__all__ = ["append_training_ledger_entry"]
