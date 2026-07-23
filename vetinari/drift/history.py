"""Drift deviation history helpers."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from vetinari.constants import VETINARI_STATE_DIR

logger = logging.getLogger(__name__)

_DEFAULT_HISTORY_PATH = VETINARI_STATE_DIR / "drift" / "deviation_history.jsonl"


def get_deviation_history(path: Path | str | None = None) -> list[dict[str, Any]]:
    """Return recorded drift deviations.

    Returns:
        Deviation history list.
    """
    history_path = Path(path) if path is not None else _DEFAULT_HISTORY_PATH
    if not history_path.exists():
        return [
            {
                "status": "unavailable",
                "reason": "deviation history file is missing",
                "path": str(history_path),
            }
        ]

    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(history_path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            logger.warning("Failed to parse drift deviation history at line %s", line_number, exc_info=True)
            return [
                {
                    "status": "unavailable",
                    "reason": f"deviation history is corrupt at line {line_number}: {exc}",
                    "path": str(history_path),
                }
            ]
        if isinstance(row, dict):
            rows.append(row)
    return rows or [
        {
            "status": "unavailable",
            "reason": "deviation history file contains no records",
            "path": str(history_path),
        }
    ]


__all__ = ["get_deviation_history"]
