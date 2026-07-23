"""Model artifact validation and provenance helpers for discovery downloads.

This module owns Hugging Face filename validation, artifact filtering, marker
files, digest checks, and local snapshot materialization safety checks.
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from vetinari.learning.atomic_writers import write_json_atomic
from vetinari.model_discovery_types import RepoModelFile, RepoModelSnapshot, RepoSnapshotFile

logger = logging.getLogger(__name__)


_GGUF_DOWNLOAD_SUFFIXES = {".gguf"}  # Managed llama.cpp downloads are GGUF-only.
_NATIVE_DOWNLOAD_FORMATS = {"safetensors", "awq", "gptq"}  # Native backend artifact families.
_NATIVE_DOWNLOAD_BACKENDS = {"vllm", "nim"}  # Backends that download full HF snapshots.
_NATIVE_METADATA_FILENAMES = {
    "added_tokens.json",
    "chat_template.jinja",
    "config.json",
    "generation_config.json",
    "merges.txt",
    "preprocessor_config.json",
    "special_tokens_map.json",
    "tokenizer.json",
    "tokenizer.model",
    "tokenizer_config.json",
    "vocab.json",
}
_DOWNLOAD_MARKER_SUFFIX = ".vetinari-download.json"  # Sidecar filename for provenance markers.
_MIN_FREE_SPACE_HEADROOM_BYTES = 512 * 1024 * 1024  # Extra free space required before downloads.
_MAX_REPO_FILES = 200  # Maximum repo artifacts exposed per discovery request.


def _validate_repo_id(repo_id: str) -> str:
    if not repo_id or not isinstance(repo_id, str):
        raise ValueError("repo_id is required")
    repo_id = repo_id.strip()
    parts = repo_id.split("/")
    if len(parts) != 2 or any(part in {"", ".", ".."} for part in parts):
        raise ValueError("repo_id must be a Hugging Face owner/repo identifier")
    if any("\\" in part or ".." in part for part in parts):
        raise ValueError("repo_id must be a Hugging Face owner/repo identifier")
    return repo_id


def _normalize_backend(backend: str | None) -> str:
    normalized = (backend or "llama_cpp").strip().lower().replace("-", "_")
    if normalized in {"local", "llama"}:
        return "llama_cpp"
    if normalized not in {"llama_cpp", *_NATIVE_DOWNLOAD_BACKENDS}:
        raise ValueError("backend must be one of: llama_cpp, vllm, nim")
    return normalized


def _normalize_model_format(backend: str, model_format: str | None) -> str:
    normalized = (model_format or ("gguf" if backend == "llama_cpp" else "safetensors")).strip().lower()
    normalized = normalized.removeprefix(".")
    if backend == "llama_cpp":
        if normalized != "gguf":
            raise ValueError("llama_cpp managed downloads only support GGUF")
        return "gguf"
    if normalized not in _NATIVE_DOWNLOAD_FORMATS:
        raise ValueError("native downloads support format=safetensors, awq, or gptq")
    return normalized


def _repo_storage_name(repo_id: str) -> str:
    return repo_id.replace("/", "--")


def _safe_hf_filename(filename: str) -> PurePosixPath:
    """Validate a Hugging Face repository filename for local materialization."""
    candidate = PurePosixPath(filename.strip())
    if not str(candidate) or candidate.is_absolute() or any(part in {"", ".", ".."} for part in candidate.parts):
        raise ValueError("filename must be a relative model file path")
    if "\\" in filename:
        raise ValueError("filename must use repository-relative '/' separators")
    if candidate.suffix.lower() not in _GGUF_DOWNLOAD_SUFFIXES:
        raise ValueError("only GGUF model files are supported for managed downloads")
    return candidate


def _safe_snapshot_relative(filename: str) -> PurePosixPath:
    candidate = PurePosixPath(filename.strip())
    if not str(candidate) or candidate.is_absolute() or any(part in {"", ".", ".."} for part in candidate.parts):
        raise ValueError("snapshot file must be a relative repository path")
    if "\\" in filename:
        raise ValueError("snapshot file must use repository-relative '/' separators")
    return candidate


def _resolve_destination(models_dir: Path, filename: str) -> Path:
    relative = _safe_hf_filename(filename)
    root = models_dir.resolve()
    destination = root.joinpath(*relative.parts).resolve()
    if not destination.is_relative_to(root):
        raise ValueError("download destination escapes the models directory")
    return destination


def _resolve_snapshot_destination(native_root: Path, snapshot: RepoModelSnapshot) -> Path:
    root = native_root.resolve()
    destination = (
        root / snapshot.backend / snapshot.model_format / _repo_storage_name(snapshot.repo_id) / snapshot.revision
    ).resolve()
    if not destination.is_relative_to(root):
        raise ValueError("snapshot destination escapes the native models directory")
    return destination


def _marker_path(model_path: Path) -> Path:
    return model_path.with_name(f"{model_path.name}{_DOWNLOAD_MARKER_SUFFIX}")


def _snapshot_marker_path(snapshot_dir: Path) -> Path:
    return snapshot_dir / _DOWNLOAD_MARKER_SUFFIX


def _sha256_file(file_path: Path) -> str:
    digest = hashlib.sha256()
    with file_path.open("rb") as fh:
        while True:
            chunk = fh.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _validate_model_header(file_path: Path) -> None:
    if file_path.suffix.lower() == ".gguf":
        with file_path.open("rb") as fh:
            if fh.read(4) != b"GGUF":
                raise ValueError(f"{file_path.name} is not a valid GGUF file")


def _file_size_gb(size_bytes: int | None) -> float | None:
    if size_bytes is None:
        return None
    return size_bytes / 1024**3


def _infer_file_quantization(name: str, model_format: str | None = None) -> str:
    if model_format in {"awq", "gptq"}:
        return model_format.upper()
    upper = PurePosixPath(name).name.upper()
    markers = (
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
        "BF16",
        "AWQ",
        "GPTQ",
    )
    for marker in markers:
        if marker in upper:
            return marker
    return "unknown"


def _infer_model_family(name: str) -> str:
    lowered = name.lower()
    families: tuple[tuple[str, str], ...] = (
        ("tinyllama", "tinyllama"),
        ("codellama", "codellama"),
        ("codestral", "codestral"),
        ("starcoder", "starcoder"),
        ("deepseek", "deepseek"),
        ("mixtral", "mixtral"),
        ("mistral", "mistral"),
        ("llama", "llama"),
        ("qwen", "qwen"),
        ("gemma", "gemma"),
        ("phi", "phi"),
        ("falcon", "falcon"),
        ("mpt", "mpt"),
    )
    for keyword, family in families:
        if keyword in lowered:
            return family
    return "unknown"


def _matches_objective(name: str, objective: str | None) -> bool:
    if not objective:
        return True
    normalized = objective.strip().lower()
    if not normalized or normalized == "general":
        return True
    keywords: dict[str, tuple[str, ...]] = {
        "coding": ("code", "coder", "codestral", "starcoder", "deepseek"),
        "code": ("code", "coder", "codestral", "starcoder", "deepseek"),
        "chat": ("chat", "instruct", "assistant"),
        "instruction": ("instruct", "chat"),
        "reasoning": ("reason", "math", "r1", "qwq"),
        "math": ("math", "reason", "qwq"),
        "vision": ("vision", "vl", "vqa"),
        "embeddings": ("embed", "embedding", "e5", "bge"),
    }
    haystack = name.lower()
    return any(keyword in haystack for keyword in keywords.get(normalized, (normalized,)))


def _matches_artifact_filters(
    *,
    name: str,
    size: int | None,
    model_format: str | None = None,
    objective: str | None = None,
    family: str | None = None,
    quantization: str | None = None,
    file_type: str | None = None,
    min_size_gb: float | None = None,
    max_size_gb: float | None = None,
) -> bool:
    if objective and not _matches_objective(name, objective):
        return False
    if family and family.strip().lower() not in {"", "any"} and _infer_model_family(name) != family.strip().lower():
        return False
    if quantization and quantization.strip().lower() not in {"", "any"}:
        expected = quantization.strip().upper()
        inferred = _infer_file_quantization(name, model_format=model_format).upper()
        if expected not in inferred and expected not in PurePosixPath(name).name.upper():
            return False
    if file_type and file_type.strip().lower() not in {"", "any"}:
        expected_type = file_type.strip().lower().lstrip(".")
        suffix = PurePosixPath(name).suffix.lower().lstrip(".")
        expected_format = "" if model_format is None else model_format.lower()
        if expected_type != suffix and expected_type != expected_format:
            return False
    size_gb = _file_size_gb(size)
    if min_size_gb is not None and (size_gb is None or size_gb < min_size_gb):
        return False
    return not (max_size_gb is not None and (size_gb is None or size_gb > max_size_gb))


def _extract_lfs_metadata(sibling: Any) -> tuple[int | None, str | None]:
    size = getattr(sibling, "size", None)
    sha256 = None
    lfs = getattr(sibling, "lfs", None)
    if isinstance(lfs, dict):
        size = lfs.get("size", size)
        sha256 = lfs.get("sha256")
    elif lfs is not None:
        size = getattr(lfs, "size", size)
        sha256 = getattr(lfs, "sha256", None)
    return size, sha256


def _is_native_snapshot_file(filename: str) -> bool:
    relative = _safe_snapshot_relative(filename)
    name = relative.name.lower()
    full_name = str(relative).lower()
    if full_name.endswith((".safetensors", ".safetensors.index.json")):
        return True
    if name in _NATIVE_METADATA_FILENAMES:
        return True
    return name.startswith("tokenizer.") and name.rsplit(".", 1)[-1] in {"json", "model", "txt"}


def _select_native_snapshot_files(
    siblings: list[Any],
    *,
    repo_id: str,
    model_format: str,
    objective: str | None = None,
    family: str | None = None,
    quantization: str | None = None,
    file_type: str | None = None,
    min_size_gb: float | None = None,
    max_size_gb: float | None = None,
) -> tuple[RepoSnapshotFile, ...]:
    selected: list[RepoSnapshotFile] = []
    has_config = False
    has_weights = False
    identity_name = repo_id.split("/", 1)[-1]
    for sibling in siblings:
        name = getattr(sibling, "rfilename", "")
        if not isinstance(name, str) or not name:
            continue
        try:
            if not _is_native_snapshot_file(name):
                continue
        except ValueError:
            logger.warning("Skipping unsafe snapshot path from %s: %r", repo_id, name)
            continue
        size, sha256 = _extract_lfs_metadata(sibling)
        if PurePosixPath(name).name.lower() == "config.json":
            has_config = True
        if name.lower().endswith(".safetensors"):
            has_weights = True
        if not _matches_artifact_filters(
            name=f"{identity_name}/{name}",
            size=size,
            model_format=model_format,
            objective=objective,
            family=family,
            quantization=quantization,
            file_type=file_type,
            min_size_gb=min_size_gb,
            max_size_gb=max_size_gb,
        ):
            continue
        selected.append(RepoSnapshotFile(filename=str(_safe_snapshot_relative(name)), size=size, sha256=sha256))
        if len(selected) >= _MAX_REPO_FILES:
            break
    selected_names = {file.filename for file in selected}
    if "config.json" not in selected_names and has_config and not file_type:
        for sibling in siblings:
            if getattr(sibling, "rfilename", None) == "config.json":
                size, sha256 = _extract_lfs_metadata(sibling)
                selected.insert(0, RepoSnapshotFile(filename="config.json", size=size, sha256=sha256))
                break
    if not selected:
        raise FileNotFoundError(f"no supported native snapshot files found in Hugging Face repo {repo_id!r}")
    if not has_config:
        raise FileNotFoundError(f"native snapshot repo {repo_id!r} does not contain config.json")
    if not has_weights:
        raise FileNotFoundError(f"native snapshot repo {repo_id!r} does not contain safetensors weights")
    requested_type = file_type.strip().lower().lstrip(".") if isinstance(file_type, str) else ""
    if requested_type in {"", "safetensors", "awq", "gptq"} and not any(
        file.filename.lower().endswith(".safetensors") for file in selected
    ):
        raise FileNotFoundError(f"native snapshot repo {repo_id!r} has no matching safetensors weights")
    return tuple(selected[:_MAX_REPO_FILES])


def _write_download_marker(path: Path, repo_file: RepoModelFile, digest: str) -> None:
    marker = {
        **repo_file.to_dict(),
        "sha256": digest,
        "size": path.stat().st_size,
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }
    write_json_atomic(_marker_path(path), marker)


def _write_snapshot_marker(path: Path, snapshot: RepoModelSnapshot, files: list[dict[str, Any]]) -> None:
    marker = {
        **snapshot.to_dict(),
        "path": str(path),
        "files": files,
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }
    write_json_atomic(_snapshot_marker_path(path), marker)


def _read_download_marker(path: Path) -> dict[str, Any] | None:
    marker_path = _marker_path(path)
    if not marker_path.exists():
        return None
    try:
        data = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("Ignoring unreadable model download marker %s", marker_path, exc_info=True)
        return None
    return data if isinstance(data, dict) else None


def _read_snapshot_marker(path: Path) -> dict[str, Any] | None:
    marker_path = _snapshot_marker_path(path)
    if not marker_path.exists():
        return None
    try:
        data = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("Ignoring unreadable native snapshot marker %s", marker_path, exc_info=True)
        return None
    return data if isinstance(data, dict) else None


def _validate_existing_download(path: Path, repo_file: RepoModelFile, expected_sha256: str | None = None) -> str:
    _validate_model_header(path)
    digest = _sha256_file(path)
    expected = expected_sha256 or repo_file.sha256
    if expected and digest.lower() != expected.lower():
        raise ValueError(
            f"existing model digest mismatch for {path.name}: expected {expected.lower()}, got {digest.lower()}"
        )

    marker = _read_download_marker(path)
    if marker is None:
        if expected is None:
            raise ValueError(f"existing model {path.name} has no provenance marker or expected upstream digest")
        _write_download_marker(path, repo_file, digest)
        return digest

    if marker.get("repo_id") != repo_file.repo_id:
        raise ValueError(f"existing model {path.name} provenance repo does not match {repo_file.repo_id}")
    if marker.get("filename") != repo_file.filename:
        raise ValueError(f"existing model {path.name} provenance filename does not match {repo_file.filename}")
    if marker.get("revision") != repo_file.revision:
        raise ValueError(f"existing model {path.name} provenance revision does not match {repo_file.revision}")
    if marker.get("sha256") != digest:
        raise ValueError(f"existing model {path.name} has changed since its completion marker was written")
    return digest


def _materialized_snapshot_files(path: Path, snapshot: RepoModelSnapshot) -> list[dict[str, Any]]:
    root = path.resolve()
    files: list[dict[str, Any]] = []
    by_name = {file.filename: file for file in snapshot.files}
    for filename, expected in by_name.items():
        relative = _safe_snapshot_relative(filename)
        local_path = root.joinpath(*relative.parts).resolve()
        if not local_path.is_relative_to(root):
            raise ValueError(f"snapshot file {filename!r} escapes the snapshot directory")
        if not local_path.is_file():
            raise FileNotFoundError(f"snapshot file {filename!r} is missing from {path}")
        digest = _sha256_file(local_path)
        if expected.sha256 and digest.lower() != expected.sha256.lower():
            raise ValueError(
                f"snapshot file {filename!r} digest mismatch: expected {expected.sha256.lower()}, got {digest.lower()}"
            )
        files.append({
            "filename": filename,
            "size": local_path.stat().st_size,
            "sha256": digest,
            "expected_sha256": expected.sha256,
        })
    return files


def _validate_existing_snapshot(path: Path, snapshot: RepoModelSnapshot) -> list[dict[str, Any]]:
    marker = _read_snapshot_marker(path)
    if marker is None:
        raise ValueError(f"existing native model snapshot {path} has no provenance manifest")
    expected = {
        "repo_id": snapshot.repo_id,
        "revision": snapshot.revision,
        "backend": snapshot.backend,
        "format": snapshot.model_format,
    }
    for key, value in expected.items():
        if marker.get(key) != value:
            raise ValueError(f"existing native model snapshot {path} provenance {key} does not match {value}")
    return _materialized_snapshot_files(path, snapshot)


def _ensure_free_space(models_dir: Path, expected_size: int | None) -> None:
    if expected_size is None:
        return
    usage = shutil.disk_usage(models_dir)
    required = expected_size + _MIN_FREE_SPACE_HEADROOM_BYTES
    if usage.free < required:
        raise OSError(
            f"not enough free space for model download: need at least {required} bytes, have {usage.free} bytes"
        )
