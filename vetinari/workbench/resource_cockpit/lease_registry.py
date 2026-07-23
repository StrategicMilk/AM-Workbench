"""Persistent per-project lease registry for Workbench resource accounting."""

from __future__ import annotations

import threading
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from vetinari.learning.atomic_writers import write_json_atomic


class LeaseRegistryError(RuntimeError):
    """Raised when lease registry state is unavailable or cannot be trusted."""


class PersistentLeaseRegistry:
    """Atomic JSON lease registry keyed by project and lease id."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    def snapshot(self) -> dict[str, Any]:
        """Return current state, raising on corrupt or unreadable data."""
        return self._read_state()

    def register(
        self,
        *,
        project_id: str,
        lease_id: str,
        lane: str,
        workload_id: str,
        target_compute: str,
        target_model: str,
        acquired_at_utc: str | None = None,
    ) -> dict[str, Any]:
        """Persist an active lease under its project.

        Returns:
            The active lease row written to the registry.
        """
        project_id = _require_text(project_id, "project_id")
        lease_id = _require_text(lease_id, "lease_id")
        row = {
            "lease_id": lease_id,
            "lane": _require_text(lane, "lane"),
            "workload_id": _require_text(workload_id, "workload_id"),
            "target_compute": _require_text(target_compute, "target_compute"),
            "target_model": _require_text(target_model, "target_model"),
            "acquired_at_utc": acquired_at_utc or _now_utc(),
            "status": "active",
        }
        with self._lock:
            state = self._read_state()
            project = state["projects"].setdefault(project_id, {"active_leases": {}})
            project["active_leases"][lease_id] = row
            state["updated_at_utc"] = _now_utc()
            self._write_state(state)
            return row

    def release(self, *, project_id: str, lease_id: str, released_at_utc: str | None = None) -> None:
        """Remove an active lease and record a compact release tombstone."""
        project_id = _require_text(project_id, "project_id")
        lease_id = _require_text(lease_id, "lease_id")
        with self._lock:
            state = self._read_state()
            project = state["projects"].setdefault(project_id, {"active_leases": {}})
            active = project.setdefault("active_leases", {})
            released = active.pop(lease_id, None)
            if released is not None:
                project["last_released_lease"] = {
                    "lease_id": lease_id,
                    "released_at_utc": released_at_utc or _now_utc(),
                }
            state["updated_at_utc"] = _now_utc()
            self._write_state(state)

    def _read_state(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"schema_version": "1.0", "projects": {}, "updated_at_utc": _now_utc()}
        try:
            import json

            state = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise LeaseRegistryError(f"lease registry unreadable: {self.path}") from exc
        if not isinstance(state, dict) or state.get("schema_version") != "1.0":
            raise LeaseRegistryError(f"lease registry invalid schema: {self.path}")
        projects = state.get("projects")
        if not isinstance(projects, dict):
            raise LeaseRegistryError(f"lease registry projects must be a mapping: {self.path}")
        for project_id, project in projects.items():
            if not isinstance(project_id, str) or not project_id:
                raise LeaseRegistryError("lease registry project_id must be non-empty")
            if not isinstance(project, Mapping) or not isinstance(project.get("active_leases", {}), dict):
                raise LeaseRegistryError(f"lease registry active_leases invalid for {project_id}")
        return state

    def _write_state(self, state: dict[str, Any]) -> None:
        try:
            write_json_atomic(self.path, state)
        except OSError as exc:
            raise LeaseRegistryError(f"lease registry write failed: {self.path}") from exc


def _require_text(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise LeaseRegistryError(f"{field_name} must be non-empty")
    return value.strip()


def _now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


__all__ = ["LeaseRegistryError", "PersistentLeaseRegistry"]
