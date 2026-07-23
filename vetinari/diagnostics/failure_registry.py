"""Failure registry helpers."""

from __future__ import annotations

import os
from pathlib import Path

from vetinari.analytics.failure_registry import FailureRegistry


def get_failure_registry_path() -> Path:
    """Return the failure registry path.

    Returns:
        JSONL registry path under the user directory.
    """
    return Path(os.environ.get("VETINARI_USER_DIR", ".vetinari")) / "failures.jsonl"


def _record_failure(self: FailureRegistry, failure: dict[str, object]) -> None:
    self.log_failure(
        pipeline="diagnostics",
        task_id=str(failure.get("task_id", "manual")),
        failure_type=str(failure.get("failure_type", "unknown")),
        error_message=str(failure.get("error_message", failure)),
    )


def _install_method(target: type, name: str, method: object) -> None:
    setattr(target, name, method)


if not hasattr(FailureRegistry, "record"):
    _install_method(FailureRegistry, "record", _record_failure)
    _install_method(FailureRegistry, "append", _record_failure)


__all__ = ["FailureRegistry", "get_failure_registry_path"]
