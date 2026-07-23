# SPDX-FileCopyrightText: 2024-2026 Vetinari Contributors
# SPDX-License-Identifier: Apache-2.0
"""Remote model discovery and download helpers for the packaging CLI."""

from __future__ import annotations

import importlib.util
import json
import logging
import sys
from pathlib import Path
from typing import Any

from vetinari.cli_packaging_data import _RICH_AVAILABLE, _console

logger = logging.getLogger(__name__)


def _write_line(text: object = "") -> None:
    """Write one CLI output line to standard output."""
    sys.stdout.write(f"{text}\n")


def _write_error(text: object = "") -> None:
    """Write one diagnostic CLI output line to standard error."""
    sys.stderr.write(f"{text}\n")


_NATIVE_BACKENDS = {"vllm", "nim"}


def _module_available(module_name: str) -> bool:
    """Return True when a module is importable or already loaded."""
    if sys.modules.get(module_name) is not None:
        return True
    try:
        return importlib.util.find_spec(module_name) is not None
    except (ImportError, ValueError) as exc:
        logger.debug("Module availability probe failed for %s: %s", module_name, exc)
        return False


def _infer_cli_backend(
    backend: str | None,
    *,
    filename: str | None = None,
    model_format: str | None = None,
    action: str | None = None,
) -> str:
    backend_value = "auto" if backend is None else backend
    normalized = backend_value.strip().lower().replace("-", "_")
    if normalized in {"llama", "local"}:
        return "llama_cpp"
    if normalized != "auto":
        return normalized

    requested_format = "" if model_format is None else model_format.strip().lower().lstrip(".")
    requested_filename = "" if filename is None else filename.strip().lower()
    if requested_format == "gguf" or requested_filename.endswith(".gguf"):
        return "llama_cpp"
    if action in {"remove", "info"}:
        return "llama_cpp"
    return "vllm"


# Rich progress classes are imported only when rich is available.
if _RICH_AVAILABLE:
    from rich.progress import (
        BarColumn,
        Progress,
        SpinnerColumn,
        TaskProgressColumn,
        TextColumn,
        TimeElapsedColumn,
    )


def _download_with_progress(
    hf_hub_download: Any,
    repo: str,
    filename: str,
    models_dir: Path,
    revision: str,
) -> Path:
    """Run ``hf_hub_download`` inside a rich progress bar when rich is available.

    The progress bar shows the filename being downloaded, elapsed time, and a
    spinner while the download runs (file size is not known in advance for GGUF
    files served by HuggingFace Hub's redirect layer).

    When rich is not installed the download runs silently and the caller's
    plain-text ``_write_line()`` messages provide the user feedback.

    Args:
        hf_hub_download: The callable from ``huggingface_hub`` that performs
            the actual download.
        repo: HuggingFace repository ID in ``owner/name`` format.
        filename: The GGUF filename within the repository.
        models_dir: Local directory in which to save the model.
        revision: Immutable revision or commit SHA to download from.

    Returns:
        ``Path`` to the downloaded file as returned by ``hf_hub_download``.
    """
    models_dir_text = str(models_dir)
    # GDPR Art. 46: log the external host before each cross-border CLI model
    # fetch so the transfer is auditable and downstream policy can verify the
    # standard contractual clause / equivalent safeguard is in place.
    logger.info(
        "Downloading model %s/%s from external host huggingface.co — "
        "transfer governed by GDPR Art. 46 standard contractual clauses "
        "or equivalent safeguard",
        repo,
        filename,
    )
    if _RICH_AVAILABLE and _console is not None:
        with Progress(
            SpinnerColumn(),
            TextColumn("[bold cyan]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            TimeElapsedColumn(),
            console=_console,
            transient=True,
        ) as progress:
            task_id = progress.add_task(f"Downloading {filename}", total=None)
            progress.update(task_id, description=f"Downloading {filename} from {repo}")
            local = hf_hub_download(
                repo_id=repo,
                filename=filename,
                local_dir=models_dir_text,
                resume_download=True,
                revision=revision,
            )
            progress.update(task_id, description=f"Completed {filename}", completed=1, total=1)
    else:
        _write_line(f"Downloading {filename} from {repo}...")
        local = hf_hub_download(
            repo_id=repo,
            filename=filename,
            local_dir=models_dir_text,
            resume_download=True,
            revision=revision,
        )

    return Path(local)


def _models_download(
    repo: str | None,
    filename: str | None,
    models_dir: Path,
    *,
    backend: str = "auto",
    model_format: str | None = None,
    revision: str | None = None,
) -> int:
    """Download a managed model artifact from HuggingFace Hub.

    Args:
        repo: HuggingFace repository ID in ``owner/name`` format.
        filename: The GGUF filename within the repository.
        models_dir: Local directory in which to save the model.
        backend: Target backend; native backends download HF snapshots.
        model_format: Model format such as ``gguf``, ``safetensors``, ``awq``, or ``gptq``.
        revision: Optional revision to resolve before download.

    Returns:
        0 on success, 1 on failure.
    """
    backend_normalized = _infer_cli_backend(backend, filename=filename, model_format=model_format, action="download")
    if not repo:
        _write_error("Error: --repo is required for download.")
        _write_error(
            "  GGUF example: vetinari models download --repo <recommended-gguf-repo> --filename <recommended-gguf-file>"
        )
        _write_error("  Native example: vetinari models download --repo <recommended-native-repo> --format safetensors")
        return 1
    if backend_normalized in {"llama_cpp", "local", "llama"} and not filename:
        _write_error("Error: --filename is required for llama.cpp GGUF downloads.")
        return 1

    if not _module_available("huggingface_hub"):
        _write_error("huggingface_hub is not installed; automatic model download is unavailable.")
        _write_error("Install with: python -m pip install huggingface_hub")
        logger.warning("huggingface_hub not installed — model download unavailable")
        return 1

    models_dir.mkdir(parents=True, exist_ok=True)
    try:
        from vetinari.model_discovery import ModelDiscovery

        result = ModelDiscovery().download_model(
            repo,
            filename,
            models_dir=models_dir,
            revision=revision,
            backend=backend_normalized,
            model_format=model_format,
        )
        local_path = Path(str(result["path"]))
        _write_line(f"Saved to: {local_path}")

        _write_line(f"  Backend : {result.get('backend', backend_normalized)}")
        default_format = model_format or ("safetensors" if backend_normalized in _NATIVE_BACKENDS else "gguf")
        _write_line(f"  Format  : {result.get('format', default_format)}")
        _write_line(f"  Revision: {result.get('revision')}")
        if result.get("sha256"):
            _write_line(f"  SHA-256 : {result.get('sha256')}")
        if result.get("manifest_path"):
            _write_line(f"  Manifest: {result.get('manifest_path')}")
        if result.get("file_count"):
            _write_line(f"  Files   : {result.get('file_count')}")

        return 0
    except Exception as exc:
        _write_error(f"Download failed: {exc}")
        _write_error("Check the repository name, filename, revision, and network access, then retry.")
        logger.warning("Model download from %s failed: %s", repo, exc)
        return 1


def _models_files(
    repo: str | None,
    *,
    backend: str = "auto",
    model_format: str | None = None,
    revision: str | None = None,
    vram_gb: int = 32,
    objective: str | None = None,
    family: str | None = None,
    quantization: str | None = None,
    file_type: str | None = None,
    min_size_gb: float | None = None,
    max_size_gb: float | None = None,
) -> int:
    """List downloadable Hugging Face repo artifacts with optional filters."""
    if not repo:
        _write_error("Error: --repo is required for files.")
        return 1
    backend_normalized = _infer_cli_backend(backend, model_format=model_format, action="files")
    try:
        from vetinari.model_discovery import ModelDiscovery

        files = ModelDiscovery().get_repo_files(
            repo,
            vram_gb=vram_gb,
            backend=backend_normalized,
            model_format=model_format,
            revision=revision,
            objective=objective,
            family=family,
            quantization=quantization,
            file_type=file_type,
            min_size_gb=min_size_gb,
            max_size_gb=max_size_gb,
        )
    except Exception as exc:
        _write_error(f"File listing failed: {exc}")
        _write_error("Check the repository name, revision, and network access, then retry.")
        logger.warning("Model file listing from %s failed: %s", repo, exc)
        return 1

    if not files:
        _write_line("No matching files found.")
        return 0

    header = f"{'Filename':<58} {'Type':<12} {'Size':>9} {'Quant':<10} {'Revision'}"
    _write_line(header)
    _write_line("-" * len(header))
    for item in files:
        size = item.get("size")
        size_text = f"{(int(size) / 1024**3):.2f}G" if isinstance(size, int) else "unknown"
        filename = str(item.get("filename", ""))
        type_text = str(item.get("file_type") or item.get("format") or "")
        quant_text = str(item.get("quantization") or "")
        revision_text = str(item.get("revision") or "")
        _write_line(f"{filename:<58} {type_text:<12} {size_text:>9} {quant_text:<10} {revision_text}")
    return 0


def _models_status(download_id: str | None) -> int:
    """Print persisted model download status."""
    if not download_id:
        _write_error("Error: --download-id is required for status.")
        return 1
    from vetinari.model_discovery import ModelDiscovery

    status = ModelDiscovery().get_download_status(download_id)
    if status is None:
        _write_error(f"No tracked download found for {download_id}")
        return 1
    _write_line(json.dumps(status, indent=2, sort_keys=True))
    return 0


def _verify_sha256(file_path: Path) -> None:
    """Compute and display SHA-256 hash for a downloaded model file.

    Uses chunked reading to handle large files without loading them
    into memory. Prints the hash for the user to verify against the
    repository's expected checksum.

    Args:
        file_path: Path to the downloaded file.
    """
    import hashlib

    sha256 = hashlib.sha256()
    with file_path.open("rb") as fh:
        while True:
            chunk = fh.read(1024 * 1024)  # 1 MB chunks avoid loading large models into memory
            if not chunk:
                break
            sha256.update(chunk)
    digest = sha256.hexdigest()
    _write_line(f"  SHA-256: {digest}")
    _write_line("  Verify this matches the expected checksum from the model repository.")


# ── models remove / info / recommend ──────────────────────────────────────────
