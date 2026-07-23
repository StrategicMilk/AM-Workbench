"""Adapter discovery helpers."""

from __future__ import annotations

import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

from vetinari.security.fail_closed import sanitize_untrusted_text
from vetinari.utils.bounded_collections import bounded_rglob

logger = logging.getLogger(__name__)

_CACHE: list[str] | None = None
_CACHE_LOCK = threading.RLock()
_MODEL_FILE_SUFFIXES = {".gguf", ".safetensors", ".awq", ".gptq", ".bin"}
_PROJECT_CONFIG = Path(__file__).resolve().parents[2] / "vetinari.yaml"
_DISCOVERY_MAX_DEPTH = 8
_DISCOVERY_MAX_FILES = 10_000


def _parse_discovery_timestamp(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        timestamp = value
    elif isinstance(value, str) and value.strip():
        raw = value.strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        try:
            timestamp = datetime.fromisoformat(raw)
        except ValueError as exc:
            logger.warning("Invalid model discovery timestamp %r: %s", value, exc)
            return None
    else:
        return None
    if timestamp.tzinfo is None:
        return timestamp.replace(tzinfo=timezone.utc)
    return timestamp.astimezone(timezone.utc)


def _warn_if_discovery_stale(
    *,
    config_path: Path = _PROJECT_CONFIG,
    now: datetime | None = None,
) -> None:
    """Warn when local model discovery metadata is stale or missing."""
    try:
        config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        logger.warning("Model discovery staleness check could not read %s: %s", config_path, exc)
        return
    if not isinstance(config, dict):
        logger.warning("Model discovery staleness check found non-mapping config at %s", config_path)
        return

    threshold = config.get("staleness_alert_threshold_minutes")
    if not isinstance(threshold, int):
        return
    timestamp = _parse_discovery_timestamp(config.get("last_discovered_at"))
    if timestamp is None:
        logger.warning("Model discovery is stale: last_discovered_at is missing or unreadable")
        return

    current = now or datetime.now(timezone.utc)
    age_minutes = int((current.astimezone(timezone.utc) - timestamp).total_seconds() // 60)
    if age_minutes > threshold:
        logger.warning(
            "Model discovery is stale: last_discovered_at is %d minutes old, threshold is %d minutes",
            age_minutes,
            threshold,
        )


def _discover_model_ids() -> list[str]:
    """Discover local model IDs from configured model directories and env overrides."""
    discovered: set[str] = set()
    for raw_id in os.environ.get("VETINARI_MODEL_IDS", "").split(","):
        model_id = sanitize_untrusted_text(raw_id.strip(), max_length=200) if raw_id.strip() else ""
        if model_id:
            discovered.add(model_id)

    for models_dir in _candidate_model_dirs():
        if not models_dir.exists() or not models_dir.is_dir():
            continue
        for path in bounded_rglob(
            models_dir,
            "*",
            max_depth=_DISCOVERY_MAX_DEPTH,
            max_files=_DISCOVERY_MAX_FILES,
        ):
            if path.is_file() and path.suffix.lower() in _MODEL_FILE_SUFFIXES:
                discovered.add(sanitize_untrusted_text(path.stem, max_length=200))

    return sorted(discovered)


def _candidate_model_dirs() -> tuple[Path, ...]:
    candidates: list[Path] = []
    env_dir = os.environ.get("VETINARI_MODELS_DIR")
    if env_dir:
        candidates.append(Path(sanitize_untrusted_text(env_dir, max_length=1_000)).expanduser())

    try:
        from vetinari.backend_config import load_backend_runtime_config

        runtime_cfg = load_backend_runtime_config()
        local_inference = runtime_cfg.get("local_inference", {})
        if isinstance(local_inference, dict) and local_inference.get("models_dir"):
            candidates.append(Path(str(local_inference["models_dir"])).expanduser())
    except Exception:
        logger.warning("Runtime config model directory discovery failed", exc_info=True)

    try:
        from vetinari.constants import OPERATOR_MODELS_CACHE_DIR

        candidates.append(Path(OPERATOR_MODELS_CACHE_DIR).expanduser())
    except Exception:
        logger.warning("Operator model cache directory discovery failed", exc_info=True)

    unique: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate)
        if key not in seen:
            unique.append(candidate)
            seen.add(key)
    return tuple(unique)


def discover_models(*, force: bool = False) -> list[str]:
    """Discover available model ids.

    Args:
        force: Whether to bypass caches.

    Returns:
        Discovered model id list.
    """
    global _CACHE
    with _CACHE_LOCK:
        if not force and _CACHE is not None:
            return list(_CACHE)

        _warn_if_discovery_stale()
        discovered = _discover_model_ids()
        _CACHE = list(discovered)
        return list(_CACHE)


def reset_discovery_cache_for_test() -> None:
    """Clear cached model ids for isolated tests."""
    global _CACHE
    with _CACHE_LOCK:
        _CACHE = None


__all__ = ["discover_models", "reset_discovery_cache_for_test"]
