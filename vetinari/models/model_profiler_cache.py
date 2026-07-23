"""Persistence and artifact identity helpers for model profiling."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, cast

from vetinari.constants import get_user_dir

logger = logging.getLogger(__name__)

_ARTIFACT_HASH_CACHE: dict[tuple[str, int, int], str] = {}
_ARTIFACT_HASH_LOCK = threading.Lock()


def _get_config_dir() -> Path:
    """Return the model configs directory, re-reading the env on each call.

    Using ``get_user_dir()`` instead of a module-level cached constant ensures
    that test overrides via ``monkeypatch.setenv("VETINARI_USER_DIR", ...)``
    take effect without restarting the process.

    Returns:
        Path to the per-user model configs directory.
    """
    return cast(Path, get_user_dir() / "model_configs")


def _config_path(model_id: str) -> Path:
    """Return the per-model config file path.

    Args:
        model_id: Model identifier (used as filename stem).

    Returns:
        Path to ``~/.vetinari/model_configs/{model_id}.json``.
    """
    safe_id = re.sub(r"[^\w\-.]", "_", model_id)
    return _get_config_dir() / f"{safe_id}.json"


def compute_artifact_sha256(model_path: Path) -> str:
    """Return a memoized SHA-256 digest for a model artifact.

    Returns:
        Hex-encoded SHA-256 digest of the resolved model artifact.
    """
    resolved = model_path.resolve()
    stat = resolved.stat()
    cache_key = (str(resolved), stat.st_size, stat.st_mtime_ns)
    with _ARTIFACT_HASH_LOCK:
        cached = _ARTIFACT_HASH_CACHE.get(cache_key)
        if cached is not None:
            return cached

    digest = hashlib.sha256()
    with resolved.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    value = digest.hexdigest()

    with _ARTIFACT_HASH_LOCK:
        _ARTIFACT_HASH_CACHE[cache_key] = value
    return value


def build_model_artifact_identity(model_path: Path) -> dict[str, Any]:
    """Return identity fields for a local model artifact.

    Returns:
        Stable path, digest, size, and mtime fields for profile cache keys.
    """
    resolved = model_path.resolve()
    stat = resolved.stat()
    digest = compute_artifact_sha256(resolved)
    return {
        "artifact_path": str(resolved),
        "artifact_sha256": digest,
        "artifact_size_bytes": stat.st_size,
        "artifact_mtime_ns": stat.st_mtime_ns,
        "path": str(resolved),
        "digest": digest,
        "size": stat.st_size,
        "mtime": stat.st_mtime_ns,
    }


def model_profile_cache_id(model_path: Path, artifact_sha256: str | None = None) -> str:
    """Return a collision-resistant cache ID for a GGUF model profile.

    Args:
        model_path: Local model artifact path.
        artifact_sha256: Optional precomputed artifact digest.

    Returns:
        Filename-safe profile cache identifier.
    """
    digest = artifact_sha256 or compute_artifact_sha256(model_path)
    return f"{model_path.stem}-{digest[:16]}"


def _load_cached_profile(model_id: str) -> dict[str, Any] | None:
    """Load a cached profile from disk if it exists.

    Args:
        model_id: Model identifier.

    Returns:
        Raw profile dict, or None if not cached.
    """
    path = _config_path(model_id)
    if not path.exists():
        return None
    try:
        with path.open(encoding="utf-8") as f:
            return cast(dict[str, Any], json.load(f))
    except json.JSONDecodeError as exc:
        corrupt_path = path.with_suffix(path.suffix + f".corrupt.{int(time.time())}")
        try:
            path.replace(corrupt_path)
            logger.warning(
                "Corrupt cached profile for %s moved to %s before re-profile: %s",
                model_id,
                corrupt_path,
                exc,
            )
        except OSError as move_exc:
            logger.warning(
                "Corrupt cached profile for %s could not be moved aside; refusing silent overwrite: %s",
                model_id,
                move_exc,
            )
        return None
    except OSError as exc:
        logger.warning("Failed to load cached profile for %s — will re-profile: %s", model_id, exc)
        return None


def _save_profile(model_id: str, profile_data: dict[str, Any]) -> None:
    """Persist a profile to disk.

    Args:
        model_id: Model identifier.
        profile_data: Serialized profile dict.
    """
    path = _config_path(model_id)
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tmp_path.open("w", encoding="utf-8") as f:
            json.dump(profile_data, f, indent=2, default=str)
            f.write("\n")
        tmp_path.replace(path)
        logger.debug("Saved model profile for %s to %s", model_id, path)
    except OSError as exc:
        logger.warning("Failed to save model profile for %s — results not cached: %s", model_id, exc)
    finally:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            logger.warning("Could not remove temporary profile file %s", tmp_path, exc_info=True)


# ── GGUF metadata reader ──────────────────────────────────────────────────────
