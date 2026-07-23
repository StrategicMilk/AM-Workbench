"""Server-side controls for training jobs.

This module is the shared authority for CLI and HTTP training controls. It
wraps the scheduler behind a lock, emits durable audit rows when configured,
and returns explicit receipts instead of letting unsupported commands silently
look successful.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)
_MAX_AUDIT_LINES = 1_000


class TrainingControlError(RuntimeError):
    """Raised when a training control cannot be applied safely."""


@dataclass(frozen=True, slots=True)
class TrainingJobSnapshot:
    """Serializable view of a training job."""

    job_id: str
    status: str
    activity_description: str
    started_at: str
    task_type: str | None = None
    progress: float = 0.0

    def __repr__(self) -> str:
        return (
            "TrainingJobSnapshot("
            f"job_id={self.job_id!r}, status={self.status!r}, task_type={self.task_type!r}, "
            f"progress={self.progress!r})"
        )


@dataclass(frozen=True, slots=True)
class TrainingControlReceipt:
    """Receipt for one training control action."""

    control: str
    status: str
    message: str
    created_at_utc: str
    job_id: str | None = None
    audit_path: str | None = None

    def __repr__(self) -> str:
        return (
            "TrainingControlReceipt("
            f"control={self.control!r}, status={self.status!r}, job_id={self.job_id!r}, "
            f"audit_path={self.audit_path!r})"
        )

    @property
    def passed(self) -> bool:
        """Whether the control action succeeded."""
        return self.status == "accepted"

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible receipt payload."""
        return asdict(self)


class TrainingControlService:
    """Fail-closed control surface for a scheduler instance."""

    def __init__(self, scheduler: Any, *, audit_path: Path | None = None) -> None:
        if scheduler is None:
            raise TrainingControlError("training scheduler unavailable")
        self._scheduler = scheduler
        self._audit_path = audit_path
        self._checkpoint_dir = (
            audit_path.parent / "training-checkpoints"
            if audit_path is not None
            else Path(".vetinari") / "training-checkpoints"
        )
        self._lock = threading.RLock()

    def status(self) -> TrainingControlReceipt:
        """Return scheduler status as a receipt.

        Returns:
            Accepted receipt describing the current scheduler state.
        """
        job = self._current_job_snapshot()
        message = "training idle" if job is None else f"training job {job.job_id} is {job.status}"
        return self._receipt("status", "accepted", message, job.job_id if job else None)

    def jobs(self) -> tuple[TrainingJobSnapshot, ...]:
        """Return known current and historical jobs.

        Returns:
            Sorted snapshots for current and historical training jobs.
        """
        with self._lock:
            snapshots: dict[str, TrainingJobSnapshot] = {}
            current = self._current_job_snapshot()
            if current is not None:
                snapshots[current.job_id] = current
            for row in getattr(self._scheduler, "_history", ()):
                if not isinstance(row, dict):
                    continue
                job_id = str(row.get("job_id", "")).strip()
                if not job_id or job_id in snapshots:
                    continue
                snapshots[job_id] = TrainingJobSnapshot(
                    job_id=job_id,
                    status=str(row.get("status", "recorded")),
                    activity_description=str(row.get("activity_description", "")),
                    started_at=str(row.get("started_at", "")),
                    task_type=row.get("task_type"),
                    progress=float(row.get("progress", 0.0) or 0.0),
                )
            return tuple(snapshots[key] for key in sorted(snapshots))

    def start(self, *, skill: str | None = None) -> TrainingControlReceipt:
        """Start a manual training job through the scheduler API.

        Returns:
            Receipt describing whether the start request was accepted.
        """
        activity = "Manual training cycle" if not skill else f"Manual training cycle for skill '{skill}'"
        with self._lock:
            try:
                job_id = self._scheduler.start_manual_cycle(activity_description=activity, task_type=skill)
            except Exception as exc:
                logger.warning("training start rejected by scheduler: %s", _redact(str(exc)))
                return self._receipt("start", "rejected", f"training start failed: {_redact(str(exc))}", None)
            if job_id == "already_running":
                current = self._current_job_snapshot()
                return self._receipt(
                    "start",
                    "rejected",
                    "training already in progress",
                    current.job_id if current else None,
                )
            return self._receipt("start", "accepted", "training cycle initiated", str(job_id))

    def pause(self, *, job_id: str | None = None) -> TrainingControlReceipt:
        """Pause the current job via the scheduler state transition.

        Returns:
            Receipt describing whether the pause request was accepted.
        """
        with self._lock:
            try:
                current = self._require_current_job(job_id, "pause")
            except TrainingControlError as exc:
                logger.warning("training pause rejected: %s", _redact(str(exc)))
                return self._receipt("pause", "rejected", str(exc), job_id)
            self._scheduler.pause_for_user_request()
            return self._receipt("pause", "accepted", "training job paused", current.job_id)

    def resume(self, *, job_id: str | None = None) -> TrainingControlReceipt:
        """Resume the current paused job via the scheduler state transition.

        Returns:
            Receipt describing whether the resume request was accepted.
        """
        with self._lock:
            try:
                current = self._require_current_job(job_id, "resume")
            except TrainingControlError as exc:
                logger.warning("training resume rejected: %s", _redact(str(exc)))
                return self._receipt("resume", "rejected", str(exc), job_id)
            if current.status != "paused":
                return self._receipt("resume", "rejected", f"job is {current.status}, not paused", current.job_id)
            self._scheduler.resume_after_user_request()
            after = self._current_job_snapshot()
            if after is not None and after.status == "paused":
                return self._receipt(
                    "resume",
                    "rejected",
                    "job remains paused because scheduler idle gate is closed",
                    current.job_id,
                )
            return self._receipt("resume", "accepted", "training job resumed", current.job_id)

    def stop(self, *, job_id: str | None = None) -> TrainingControlReceipt:
        """Stop the current job and mark scheduler state explicitly.

        Returns:
            Receipt describing whether the stop request was accepted.
        """
        with self._lock:
            try:
                current = self._require_current_job(job_id, "stop")
            except TrainingControlError as exc:
                logger.warning("training stop rejected: %s", _redact(str(exc)))
                return self._receipt("stop", "rejected", str(exc), job_id)
            scheduler_lock = getattr(self._scheduler, "_lock", None)
            if scheduler_lock is None:
                return self._receipt("stop", "rejected", "scheduler lock unavailable", current.job_id)
            with scheduler_lock:
                live = getattr(self._scheduler, "_current_job", None)
                if live is None:
                    return self._receipt("stop", "rejected", "no active training job", current.job_id)
                live.status = "stopped"
                live.progress = min(float(getattr(live, "progress", 0.0) or 0.0), 1.0)
                history = getattr(self._scheduler, "_history", None)
                if isinstance(history, list):
                    for row in history:
                        if isinstance(row, dict) and row.get("job_id") == current.job_id:
                            row["status"] = "stopped"
            return self._receipt("stop", "accepted", "training job stopped", current.job_id)

    def cancel(self, *, job_id: str | None = None) -> TrainingControlReceipt:
        """Cancel the current job and leave an explicit interrupted state.

        Returns:
            Receipt describing whether the cancel request was accepted.
        """
        return self._set_terminal_status(
            control="cancel",
            target_status="cancelled",
            message="training job cancelled",
            job_id=job_id,
        )

    def checkpoint(self, *, job_id: str | None = None) -> TrainingControlReceipt:
        """Write a durable checkpoint receipt for the current training job.

        Returns:
            Receipt describing whether checkpoint persistence succeeded.
        """
        from vetinari.training.checkpoint_resume import (
            TrainingCheckpoint,
            TrainingCheckpointStatus,
            write_training_checkpoint_atomic,
        )

        with self._lock:
            try:
                current = self._require_current_job(job_id, "checkpoint")
            except TrainingControlError as exc:
                logger.warning("training checkpoint rejected: %s", _redact(str(exc)))
                return self._receipt("checkpoint", "rejected", str(exc), job_id)
            status = TrainingCheckpointStatus.RUNNING
            if current.status == "completed":
                status = TrainingCheckpointStatus.COMPLETED
            elif current.status == "failed":
                status = TrainingCheckpointStatus.FAILED
            elif current.status in {"paused", "stopped", "cancelled"}:
                status = TrainingCheckpointStatus.INTERRUPTED
            checkpoint_path = self._checkpoint_dir / f"{current.job_id}.json"
            try:
                write_training_checkpoint_atomic(
                    checkpoint_path,
                    TrainingCheckpoint(
                        run_id=current.job_id,
                        status=status,
                        step=max(0, int(current.progress * 100)),
                        output_dir=str(self._checkpoint_dir),
                        metadata={
                            "activity_description": current.activity_description,
                            "task_type": current.task_type,
                            "source": "TrainingControlService.checkpoint",
                        },
                    ),
                )
            except Exception as exc:
                logger.warning("training checkpoint write failed: %s", _redact(str(exc)))
                return self._receipt(
                    "checkpoint",
                    "rejected",
                    f"training checkpoint failed: {_redact(str(exc))}",
                    current.job_id,
                )
            return self._receipt(
                "checkpoint",
                "accepted",
                f"training checkpoint written: {checkpoint_path}",
                current.job_id,
            )

    def _set_terminal_status(
        self,
        *,
        control: str,
        target_status: str,
        message: str,
        job_id: str | None,
    ) -> TrainingControlReceipt:
        with self._lock:
            try:
                current = self._require_current_job(job_id, control)
            except TrainingControlError as exc:
                logger.warning("training %s rejected: %s", control, _redact(str(exc)))
                return self._receipt(control, "rejected", str(exc), job_id)
            scheduler_lock = getattr(self._scheduler, "_lock", None)
            if scheduler_lock is None:
                return self._receipt(control, "rejected", "scheduler lock unavailable", current.job_id)
            with scheduler_lock:
                live = getattr(self._scheduler, "_current_job", None)
                if live is None:
                    return self._receipt(control, "rejected", "no active training job", current.job_id)
                live.status = target_status
                live.progress = min(float(getattr(live, "progress", 0.0) or 0.0), 1.0)
                history = getattr(self._scheduler, "_history", None)
                if isinstance(history, list):
                    for row in history:
                        if isinstance(row, dict) and row.get("job_id") == current.job_id:
                            row["status"] = target_status
            return self._receipt(control, "accepted", message, current.job_id)

    def _require_current_job(self, job_id: str | None, control: str) -> TrainingJobSnapshot:
        current = self._current_job_snapshot()
        if current is None:
            raise TrainingControlError(f"cannot {control}: no active training job")
        if job_id is not None and job_id != current.job_id:
            raise TrainingControlError(f"cannot {control}: active job is {current.job_id!r}, not {job_id!r}")
        return current

    def _current_job_snapshot(self) -> TrainingJobSnapshot | None:
        job = getattr(self._scheduler, "current_job", None)
        if job is None:
            return None
        return TrainingJobSnapshot(
            job_id=str(getattr(job, "job_id", "")),
            status=str(getattr(job, "status", "unknown")),
            activity_description=str(getattr(job, "activity_description", "")),
            started_at=str(getattr(job, "started_at", "")),
            task_type=getattr(job, "task_type", None),
            progress=float(getattr(job, "progress", 0.0) or 0.0),
        )

    def _receipt(self, control: str, status: str, message: str, job_id: str | None) -> TrainingControlReceipt:
        receipt = TrainingControlReceipt(
            control=control,
            status=status,
            message=_redact(message),
            created_at_utc=datetime.now(timezone.utc).isoformat(),
            job_id=job_id,
            audit_path=str(self._audit_path) if self._audit_path is not None else None,
        )
        self._append_audit(receipt)
        return receipt

    def _append_audit(self, receipt: TrainingControlReceipt) -> None:
        if self._audit_path is None:
            return
        self._audit_path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(receipt.to_dict(), sort_keys=True) + "\n"
        with self._lock, self._audit_path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
        _prune_jsonl(self._audit_path)


_SERVICE: TrainingControlService | None = None
_SERVICE_LOCK = threading.Lock()


def get_training_control_service() -> TrainingControlService:
    """Return the process-wide training control service.

    Returns:
        Lazily initialized training control service.
    """
    global _SERVICE
    if _SERVICE is not None:
        return _SERVICE
    with _SERVICE_LOCK:
        if _SERVICE is not None:
            return _SERVICE
        from vetinari.training.idle_scheduler import get_training_scheduler

        _SERVICE = TrainingControlService(
            get_training_scheduler(),
            audit_path=Path(".vetinari") / "training-control-audit.jsonl",
        )
        return _SERVICE


def reset_training_control_service_for_test() -> None:
    """Clear the process-wide service for isolated tests."""
    global _SERVICE
    with _SERVICE_LOCK:
        _SERVICE = None


def _redact(message: str) -> str:
    try:
        from vetinari.security.redaction import redact_text
    except Exception:
        logger.warning("central redaction unavailable; using fallback training-control redactor")
        redacted = message
        for marker in ("token=", "password=", "secret=", "api_key="):
            lower = redacted.lower()
            if marker in lower:
                prefix = lower.split(marker, 1)[0]
                redacted = f"{redacted[: len(prefix)]}{marker}<redacted>"
        return redacted
    return redact_text(message)


def _prune_jsonl(path: Path, *, max_lines: int = _MAX_AUDIT_LINES) -> None:
    lines = path.read_text(encoding="utf-8").splitlines()
    if len(lines) <= max_lines:
        return
    _replace_text_atomic(path, "\n".join(lines[-max_lines:]) + "\n")


def _replace_text_atomic(path: Path, text: str) -> None:
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with tmp_path.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


__all__ = [
    "TrainingControlError",
    "TrainingControlReceipt",
    "TrainingControlService",
    "TrainingJobSnapshot",
    "get_training_control_service",
    "reset_training_control_service_for_test",
]
