"""Durable job registry helpers for TrainingManager."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from vetinari.constants import get_user_dir

from . import atomic_writers

if TYPE_CHECKING:
    from .training_manager import TrainingJob, TrainingResult

_TRAINING_JOBS_SCHEMA_VERSION = 1
_TRAINING_JOBS_FILENAME = "training_jobs.json"


def _resolve_training_jobs_path(data_path: str | None, jobs_path: str | Path | None) -> Path:
    """Resolve the durable training-job registry path.

    Args:
        data_path: Optional training data path supplied to TrainingManager.
        jobs_path: Optional explicit registry path.

    Returns:
        JSON registry path for persisted training job status.
    """
    if jobs_path is not None:
        return Path(jobs_path)
    if data_path:
        data_root = Path(data_path)
        if data_root.suffix:
            return data_root.with_name(_TRAINING_JOBS_FILENAME)
        return data_root / _TRAINING_JOBS_FILENAME
    return get_user_dir() / "training" / _TRAINING_JOBS_FILENAME


def _training_result_to_dict(result: TrainingResult | None) -> dict[str, Any] | None:
    """Serialize an optional TrainingResult for the job registry."""
    if result is None:
        return None
    return _jsonable_training_value(asdict(result))


def _jsonable_training_value(value: object) -> Any:
    """Convert dynamic training job metadata to JSON-compatible values."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _jsonable_training_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable_training_value(item) for item in value]
    return str(value)


def _training_result_from_dict(raw_result: object) -> TrainingResult | None:
    """Deserialize an optional TrainingResult from the job registry."""
    from .training_manager import TrainingResult

    if raw_result is None:
        return None
    if not isinstance(raw_result, Mapping):
        raise ValueError("training job result must be an object or null")
    raw_metrics = raw_result.get("metrics", {})
    if not isinstance(raw_metrics, Mapping):
        raise ValueError("training job result metrics must be an object")
    return TrainingResult(
        success=bool(raw_result.get("success", False)),
        model_path=str(raw_result["model_path"]) if raw_result.get("model_path") is not None else None,
        metrics=dict(raw_metrics),
        duration_seconds=float(raw_result.get("duration_seconds", 0.0)),
        error=str(raw_result["error"]) if raw_result.get("error") is not None else None,
    )


def _training_job_to_dict(job: TrainingJob) -> dict[str, Any]:
    """Serialize a TrainingJob for the durable registry."""
    raw = asdict(job)
    raw["result"] = _training_result_to_dict(job.result)
    return raw


def _training_job_from_dict(raw_job: Mapping[str, Any]) -> TrainingJob:
    """Deserialize a TrainingJob from the durable registry."""
    from .training_manager import TrainingJob

    return TrainingJob(
        job_id=str(raw_job["job_id"]),
        status=str(raw_job["status"]),
        provider=str(raw_job["provider"]),
        model_id=str(raw_job["model_id"]),
        created_at=str(raw_job["created_at"]),
        progress=float(raw_job.get("progress", 0.0)),
        result=_training_result_from_dict(raw_job.get("result")),
    )


class TrainingManagerJobRegistryMixin:
    """Persist and expose TrainingManager job state."""

    if TYPE_CHECKING:
        _jobs: Any
        _jobs_lock: Any
        _jobs_path: Any

    def _load_jobs(self) -> dict[str, TrainingJob]:
        """Load durable training job status from disk.

        Returns:
            Mapping of job id to the last persisted TrainingJob state.

        Raises:
            ValueError: If the persisted registry is malformed or unsupported.
        """
        if not self._jobs_path.exists():
            return {}
        try:
            data = json.loads(self._jobs_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"Training job registry {self._jobs_path} is unreadable") from exc
        if not isinstance(data, dict):
            raise ValueError(f"Training job registry {self._jobs_path} must contain a JSON object")
        schema_version = int(data.get("schema_version", 0) or 0)
        if schema_version != _TRAINING_JOBS_SCHEMA_VERSION:
            raise ValueError(f"Training job registry {self._jobs_path} has unsupported schema_version {schema_version}")
        raw_jobs = data.get("jobs", [])
        if not isinstance(raw_jobs, list):
            raise ValueError(f"Training job registry {self._jobs_path} jobs field must be a list")
        jobs: dict[str, TrainingJob] = {}
        for index, raw_job in enumerate(raw_jobs):
            if not isinstance(raw_job, dict):
                raise ValueError(f"Training job registry {self._jobs_path} job {index} must be an object")
            job = _training_job_from_dict(raw_job)
            jobs[job.job_id] = job
        return jobs

    def _persist_jobs(self) -> None:
        """Persist all visible training job states atomically."""
        payload = {
            "schema_version": _TRAINING_JOBS_SCHEMA_VERSION,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "jobs": [_training_job_to_dict(job) for job in self._jobs.values()],
        }
        atomic_writers.write_json_atomic(self._jobs_path, payload)

    def _upsert_job(self, job: TrainingJob) -> None:
        """Store and durably publish one job state."""
        with self._jobs_lock:
            self._jobs[job.job_id] = job
            self._persist_jobs()

    def get_training_status(self, job_id: str) -> TrainingJob | None:
        """Look up a training job by its ID.

        Returns:
            Value produced for the caller.
        """
        with self._jobs_lock:
            return self._jobs.get(job_id)

    def list_jobs(self) -> list[TrainingJob]:
        """Return all tracked training jobs.

        Returns:
            Value produced for the caller.
        """
        with self._jobs_lock:
            return list(self._jobs.values())
