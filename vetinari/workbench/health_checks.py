"""Stable read-only Workbench health entrypoints."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from vetinari.workbench.status import build_assistant_status_context, build_workbench_status_snapshot
from vetinari.workbench.status.contracts import WorkbenchStatusSnapshot


def get_workbench_health_snapshot(
    *,
    project_id: str = "default",
    dependency_snapshots: Mapping[str, Any] | None = None,
    config_path: str | Path = "config/workbench/status_checks.yaml",
) -> WorkbenchStatusSnapshot:
    """Return a read-only Workbench status snapshot."""
    return build_workbench_status_snapshot(
        project_id=project_id,
        dependency_snapshots=dependency_snapshots,
        config_path=config_path,
    )


def get_workbench_assistant_status_context(
    *,
    project_id: str = "default",
    dependency_snapshots: Mapping[str, Any] | None = None,
    config_path: str | Path = "config/workbench/status_checks.yaml",
) -> dict[str, Any]:
    """Return a redacted assistant context for Workbench health."""
    return build_assistant_status_context(
        get_workbench_health_snapshot(
            project_id=project_id,
            dependency_snapshots=dependency_snapshots,
            config_path=config_path,
        )
    )


__all__ = ["get_workbench_assistant_status_context", "get_workbench_health_snapshot"]
