# SPDX-FileCopyrightText: 2024-2026 Vetinari Contributors
# SPDX-License-Identifier: Apache-2.0
"""Managed model download lifecycle for model discovery.

The download mixin tracks foreground and background downloads, validates cache
hits, and persists bounded status dictionaries for the public facade.
"""

from __future__ import annotations

import importlib
import logging
import os
import shutil
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from threading import Event
from typing import Any, Protocol, cast

from vetinari.constants import DEFAULT_NATIVE_MODELS_DIR, MODEL_DISCOVERY_TIMEOUT, OPERATOR_MODELS_CACHE_DIR
from vetinari.model_discovery_artifacts import (
    _NATIVE_DOWNLOAD_BACKENDS,
    _ensure_free_space,
    _materialized_snapshot_files,
    _normalize_backend,
    _normalize_model_format,
    _resolve_destination,
    _resolve_snapshot_destination,
    _sha256_file,
    _snapshot_marker_path,
    _validate_existing_download,
    _validate_existing_snapshot,
    _validate_model_header,
    _write_download_marker,
    _write_snapshot_marker,
)
from vetinari.model_discovery_cache import _load_download_state, _public_download_state, _write_download_state
from vetinari.model_discovery_download_jobs import _start_gguf_download_job
from vetinari.model_discovery_repository import _ModelDiscoveryRepository
from vetinari.model_discovery_types import RepoModelFile, RepoModelSnapshot

logger = logging.getLogger(__name__)


_DOWNLOAD_LOCK = threading.Lock()
# Download jobs are written by ModelDiscovery.start_download worker threads,
# read by get_download_status(), and protected by _DOWNLOAD_LOCK.
_DOWNLOAD_JOBS: dict[str, dict[str, Any]] = {}
# Cancellation events are written by start_download(), read by worker threads,
# and protected by _DOWNLOAD_LOCK for the lifetime of a tracked download.
_DOWNLOAD_CANCEL_EVENTS: dict[str, Event] = {}
# Worker thread handles are stored here at thread.start() and removed on
# worker exit, so shutdown_model_downloads_for_test() can cancel and join
# every in-flight worker bounded. Protected by _DOWNLOAD_LOCK.
_DOWNLOAD_THREADS: dict[str, threading.Thread] = {}
_DOWNLOAD_STATE_FILENAME = "download_jobs.json"  # Per-cache persisted background-download state.
_ACTIVE_DOWNLOAD_STATES = {"started", "running", "canceling"}  # States converted to interrupted on restart.
_GGUF_STORE_MAX_GB = float(os.environ.get("VETINARI_GGUF_STORE_MAX_GB", "200"))


class _DownloadStateOwner(Protocol):
    download_state_path: Path


def _download_owner(obj: object) -> _DownloadStateOwner:
    return cast(_DownloadStateOwner, obj)


def shutdown_model_downloads_for_test(timeout: float = 5.0) -> None:
    """Cancel and bounded-join every tracked download worker thread.

    Used by ``tests/_root_conftest_harness.py`` between test sessions so
    download daemon threads do not survive into the next test's module
    state. Safe to call when no downloads are active.

    Args:
        timeout: Per-thread join budget, in seconds. Threads that have
            not exited within this budget are abandoned (they remain
            daemon threads, so they die with the process).
    """
    with _DOWNLOAD_LOCK:
        events = list(_DOWNLOAD_CANCEL_EVENTS.values())
        threads = list(_DOWNLOAD_THREADS.values())
    for event in events:
        event.set()
    for thread in threads:
        thread.join(timeout=timeout)
    with _DOWNLOAD_LOCK:
        _DOWNLOAD_CANCEL_EVENTS.clear()
        _DOWNLOAD_THREADS.clear()


def _enforce_gguf_store_cap(models_dir: Path, *, protect: Path | None = None) -> list[Path]:
    """Delete oldest GGUF files until the model store is under its disk cap."""
    max_bytes = int(max(_GGUF_STORE_MAX_GB, 0.0) * 1024**3)
    if max_bytes <= 0 or not models_dir.exists():
        return []
    protected = protect.resolve() if protect is not None else None
    files = sorted(
        (path for path in models_dir.rglob("*.gguf") if path.is_file()),
        key=lambda path: (path.stat().st_mtime, path.name),
    )
    total = sum(path.stat().st_size for path in files)
    removed: list[Path] = []
    for path in files:
        if total <= max_bytes:
            break
        if protected is not None and path.resolve() == protected:
            continue
        size = path.stat().st_size
        try:
            path.unlink()
        except OSError as exc:
            logger.warning("Failed to evict GGUF model file %s: %s", path, exc)
            continue
        total -= size
        removed.append(path)
    if removed:
        logger.info("Evicted %d GGUF model file(s) to enforce disk cap in %s", len(removed), models_dir)
    return removed


class _ModelDiscoveryDownloads(_ModelDiscoveryRepository):
    """Download behavior mixed into the public ModelDiscovery facade."""

    def _persist_job_locked(self, download_id: str) -> None:
        state = _DOWNLOAD_JOBS.get(download_id)
        if not state:
            return
        state_path = Path(str(state.get("_state_path") or _download_owner(self).download_state_path))
        persisted = _load_download_state(state_path)
        persisted[download_id] = _public_download_state(state)
        _write_download_state(state_path, persisted)

    def _persist_external_state(self, state: dict[str, Any]) -> None:
        download_id = str(state.get("download_id") or "")
        if not download_id:
            return
        state_path = Path(str(state.get("_state_path") or _download_owner(self).download_state_path))
        persisted = _load_download_state(state_path)
        persisted[download_id] = _public_download_state(state)
        _write_download_state(state_path, persisted)

    def _load_persisted_download_status(self, download_id: str) -> dict[str, Any] | None:
        download_state_path = _download_owner(self).download_state_path
        persisted = _load_download_state(download_state_path)
        state = persisted.get(download_id)
        if state is None:
            return None
        if state.get("status") in _ACTIVE_DOWNLOAD_STATES:
            state = dict(state)
            state["status"] = "interrupted"
            state["error"] = state.get("error") or "download process exited before completion"
            state["completed_at"] = state.get("completed_at") or datetime.now(timezone.utc).isoformat()
            persisted[download_id] = state
            _write_download_state(download_state_path, persisted)
        return dict(state)

    def _complete_download(
        self,
        repo_file: RepoModelFile,
        destination: Path,
        cancel_event: Event | None = None,
        job_id: str | None = None,
    ) -> dict[str, Any]:
        """Download, verify, and atomically publish one model artifact."""
        try:
            hf_hub_download = importlib.import_module("huggingface_hub").hf_hub_download
        except ImportError as exc:
            raise RuntimeError("huggingface_hub is not installed") from exc

        if cancel_event is not None and cancel_event.is_set():
            raise RuntimeError("download canceled before start")

        destination.parent.mkdir(parents=True, exist_ok=True)
        _ensure_free_space(destination.parent, repo_file.size)

        if destination.exists():
            digest = _validate_existing_download(destination, repo_file)
            _enforce_gguf_store_cap(destination.parent, protect=destination)
            return {
                "status": "completed",
                "repo_id": repo_file.repo_id,
                "filename": repo_file.filename,
                "revision": repo_file.revision,
                "backend": "llama_cpp",
                "format": "gguf",
                "artifact_type": "file",
                "path": str(destination),
                "sha256": digest,
                "bytes_downloaded": destination.stat().st_size,
                "download_id": job_id,
                "transfer_safeguard": "local-cache-hit-no-cross-border-transfer",
            }

        # GDPR Art. 46: log the external host before each cross-border model
        # fetch so the transfer is auditable and downstream policy can verify
        # the standard contractual clause / equivalent safeguard is in place.
        logger.info(
            "Downloading model %s/%s from external host huggingface.co — "
            "transfer governed by GDPR Art. 46 standard contractual clauses "
            "or equivalent safeguard",
            repo_file.repo_id,
            repo_file.filename,
        )

        with tempfile.TemporaryDirectory(prefix="vetinari_model_download_") as temp_root:
            local_path = Path(
                hf_hub_download(
                    repo_id=repo_file.repo_id,
                    filename=repo_file.filename,
                    local_dir=temp_root,
                    revision=repo_file.revision,
                    resume_download=False,
                    local_dir_use_symlinks=False,
                    token=False,
                    etag_timeout=min(10, MODEL_DISCOVERY_TIMEOUT),
                )
            )

            if cancel_event is not None and cancel_event.is_set():
                raise RuntimeError("download canceled before completion")
            if not local_path.exists():
                raise FileNotFoundError(f"download backend did not materialize {repo_file.filename!r}")

            _validate_model_header(local_path)
            digest = _sha256_file(local_path)
            if repo_file.sha256 and digest.lower() != repo_file.sha256.lower():
                raise ValueError(
                    f"downloaded model digest mismatch for {repo_file.filename}: "
                    f"expected {repo_file.sha256.lower()}, got {digest.lower()}"
                )

            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(local_path, destination)
            _write_download_marker(destination, repo_file, digest)
            _enforce_gguf_store_cap(destination.parent, protect=destination)

        return {
            "status": "completed",
            "repo_id": repo_file.repo_id,
            "filename": repo_file.filename,
            "revision": repo_file.revision,
            "backend": "llama_cpp",
            "format": "gguf",
            "artifact_type": "file",
            "path": str(destination),
            "sha256": digest,
            "bytes_downloaded": destination.stat().st_size,
            "download_id": job_id,
            "transfer_safeguard": "gdpr-art-46-scc-or-equivalent",
        }

    @staticmethod
    def _complete_snapshot_download(
        snapshot: RepoModelSnapshot,
        destination: Path,
        cancel_event: Event | None = None,
        job_id: str | None = None,
    ) -> dict[str, Any]:
        """Download, verify, and publish one native HF snapshot directory."""
        try:
            snapshot_download = importlib.import_module("huggingface_hub").snapshot_download
        except ImportError as exc:
            raise RuntimeError("huggingface_hub is not installed") from exc

        if cancel_event is not None and cancel_event.is_set():
            raise RuntimeError("download canceled before start")

        destination.parent.mkdir(parents=True, exist_ok=True)
        _ensure_free_space(destination.parent, snapshot.total_size)

        if destination.exists():
            files = _validate_existing_snapshot(destination, snapshot)
            return {
                "status": "completed",
                "repo_id": snapshot.repo_id,
                "revision": snapshot.revision,
                "backend": snapshot.backend,
                "format": snapshot.model_format,
                "artifact_type": "snapshot",
                "path": str(destination),
                "manifest_path": str(_snapshot_marker_path(destination)),
                "files": files,
                "bytes_total": snapshot.total_size,
                "bytes_downloaded": sum(int(file.get("size") or 0) for file in files),
                "download_id": job_id,
                "transfer_safeguard": "local-cache-hit-no-cross-border-transfer",
            }

        # GDPR Art. 46: log the external host before each cross-border snapshot
        # fetch so the transfer is auditable and downstream policy can verify
        # the standard contractual clause / equivalent safeguard is in place.
        logger.info(
            "Downloading snapshot %s revision %s from external host huggingface.co — "
            "transfer governed by GDPR Art. 46 standard contractual clauses "
            "or equivalent safeguard",
            snapshot.repo_id,
            snapshot.revision,
        )

        with tempfile.TemporaryDirectory(prefix="vetinari_native_model_download_") as temp_root:
            temp_destination = Path(temp_root) / "snapshot"
            snapshot_download(
                repo_id=snapshot.repo_id,
                revision=snapshot.revision,
                local_dir=str(temp_destination),
                local_dir_use_symlinks=False,
                allow_patterns=[file.filename for file in snapshot.files],
                token=False,
                etag_timeout=min(10, MODEL_DISCOVERY_TIMEOUT),
            )

            if cancel_event is not None and cancel_event.is_set():
                raise RuntimeError("download canceled before completion")
            files = _materialized_snapshot_files(temp_destination, snapshot)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(temp_destination), str(destination))
            _write_snapshot_marker(destination, snapshot, files)

        return {
            "status": "completed",
            "repo_id": snapshot.repo_id,
            "revision": snapshot.revision,
            "backend": snapshot.backend,
            "format": snapshot.model_format,
            "artifact_type": "snapshot",
            "path": str(destination),
            "manifest_path": str(_snapshot_marker_path(destination)),
            "files": files,
            "bytes_total": snapshot.total_size,
            "bytes_downloaded": sum(int(file.get("size") or 0) for file in files),
            "download_id": job_id,
            "transfer_safeguard": "gdpr-art-46-scc-or-equivalent",
        }

    def download_model(
        self,
        repo_id: str,
        filename: str | None = None,
        models_dir: str | Path | None = None,
        revision: str | None = None,
        *,
        backend: str = "llama_cpp",
        model_format: str | None = None,
    ) -> dict[str, Any]:
        """Synchronously download a model with integrity and provenance checks.

        Args:
            repo_id: Repo id value consumed by download_model().
            filename: File path or file-like value consumed by the operation.
            models_dir: Models dir value consumed by download_model().
            revision: Revision value consumed by download_model().
            backend: Backend value consumed by download_model().
            model_format: Model format value consumed by download_model().

        Returns:
            Value produced for the caller.

        Raises:
            ValueError: Propagated when validation, persistence, or execution fails.
        """
        backend = _normalize_backend(backend)
        model_format = _normalize_model_format(backend, model_format)
        if backend in _NATIVE_DOWNLOAD_BACKENDS:
            root = Path(models_dir or DEFAULT_NATIVE_MODELS_DIR)
            root.mkdir(parents=True, exist_ok=True)
            snapshot = self._resolve_repo_snapshot(
                repo_id,
                backend=backend,
                model_format=model_format,
                revision=revision,
            )
            destination = _resolve_snapshot_destination(root, snapshot)
            return self._complete_snapshot_download(snapshot, destination)

        if not filename:
            raise ValueError("filename is required for GGUF downloads")
        root = Path(models_dir or OPERATOR_MODELS_CACHE_DIR)
        root.mkdir(parents=True, exist_ok=True)
        repo_file = self._resolve_repo_file(repo_id, filename, revision=revision)
        destination = _resolve_destination(root, repo_file.filename)
        return self._complete_download(repo_file, destination)

    def start_download(
        self,
        repo_id: str,
        filename: str | None = None,
        models_dir: str | Path | None = None,
        revision: str | None = None,
        *,
        backend: str = "llama_cpp",
        model_format: str | None = None,
    ) -> dict[str, Any]:
        """Start a tracked background model download or return a completed hit.

        Args:
            repo_id: Repo id value consumed by start_download().
            filename: File path or file-like value consumed by the operation.
            models_dir: Models dir value consumed by start_download().
            revision: Revision value consumed by start_download().
            backend: Backend value consumed by start_download().
            model_format: Model format value consumed by start_download().

        Returns:
            Value produced for the caller.
        """
        backend = _normalize_backend(backend)
        model_format = _normalize_model_format(backend, model_format)
        if backend in _NATIVE_DOWNLOAD_BACKENDS:
            return self._start_snapshot_download(
                repo_id,
                models_dir=models_dir,
                revision=revision,
                backend=backend,
                model_format=model_format,
            )
        return self._start_gguf_download(repo_id, filename, models_dir=models_dir, revision=revision)

    def _start_gguf_download(
        self,
        repo_id: str,
        filename: str | None,
        *,
        models_dir: str | Path | None = None,
        revision: str | None = None,
    ) -> dict[str, Any]:
        if not filename:
            raise ValueError("filename is required for GGUF downloads")
        root = Path(models_dir or OPERATOR_MODELS_CACHE_DIR)
        root.mkdir(parents=True, exist_ok=True)
        repo_file = self._resolve_repo_file(repo_id, filename, revision=revision)
        destination = _resolve_destination(root, repo_file.filename)

        if destination.exists():
            return self._complete_download(repo_file, destination)

        destination.parent.mkdir(parents=True, exist_ok=True)
        _ensure_free_space(destination.parent, repo_file.size)
        return _start_gguf_download_job(
            self,  # type: ignore[arg-type]  # _ModelDiscoveryDownloads satisfies _GgufDownloadOwner at runtime via _download_owner cast
            repo_file,
            destination,
            download_jobs=_DOWNLOAD_JOBS,
            cancel_events=_DOWNLOAD_CANCEL_EVENTS,
            download_lock=_DOWNLOAD_LOCK,
            download_threads=_DOWNLOAD_THREADS,
        )

    def _start_snapshot_download(
        self,
        repo_id: str,
        *,
        models_dir: str | Path | None = None,
        revision: str | None = None,
        backend: str,
        model_format: str,
    ) -> dict[str, Any]:
        root = Path(models_dir or DEFAULT_NATIVE_MODELS_DIR)
        root.mkdir(parents=True, exist_ok=True)
        snapshot = self._resolve_repo_snapshot(repo_id, backend=backend, model_format=model_format, revision=revision)
        destination = _resolve_snapshot_destination(root, snapshot)

        if destination.exists():
            return self._complete_snapshot_download(snapshot, destination)

        destination.parent.mkdir(parents=True, exist_ok=True)
        _ensure_free_space(destination.parent, snapshot.total_size)
        job_id = uuid.uuid4().hex
        cancel_event = Event()
        state: dict[str, Any] = {
            "download_id": job_id,
            "status": "started",
            "repo_id": snapshot.repo_id,
            "revision": snapshot.revision,
            "backend": snapshot.backend,
            "format": snapshot.model_format,
            "artifact_type": "snapshot",
            "path": str(destination),
            "manifest_path": str(_snapshot_marker_path(destination)),
            "bytes_total": snapshot.total_size,
            "bytes_downloaded": 0,
            "file_count": len(snapshot.files),
            "error": None,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "completed_at": None,
            "_state_path": str(_download_owner(self).download_state_path),
        }

        with _DOWNLOAD_LOCK:
            _DOWNLOAD_JOBS[job_id] = dict(state)
            _DOWNLOAD_CANCEL_EVENTS[job_id] = cancel_event
            self._persist_job_locked(job_id)

        def _worker() -> None:
            with _DOWNLOAD_LOCK:
                _DOWNLOAD_JOBS[job_id]["status"] = "running"
                self._persist_job_locked(job_id)
            try:
                result = self._complete_snapshot_download(
                    snapshot,
                    destination,
                    cancel_event=cancel_event,
                    job_id=job_id,
                )
                with _DOWNLOAD_LOCK:
                    _DOWNLOAD_JOBS[job_id].update(result)
                    _DOWNLOAD_JOBS[job_id]["completed_at"] = datetime.now(timezone.utc).isoformat()
                    self._persist_job_locked(job_id)
            except Exception as exc:
                status = "canceled" if cancel_event.is_set() else "failed"
                with _DOWNLOAD_LOCK:
                    _DOWNLOAD_JOBS[job_id].update({
                        "status": status,
                        "error": str(exc),
                        "completed_at": datetime.now(timezone.utc).isoformat(),
                    })
                    self._persist_job_locked(job_id)
            finally:
                with _DOWNLOAD_LOCK:
                    _DOWNLOAD_CANCEL_EVENTS.pop(job_id, None)
                    _DOWNLOAD_THREADS.pop(job_id, None)

        worker_thread = threading.Thread(
            target=_worker,
            name=f"native-model-download-{job_id[:8]}",
            daemon=True,
        )
        with _DOWNLOAD_LOCK:
            _DOWNLOAD_THREADS[job_id] = worker_thread
        worker_thread.start()
        return cast(dict[str, Any], _public_download_state(state))

    def get_download_status(self, download_id: str) -> dict[str, Any] | None:
        """Return a bounded status object for a tracked download.

        Returns:
            Value produced for the caller.
        """
        with _DOWNLOAD_LOCK:
            status = _DOWNLOAD_JOBS.get(download_id)
            if status:
                return cast(dict[str, Any], _public_download_state(status))
        return self._load_persisted_download_status(download_id)

    def cancel_download(self, download_id: str) -> bool:
        """Request cancellation of a running tracked download.

        Returns:
            Value produced for the caller.
        """
        with _DOWNLOAD_LOCK:
            event = _DOWNLOAD_CANCEL_EVENTS.get(download_id)
            if event is None:
                return False
            event.set()
            if download_id in _DOWNLOAD_JOBS:
                _DOWNLOAD_JOBS[download_id]["status"] = "canceling"
                self._persist_job_locked(download_id)
            return True


# ---------------------------------------------------------------------------
# Quantization (FSA-0049)
# ---------------------------------------------------------------------------

# Curated set of GGUF quantization formats accepted by llama.cpp.  Kept narrow
# so the validator rejects typos and unsupported formats before any backend is
# invoked.  Reference: llama.cpp/ggml type table (Q2_K through Q8_0, plus F16
# and F32 full-precision passthroughs).
SUPPORTED_QUANTIZATION_FORMATS: frozenset[str] = frozenset({
    "Q2_K",
    "Q3_K_S",
    "Q3_K_M",
    "Q3_K_L",
    "Q4_0",
    "Q4_1",
    "Q4_K_S",
    "Q4_K_M",
    "Q5_0",
    "Q5_1",
    "Q5_K_S",
    "Q5_K_M",
    "Q6_K",
    "Q8_0",
    "F16",
    "F32",
})


def quantize_model(source: Path, quantization_format: str) -> Path:
    """Validate a GGUF quantization format request before invoking a backend.

    The validation step is intentionally separated from the backend call so a
    typo (e.g. "Q4KM" instead of "Q4_K_M") fails fast with a meaningful
    error and without spinning up llama.cpp/ggml.  Once the format passes
    this gate, callers can dispatch to the appropriate quantizer binary.

    Args:
        source: Path to the source GGUF file that should be quantized.
        quantization_format: Target GGUF quantization id (e.g. ``"Q4_K_M"``).
            Case-sensitive.  Members of
            :data:`SUPPORTED_QUANTIZATION_FORMATS` are accepted; anything
            else fails closed.

    Returns:
        The path the quantized artifact would be written to, derived from
        the source by inserting the format id before the ``.gguf`` suffix
        (e.g. ``model.gguf`` + ``Q4_K_M`` -> ``model.Q4_K_M.gguf``).
        Returning the projected path (without invoking the backend) lets
        downstream code stage atomic temp-file writes the same way as the
        rest of the download lifecycle.

    Raises:
        ValueError: If ``quantization_format`` is not in
            :data:`SUPPORTED_QUANTIZATION_FORMATS`.
        FileNotFoundError: If ``source`` does not exist.
    """
    if quantization_format not in SUPPORTED_QUANTIZATION_FORMATS:
        supported_sorted = ", ".join(sorted(SUPPORTED_QUANTIZATION_FORMATS))
        raise ValueError(
            f"Unsupported quantization format {quantization_format!r}; expected one of: {supported_sorted}"
        )
    source_path = Path(source)
    if not source_path.is_file():
        raise FileNotFoundError(f"Source GGUF file not found: {source_path}")
    # Insert the format id before the .gguf suffix so the output path is
    # discoverable by the same store-scan/eviction logic as plain GGUF files.
    suffix = source_path.suffix or ".gguf"
    stem = source_path.stem
    return source_path.with_name(f"{stem}.{quantization_format}{suffix}")
