"""Pipeline state persistence — checkpoint hints for crash recovery.

Persists pipeline progress to ``~/.vetinari/pipeline-state/{task_id}.json``
after each stage completes.  On restart, ``get_resume_point()`` returns the
last completed stage and its result snapshot for observability. Mid-pipeline
resume is not implemented; the active pipeline always re-executes from stage 0.
Mid-pipeline resume is not implemented as a skip-ahead mechanism.

Pipeline role: sits between the pipeline engine and disk, providing durable
checkpoint hints for crash diagnosis.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from vetinari.constants import get_user_dir
from vetinari.security.sandbox import enforce_blocked_paths

logger = logging.getLogger(__name__)


# Who writes: tests (via monkeypatch) to redirect writes to tmp_path
# Who reads: _get_state_dir() on every call
# Lifecycle: None means "derive from get_user_dir() at call time"; tests set it to a Path
# Lock: no lock needed — only written during test setup, read under store._lock
_STATE_DIR: Path | None = None


def _get_state_dir() -> Path:
    """Return the pipeline state directory, re-reading the env on each call.

    Returns the module-level ``_STATE_DIR`` override when set (used by tests to
    redirect writes to a temporary directory), otherwise derives the path from
    ``get_user_dir()`` so that ``VETINARI_USER_DIR`` env overrides work at
    runtime.
    """
    if _STATE_DIR is not None:
        return _STATE_DIR
    return get_user_dir() / "pipeline-state"


def _state_file(task_id: str) -> Path:
    """Return the state file path for a given task_id.

    Args:
        task_id: The pipeline task/execution identifier.

    Returns:
        Path to the JSON state file.

    Raises:
        ValueError: If the task ID contains path traversal sequences that
            would place the file outside the pipeline state directory.
    """
    state_dir = _get_state_dir()
    target = (state_dir / f"{task_id}.json").resolve()
    if not target.is_relative_to(state_dir.resolve()):
        raise ValueError(f"Task ID contains path traversal: {task_id}")
    return target


class PipelineStateStore:
    """Persists pipeline stage completion to disk for crash recovery.

    Thread-safe.  Each task_id gets its own JSON file containing an ordered
    list of completed stages with timestamps and result snapshots.

    Side effects:
        - Writes to ``~/.vetinari/pipeline-state/{task_id}.json`` on every
          ``mark_stage_complete()`` call.
        - Deletes state files on ``clear_state()``.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()

    def mark_stage_complete(
        self,
        task_id: str,
        stage: str,
        result_snapshot: dict[str, Any] | None = None,
    ) -> None:
        """Record that a pipeline stage completed successfully.

        Appends the stage to the task's state file.  If the stage was already
        recorded, it is updated with the new snapshot.

        Args:
            task_id: The pipeline task/execution identifier.
            stage: The stage name (e.g. ``"intake"``, ``"plan_gen"``).
            result_snapshot: Optional dict snapshot of the stage's output.
        """
        with self._lock:
            state = self._load_state(task_id)
            stages = state.get("stages", [])
            snapshot = result_snapshot if result_snapshot is not None else {}

            # Update existing stage or append new one
            found = False
            for entry in stages:
                if entry.get("stage") == stage:
                    entry["result_snapshot"] = snapshot
                    entry["completed_at"] = datetime.now(timezone.utc).isoformat()
                    found = True
                    break

            if not found:
                stages.append({
                    "stage": stage,
                    "completed_at": datetime.now(timezone.utc).isoformat(),
                    "result_snapshot": snapshot,
                })

            state["stages"] = stages
            state["task_id"] = task_id
            state["last_updated"] = datetime.now(timezone.utc).isoformat()
            state.setdefault("schema_version", 1)

            self._save_state(task_id, state)

        logger.info(
            "Pipeline state: task %s stage %s marked complete",
            task_id,
            stage,
        )

    def get_resume_point(
        self,
        task_id: str,
    ) -> tuple[str, dict[str, Any]] | None:
        """Return the last completed stage and its snapshot for a task.

        NOTE: Mid-pipeline resume is NOT implemented.  The pipeline engine
        always re-executes from stage 0 regardless of what this method returns.
        The return value is used only as an observability hint — it is recorded
        as ``stages["last_run_stage_hint"]`` in the pipeline audit log so
        operators can see that a prior run existed.

        Args:
            task_id: The pipeline task/execution identifier.

        Returns:
            Tuple of (stage_name, result_snapshot) for the last completed
            stage, or None if no state exists.
        """
        with self._lock:
            state = self._load_state(task_id)

        stages = state.get("stages", [])
        if not stages:
            return None

        last = stages[-1]
        return (last["stage"], last.get("result_snapshot", {}))

    def get_completed_stages(self, task_id: str) -> list[str]:
        """Return the names of all completed stages for a task.

        Args:
            task_id: The pipeline task/execution identifier.

        Returns:
            List of stage name strings in completion order.
        """
        with self._lock:
            state = self._load_state(task_id)

        return [entry["stage"] for entry in state.get("stages", [])]

    def clear_state(self, task_id: str) -> None:
        """Remove the state file for a completed or abandoned task.

        Args:
            task_id: The pipeline task/execution identifier.
        """
        path = _state_file(task_id)
        try:
            if path.exists():
                path.unlink()
                logger.info("Pipeline state cleared for task %s", task_id)
        except OSError as exc:
            logger.warning(
                "Could not delete pipeline state for %s — file may remain: %s",
                task_id,
                exc,
            )

    def _load_state(self, task_id: str) -> dict[str, Any]:
        """Load state from disk. Caller must hold self._lock."""
        path = _state_file(task_id)
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning(
                "Could not read pipeline state for %s — starting fresh: %s",
                task_id,
                exc,
            )
            return {}

    def _save_state(self, task_id: str, state: dict[str, Any]) -> None:
        """Write state to disk atomically. Caller must hold self._lock.

        Writes to a ``.tmp`` file first, then renames it over the final path.
        An interrupted write leaves the stale file intact rather than leaving
        a half-written JSON file that would corrupt resume on next restart.
        """
        path = _state_file(task_id)
        # Sandbox gate: fail closed if the resolved path falls inside a blocked
        # directory (e.g., ~/.ssh, /etc). Runs before mkdir so we do not create
        # directories in protected trees either.
        enforce_blocked_paths(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(".tmp")
        try:
            tmp_path.write_text(
                json.dumps(state, indent=2, default=str),
                encoding="utf-8",
            )
            os.replace(tmp_path, path)
        except OSError as exc:
            logger.error(
                "Could not write pipeline state for %s — resume will not work: %s",
                task_id,
                exc,
            )
            # Remove the temp file if the rename failed, so stale .tmp files
            # do not accumulate on disk across restart cycles.
            with contextlib.suppress(OSError):
                tmp_path.unlink(missing_ok=True)


# ── Singleton ────────────────────────────────────────────────────────────────

_store: PipelineStateStore | None = None
_store_lock = threading.Lock()


def get_pipeline_state_store() -> PipelineStateStore:
    """Return the process-wide PipelineStateStore singleton.

    Uses double-checked locking so the common read-path never acquires the lock.

    Returns:
        The singleton PipelineStateStore instance.
    """
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                _store = PipelineStateStore()
    return _store


def reset_pipeline_state_store() -> None:
    """Reset the singleton for test isolation."""
    global _store
    with _store_lock:
        _store = None
