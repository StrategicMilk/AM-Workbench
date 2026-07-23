"""Atomic GGUF and adapter artifact deployment helpers."""

from __future__ import annotations

import hashlib
import os
import shutil
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path


class AtomicArtifactCopyError(ValueError):
    """Raised when an artifact cannot be safely promoted."""


@dataclass(frozen=True, slots=True)
class AtomicCopyResult:
    """Result of an atomic artifact deployment."""

    destination: Path
    bytes_written: int
    sha256: str


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def copy_artifact_atomic(
    source: str | Path,
    destination: str | Path,
    *,
    expected_sha256: str | None = None,
    min_bytes: int = 1,
) -> AtomicCopyResult:
    """Copy an artifact through a temp file and atomically replace the destination.

    Args:
        source: Existing artifact path to copy from.
        destination: Final artifact path to replace atomically.
        expected_sha256: Optional digest that the copied bytes must match.
        min_bytes: Minimum accepted source size.

    Returns:
        The destination, byte count, and copied artifact digest.

    Raises:
        AtomicArtifactCopyError: if the source is missing, too small, partially
            copied, or digest validation fails.
        ValueError: if ``min_bytes`` is not positive.
    """
    src = Path(source)
    dst = Path(destination)
    if min_bytes < 1:
        raise ValueError("min_bytes must be positive")
    if not src.is_file():
        raise AtomicArtifactCopyError(f"source artifact missing: {src}")
    size = src.stat().st_size
    if size < min_bytes:
        raise AtomicArtifactCopyError(f"source artifact below minimum size: {size} < {min_bytes}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(f".{dst.name}.tmp")
    try:
        with src.open("rb") as read_handle, tmp.open("wb") as write_handle:
            shutil.copyfileobj(read_handle, write_handle, length=1024 * 1024)
            write_handle.flush()
            os.fsync(write_handle.fileno())
        copied_size = tmp.stat().st_size
        if copied_size != size:
            raise AtomicArtifactCopyError(f"partial artifact copy: {copied_size} != {size}")
        digest = _sha256(tmp)
        if expected_sha256 is not None and digest.lower() != expected_sha256.lower():
            raise AtomicArtifactCopyError("artifact digest mismatch")
        os.replace(tmp, dst)
        return AtomicCopyResult(destination=dst, bytes_written=copied_size, sha256=digest)
    except Exception:
        with suppress(FileNotFoundError):
            tmp.unlink()
        raise


__all__ = ["AtomicArtifactCopyError", "AtomicCopyResult", "copy_artifact_atomic"]
