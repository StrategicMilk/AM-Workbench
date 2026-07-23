"""Background GGUF download job helpers for model discovery."""

from __future__ import annotations

import threading
import uuid
from collections.abc import MutableMapping
from datetime import datetime, timezone
from pathlib import Path
from threading import Event
from typing import Any, Protocol, cast

from vetinari.model_discovery_cache import _public_download_state
from vetinari.model_discovery_types import RepoModelFile


class _GgufDownloadOwner(Protocol):
    """Protocol implemented by the model discovery download facade."""

    download_state_path: Path

    def _persist_job_locked(self, download_id: str) -> None: ...

    def _complete_download(
        self,
        repo_file: RepoModelFile,
        destination: Path,
        cancel_event: Event | None = None,
        job_id: str | None = None,
    ) -> dict[str, Any]: ...


_DownloadJobs = MutableMapping[str, dict[str, Any]]
_CancelEvents = MutableMapping[str, Event]


def _start_gguf_download_job(
    owner: _GgufDownloadOwner,
    repo_file: RepoModelFile,
    destination: Path,
    *,
    download_jobs: _DownloadJobs,
    cancel_events: _CancelEvents,
    download_lock: Any,
    download_threads: MutableMapping[str, threading.Thread] | None = None,
) -> dict[str, Any]:
    job_id = uuid.uuid4().hex
    cancel_event = Event()
    state = _build_gguf_download_state(owner, job_id, repo_file, destination)

    with download_lock:
        download_jobs[job_id] = dict(state)
        cancel_events[job_id] = cancel_event
        owner._persist_job_locked(job_id)

    worker_thread = threading.Thread(
        target=_run_gguf_download_worker,
        args=(
            owner,
            job_id,
            repo_file,
            destination,
            cancel_event,
            download_jobs,
            cancel_events,
            download_lock,
            download_threads,
        ),
        name=f"model-download-{job_id[:8]}",
        daemon=True,
    )
    if download_threads is not None:
        with download_lock:
            download_threads[job_id] = worker_thread
    worker_thread.start()
    return cast(dict[str, Any], _public_download_state(state))


def _build_gguf_download_state(
    owner: _GgufDownloadOwner,
    job_id: str,
    repo_file: RepoModelFile,
    destination: Path,
) -> dict[str, Any]:
    return {
        "download_id": job_id,
        "status": "started",
        "repo_id": repo_file.repo_id,
        "filename": repo_file.filename,
        "revision": repo_file.revision,
        "backend": "llama_cpp",
        "format": "gguf",
        "artifact_type": "file",
        "path": str(destination),
        "bytes_total": repo_file.size,
        "bytes_downloaded": 0,
        "error": None,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "completed_at": None,
        "_state_path": str(owner.download_state_path),
    }


def _run_gguf_download_worker(
    owner: _GgufDownloadOwner,
    job_id: str,
    repo_file: RepoModelFile,
    destination: Path,
    cancel_event: Event,
    download_jobs: _DownloadJobs,
    cancel_events: _CancelEvents,
    download_lock: Any,
    download_threads: MutableMapping[str, threading.Thread] | None = None,
) -> None:
    with download_lock:
        download_jobs[job_id]["status"] = "running"
        owner._persist_job_locked(job_id)
    try:
        result = owner._complete_download(repo_file, destination, cancel_event=cancel_event, job_id=job_id)
        with download_lock:
            download_jobs[job_id].update(result)
            download_jobs[job_id]["completed_at"] = datetime.now(timezone.utc).isoformat()
            owner._persist_job_locked(job_id)
    except Exception as exc:
        _record_gguf_download_worker_error(owner, job_id, cancel_event, exc, download_jobs, download_lock)
    finally:
        with download_lock:
            cancel_events.pop(job_id, None)
            if download_threads is not None:
                download_threads.pop(job_id, None)


def _record_gguf_download_worker_error(
    owner: _GgufDownloadOwner,
    job_id: str,
    cancel_event: Event,
    exc: Exception,
    download_jobs: _DownloadJobs,
    download_lock: Any,
) -> None:
    status = "canceled" if cancel_event.is_set() else "failed"
    with download_lock:
        download_jobs[job_id].update({
            "status": status,
            "error": str(exc),
            "completed_at": datetime.now(timezone.utc).isoformat(),
        })
        owner._persist_job_locked(job_id)
