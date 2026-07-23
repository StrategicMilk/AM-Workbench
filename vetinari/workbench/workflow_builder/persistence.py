"""Project-scoped workflow-builder persistence."""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
from pathlib import Path

from vetinari.security.audit_scope import require_project_id, scoped_asset_write
from vetinari.workbench.workflow_builder.contracts import (
    WorkflowBuilderError,
    WorkflowGraph,
    WorkflowRuntimeSettings,
    runtime_settings_from_dict,
    workflow_graph_from_dict,
)

DEFAULT_WORKFLOW_BUILDER_STATE_ROOT = Path("outputs") / "workbench" / "spine" / "workflow_builder"
_SAFE_ID = re.compile(r"^[A-Za-z0-9_.-]+$")


class WorkflowBuilderStore:
    """Persist graphs and settings below a configurable runtime state root."""

    def __init__(
        self, *, state_root: Path | str = DEFAULT_WORKFLOW_BUILDER_STATE_ROOT, lock_timeout_seconds: float = 2.0
    ) -> None:
        self.state_root = Path(state_root)
        self.lock_timeout_seconds = lock_timeout_seconds
        if lock_timeout_seconds <= 0:
            raise WorkflowBuilderError("lock-timeout-invalid")

    def project_dir(self, project_id: str) -> Path:
        """Execute the project dir operation.

        Returns:
            Path value produced by project_dir().

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        clean = _safe_id(project_id, "project_id")
        root = self.state_root.resolve()
        path = (root / clean).resolve()
        if root != path and root not in path.parents:
            raise WorkflowBuilderError("state-path-outside-root", str(path))
        return path

    def save_graph(self, project_id: str, graph: WorkflowGraph) -> Path:
        """Execute the save graph operation.

        Args:
            project_id: Project identifier that scopes the operation.
            graph: Graph value consumed by save_graph().

        Returns:
            Path value produced by save_graph().
        """
        path = self.project_dir(project_id) / "graphs" / f"{_safe_id(graph.graph_id, 'graph_id')}.json"
        _atomic_json(path, graph.to_dict(), self.lock_timeout_seconds, project_id=project_id)
        return path

    def load_graph(self, project_id: str, graph_id: str) -> WorkflowGraph:
        """Execute the load graph operation.

        Args:
            project_id: Project identifier that scopes the operation.
            graph_id: Graph id value consumed by load_graph().

        Returns:
            Resolved graph value.

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        path = self.project_dir(project_id) / "graphs" / f"{_safe_id(graph_id, 'graph_id')}.json"
        try:
            return workflow_graph_from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError, KeyError, TypeError, WorkflowBuilderError) as exc:
            raise WorkflowBuilderError("workflow-graph-unreadable", type(exc).__name__) from exc

    def list_graphs(self, project_id: str) -> tuple[WorkflowGraph, ...]:
        """Execute the list graphs operation.

        Returns:
            Collection of graphs values.

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        graph_dir = self.project_dir(project_id) / "graphs"
        if not graph_dir.exists():
            return ()
        graphs: list[WorkflowGraph] = []
        for path in sorted(graph_dir.glob("*.json")):
            try:
                graphs.append(workflow_graph_from_dict(json.loads(path.read_text(encoding="utf-8"))))
            except (OSError, json.JSONDecodeError, KeyError, TypeError, WorkflowBuilderError) as exc:
                raise WorkflowBuilderError("workflow-graph-list-degraded", path.name) from exc
        return tuple(graphs)

    def save_runtime_settings(self, settings: WorkflowRuntimeSettings) -> Path:
        """Execute the save runtime settings operation.

        Returns:
            Path value produced by save_runtime_settings().
        """
        path = self.project_dir(settings.project_id) / "runtime_settings.json"
        _atomic_json(path, settings.to_dict(), self.lock_timeout_seconds, project_id=settings.project_id)
        return path

    def load_runtime_settings(self, project_id: str) -> WorkflowRuntimeSettings:
        """Execute the load runtime settings operation.

        Returns:
            Resolved runtime settings value.

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        path = self.project_dir(project_id) / "runtime_settings.json"
        if not path.exists():
            return WorkflowRuntimeSettings(project_id=project_id, max_parallel_steps=2, safety_mode="simulation_only")
        try:
            return runtime_settings_from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError, KeyError, TypeError, WorkflowBuilderError) as exc:
            raise WorkflowBuilderError("runtime-settings-unreadable", type(exc).__name__) from exc


def _safe_id(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip() or "/" in value or "\\" in value or ".." in value:
        raise WorkflowBuilderError("id-invalid", field_name)
    if not _SAFE_ID.match(value):
        raise WorkflowBuilderError("id-invalid", field_name)
    return value


def _atomic_json(path: Path, payload: dict[str, object], timeout_seconds: float, *, project_id: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with _WorkflowFileLock(path.with_suffix(path.suffix + ".lock"), timeout_seconds):
        fd, tmp_name = tempfile.mkstemp(prefix=f"{path.name}.", suffix=".tmp", dir=path.parent)
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, sort_keys=True, indent=2)
                handle.write("\n")
            os.replace(tmp_path, path)
            # record_asset_written is reached through scoped_asset_write after project scope validation.
            scoped_asset_write(
                asset_id=str(payload.get("workflow_id", path.stem)),
                kind="tool",
                project_id=require_project_id(project_id),
                path=str(path),
                redact_fields=["path"],
            )
        finally:
            with _WorkflowSuppressOSError():
                tmp_path.unlink()


class _WorkflowSuppressOSError:
    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> bool:
        return isinstance(exc, OSError)


class _WorkflowFileLock:
    def __init__(self, path: Path, timeout_seconds: float) -> None:
        self.path = path
        self.timeout_seconds = timeout_seconds
        self.fd: int | None = None

    def __enter__(self) -> None:
        deadline = time.monotonic() + self.timeout_seconds
        while True:
            try:
                self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(self.fd, str(os.getpid()).encode("ascii"))
                return None
            except FileExistsError as exc:
                if time.monotonic() >= deadline:
                    raise WorkflowBuilderError("workflow-state-lock-timeout", str(self.path)) from exc
                time.sleep(0.02)

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if self.fd is not None:
            os.close(self.fd)
        with _WorkflowSuppressOSError():
            self.path.unlink()


__all__ = ["DEFAULT_WORKFLOW_BUILDER_STATE_ROOT", "WorkflowBuilderStore"]
