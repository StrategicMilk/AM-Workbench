"""Model discovery and download helpers for the first-run setup wizard."""

from __future__ import annotations

import logging
import os
import time
from importlib.util import find_spec
from pathlib import Path
from threading import Event

from rich.console import Console

from vetinari.constants import (
    DEFAULT_MODELS_DIR,
    DEFAULT_NATIVE_MODELS_DIR,
    OPERATOR_MODELS_CACHE_DIR,
)
from vetinari.setup.model_recommender import SetupModelRecommendation

logger = logging.getLogger(__name__)
console = Console()

_PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent.parent
DEFAULT_GGUF_MODELS_DIR: Path = Path(DEFAULT_MODELS_DIR)
DEFAULT_NATIVE_MODELS_PATH: Path = Path(DEFAULT_NATIVE_MODELS_DIR)
DOWNLOAD_GGUF_MODELS_DIR: Path = Path(OPERATOR_MODELS_CACHE_DIR)
DOWNLOAD_NATIVE_MODELS_DIR: Path = Path(
    os.environ.get("VETINARI_NATIVE_MODELS_DIR", str(DOWNLOAD_GGUF_MODELS_DIR / "native"))
)
_COMMON_MODEL_DIRS = [
    _PROJECT_ROOT / "models",
    _PROJECT_ROOT / "models" / "native",
    Path.home() / ".cache" / "huggingface",
    Path.home() / "models",
    Path.home() / ".local" / "share" / "vetinari" / "models",
]
_MODEL_SCAN_SUFFIXES = {".gguf", ".awq", ".safetensors", ".gptq"}
_MODEL_SCAN_MAX_FILES = 5000
_MODEL_SCAN_MAX_DEPTH = 8
_MODEL_SCAN_TIMEOUT_SECONDS = 5.0


def _scan_for_models(
    extra_dirs: list[Path] | None = None,
    *,
    max_files: int = _MODEL_SCAN_MAX_FILES,
    max_depth: int = _MODEL_SCAN_MAX_DEPTH,
    timeout_seconds: float = _MODEL_SCAN_TIMEOUT_SECONDS,
    cancel_event: Event | None = None,
) -> list[Path]:
    """Scan common directories for existing GGUF and AWQ model files.

    Args:
        extra_dirs: Additional directories to scan beyond the defaults.
        max_files: Maximum number of directory entries to inspect.
        max_depth: Maximum recursion depth under each scan root.
        timeout_seconds: Wall-clock scan budget in seconds.
        cancel_event: Optional event that stops scanning when set.

    Returns:
        Sorted list of discovered model file paths.
    """
    dirs_to_scan = [DEFAULT_GGUF_MODELS_DIR, DEFAULT_NATIVE_MODELS_PATH, *_COMMON_MODEL_DIRS]
    if extra_dirs:
        dirs_to_scan.extend(extra_dirs)

    # Also check VETINARI_MODELS_DIR env var
    env_dir = os.environ.get("VETINARI_MODELS_DIR", "")
    if env_dir:
        dirs_to_scan.append(Path(env_dir))

    found: list[Path] = []
    seen: set[Path] = set()
    scanned = 0
    started = time.monotonic()

    for scan_dir in dirs_to_scan:
        if cancel_event is not None and cancel_event.is_set():
            break
        if time.monotonic() - started > timeout_seconds:
            logger.warning("Model scan timed out after %.1fs", timeout_seconds)
            break
        if not scan_dir.is_dir():
            continue
        try:
            root = scan_dir.resolve()
            for dirpath, dirnames, filenames in os.walk(root):
                if cancel_event is not None and cancel_event.is_set():
                    break
                if time.monotonic() - started > timeout_seconds:
                    logger.warning("Model scan timed out after %.1fs", timeout_seconds)
                    break
                current = Path(dirpath)
                try:
                    depth = len(current.relative_to(root).parts)
                except ValueError:
                    logger.warning("Skipped model scan path outside root: %s", current)
                    continue
                if depth >= max_depth:
                    dirnames[:] = []
                else:
                    dirnames[:] = [name for name in dirnames if not name.startswith(".")]
                for filename in filenames:
                    scanned += 1
                    if scanned > max_files:
                        logger.warning("Model scan stopped after %d files", max_files)
                        found.sort(key=lambda p: p.name)
                        return found
                    model_path = current / filename
                    if model_path.suffix.lower() not in _MODEL_SCAN_SUFFIXES:
                        continue
                    resolved = model_path.resolve()
                    if resolved not in seen:
                        seen.add(resolved)
                        found.append(resolved)
        except PermissionError:
            logger.warning("Permission denied scanning %s — skipping directory in model search", scan_dir)

    found.sort(key=lambda p: p.name)
    return found


def _download_model(recommendation: SetupModelRecommendation) -> Path | None:
    """Download a recommended model via huggingface_hub.

    Args:
        recommendation: The model recommendation to download.

    Returns:
        Path to the downloaded file, or None on failure.
    """
    if find_spec("huggingface_hub") is None:
        logger.warning(
            "huggingface_hub not installed — model download unavailable; install with: pip install huggingface-hub"
        )
        console.print("      huggingface_hub not installed. Install with:")
        console.print("        pip install huggingface-hub")
        return None

    # vLLM removed 2026-06-09; only NIM uses native models dir.
    target_dir = DOWNLOAD_NATIVE_MODELS_DIR if recommendation.backend == "nim" else DOWNLOAD_GGUF_MODELS_DIR
    Path(target_dir).mkdir(parents=True, exist_ok=True)

    console.print(f"      Downloading {recommendation.name} ({recommendation.size_gb:.1f} GB)...")
    console.print(f"      From: {recommendation.repo_id}")

    try:
        from vetinari.model_discovery import ModelDiscovery

        downloaded = ModelDiscovery().download_model(
            repo_id=recommendation.repo_id,
            filename=recommendation.filename if recommendation.backend == "llama_cpp" else None,
            models_dir=target_dir,
            backend=recommendation.backend,
            model_format=recommendation.model_format,
        )
        path = Path(str(downloaded["path"]))
        console.print(f"      Saved to: {path}")
        console.print(f"      Revision: {downloaded.get('revision')}")
        return path
    except Exception as exc:
        logger.warning("Model download failed for %s: %s", recommendation.repo_id, exc)
        console.print(f"      Download error: {exc}")
        return None
