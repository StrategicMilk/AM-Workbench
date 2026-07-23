"""Durable local model scanning with artifact provenance."""

from __future__ import annotations

import hashlib
import json
import os
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from vetinari.constants import OUTPUTS_DIR
from vetinari.security.redaction import REDACTED_PATH, redact_text
from vetinari.utils.bounded_collections import BoundedDict, bounded_rglob


class ModelFormat(str, Enum):
    """Supported installed model artifact formats."""

    GGUF = "GGUF"
    HF = "HF"
    ONNX = "ONNX"


@dataclass(frozen=True, slots=True)
class RuntimeRequirements:
    """Minimum runtime requirements discovered for a model artifact."""

    cuda_capability: str | None = None
    min_vram_mb: int = 0


@dataclass(frozen=True, slots=True)
class ModelRecord:
    """Durable scan record for an installed model artifact."""

    model_id: str
    path: str
    format: ModelFormat
    size_bytes: int
    sha256: str
    source_url: str | None
    registered_at_utc: str
    runtime_requirements: RuntimeRequirements

    def __repr__(self) -> str:
        """Return a compact debug representation keyed by artifact identity."""
        return (
            "ModelRecord("
            f"model_id={self.model_id!r}, format={self.format.value!r}, "
            f"size_bytes={self.size_bytes!r}, sha256={self.sha256[:12]!r})"
        )

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible representation.

        Returns:
            Dictionary suitable for scan JSONL persistence.
        """
        data = asdict(self)
        data["format"] = self.format.value
        return data


class ModelScanError(RuntimeError):
    """Raised when a configured model search path cannot be scanned."""


# Cache of sha256 digests keyed by (resolved_path, mtime_ns, size_bytes).
# Writer: _sha256() helper on cache miss.
# Reader: _sha256() helper on cache hit.
# Lifecycle: process-wide singleton, bounded to prevent hostile or accidental
#       deep scans from growing memory without limit.
# Lock: _SHA256_CACHE_LOCK guards all reads and writes to prevent race conditions
#       if scan() is ever called from concurrent threads.
_SHA256_CACHE_LOCK = threading.Lock()
_SHA256_CACHE_MAXSIZE = 10_000
_SHA256_CACHE: BoundedDict[tuple[str, int, int], str] = BoundedDict(_SHA256_CACHE_MAXSIZE)
_MODEL_SCAN_MAX_DEPTH = 8
_MODEL_SCAN_MAX_FILES = 10_000

# Map lowercase file extensions to ModelFormat for artifact discovery.
_MODEL_EXTENSIONS: dict[str, ModelFormat] = {
    ".gguf": ModelFormat.GGUF,
    ".onnx": ModelFormat.ONNX,
    ".safetensors": ModelFormat.HF,
    ".bin": ModelFormat.HF,
}


def configured_model_paths() -> list[Path]:
    """Return configured model paths.

    ``VETINARI_MODELS_DIR`` is authoritative when set.  When it is absent, a
    local ``models`` directory is scanned only if it exists; otherwise the clean
    scan surface is empty rather than treating an unconfigured directory as a
    missing prerequisite.

    Returns:
        Model search roots to scan.
    """
    env_value = os.environ.get("VETINARI_MODELS_DIR", "").strip()
    if env_value:
        return [Path(part).expanduser() for part in env_value.split(os.pathsep) if part.strip()]
    default = Path("models")
    return [default] if default.exists() else []


def scan(search_paths: list[Path]) -> list[ModelRecord]:
    """Scan local model paths and return durable provenance records.

    Args:
        search_paths: Files or directories to inspect for model artifacts.

    Returns:
        Sorted model records for discovered artifacts.

    Raises:
        ModelScanError: If a configured search path does not exist.
    """
    records: list[ModelRecord] = []
    for search_path in search_paths:
        root = Path(search_path).expanduser()
        if not root.exists():
            raise ModelScanError(f"configured model path is inaccessible: {root}")
        if root.is_file():
            record = _record_for_file(root)
            if record is not None:
                records.append(record)
            continue
        for path in sorted(bounded_rglob(root, "*", max_depth=_MODEL_SCAN_MAX_DEPTH, max_files=_MODEL_SCAN_MAX_FILES)):
            if path.is_file():
                record = _record_for_file(path)
                if record is not None:
                    records.append(record)
            elif _is_hf_checkpoint_dir(path):
                records.append(_record_for_hf_dir(path))
    return sorted(records, key=lambda record: (record.model_id, record.path))


def write_scan_jsonl(records: list[ModelRecord], output_path: Path | None = None) -> Path:
    """Persist scan records as JSONL via temp-file replace.

    Args:
        records: Records to write.
        output_path: Optional destination override.

    Returns:
        Path written.
    """
    path = output_path or OUTPUTS_DIR / "models" / "scan.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    lines = [json.dumps(record.to_dict(), sort_keys=True) for record in records]
    payload = "\n".join(lines) + ("\n" if lines else "")
    tmp_path.write_text(payload, encoding="utf-8")
    tmp_path.replace(path)
    return path


def _record_for_file(path: Path) -> ModelRecord | None:
    model_format = _MODEL_EXTENSIONS.get(path.suffix.lower())
    if model_format is None:
        return None
    stat = path.stat()
    return ModelRecord(
        model_id=path.stem,
        path=_path_receipt(path),
        format=model_format,
        size_bytes=stat.st_size,
        sha256=_sha256(path, stat.st_mtime_ns, stat.st_size),
        source_url=_source_url_receipt(_read_source_url(path)),
        registered_at_utc=_registered_at(stat.st_mtime),
        runtime_requirements=RuntimeRequirements(),
    )


def _record_for_hf_dir(path: Path) -> ModelRecord:
    stat = path.stat()
    files = sorted(
        child
        for child in bounded_rglob(path, "*", max_depth=_MODEL_SCAN_MAX_DEPTH, max_files=_MODEL_SCAN_MAX_FILES)
        if child.is_file()
    )
    digest = hashlib.sha256()
    size_bytes = 0
    for child in files:
        rel = child.relative_to(path).as_posix().encode("utf-8")
        digest.update(rel)
        child_stat = child.stat()
        digest.update(_sha256(child, child_stat.st_mtime_ns, child_stat.st_size).encode("ascii"))
        size_bytes += child_stat.st_size
    return ModelRecord(
        model_id=path.name,
        path=_path_receipt(path),
        format=ModelFormat.HF,
        size_bytes=size_bytes,
        sha256=digest.hexdigest(),
        source_url=_source_url_receipt(_read_source_url(path)),
        registered_at_utc=_registered_at(stat.st_mtime),
        runtime_requirements=RuntimeRequirements(),
    )


def _sha256(path: Path, mtime_ns: int, size_bytes: int) -> str:
    key = (str(path.resolve()), mtime_ns, size_bytes)
    with _SHA256_CACHE_LOCK:
        cached = _SHA256_CACHE.get(key)
        if cached:
            return cached
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    value = digest.hexdigest()
    with _SHA256_CACHE_LOCK:
        _SHA256_CACHE[key] = value
    return value


def _is_hf_checkpoint_dir(path: Path) -> bool:
    return (path / "config.json").is_file() and any(
        child.suffix.lower() in {".safetensors", ".bin"} for child in path.iterdir() if child.is_file()
    )


def _read_source_url(path: Path) -> str | None:
    candidates = [
        path.with_suffix(path.suffix + ".source_url") if path.is_file() else path / "source_url.txt",
        path.with_suffix(path.suffix + ".source-url") if path.is_file() else path / "source-url.txt",
    ]
    for candidate in candidates:
        if candidate.is_file():
            value = candidate.read_text(encoding="utf-8").strip()
            return value or None
    return None


def _path_receipt(path: Path) -> str:
    resolved = str(path.resolve())
    digest = hashlib.sha256(resolved.encode("utf-8", errors="replace")).hexdigest()[:12]
    redacted = redact_text(resolved)
    if redacted == resolved:
        redacted = f"{REDACTED_PATH}/{path.name}"
    return f"{redacted}#path_sha256:{digest}"


def _source_url_receipt(source_url: str | None) -> str | None:
    if source_url is None:
        return None
    digest = hashlib.sha256(source_url.encode("utf-8", errors="replace")).hexdigest()[:12]
    return f"[REDACTED_URL]#url_sha256:{digest}#len:{len(source_url)}"


def _registered_at(mtime: float) -> str:
    return datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()
