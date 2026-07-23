"""Model and backend resolution helpers for training execution.

This module owns the model-path, backend, and revision decisions used before a
training run starts. It keeps those lookup concerns separate from the pipeline
orchestrator that runs curation, training, deployment, and receipts.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from collections.abc import Mapping
from datetime import datetime, timezone
from importlib import import_module
from pathlib import Path
from typing import Any

from vetinari.constants import MODEL_DISCOVERY_TIMEOUT, OPERATOR_MODELS_CACHE_DIR

from .pipeline_core import TrainingRun

logger = logging.getLogger(__name__)

_MODELS_DIR = Path(OPERATOR_MODELS_CACHE_DIR)  # GGUF model cache root used as a native-root fallback.
_NATIVE_MODELS_DIR = Path(
    os.environ.get("VETINARI_NATIVE_MODELS_DIR", str(_MODELS_DIR / "native"))
)  # HuggingFace-format model root used when no patched pipeline root is present.
_TRAINING_NATIVE_BACKENDS = {"vllm", "nim"}  # Backends that consume native adapter artifacts.
_TRAINING_NATIVE_FORMATS = {"safetensors", "awq", "gptq"}  # Native artifact formats accepted by deployment.
_TRAINING_DEFAULT_FORMAT_BY_BACKEND = {  # Default artifact format for each supported deployment backend.
    "llama_cpp": "gguf",
    "vllm": "safetensors",
    "nim": "safetensors",
}
_DEFAULT_TRAINING_BASE_MODEL = "Qwen/Qwen2.5-Coder-7B-Instruct"  # Fallback remote model for auto mode.
_DEFAULT_TRAINING_BASE_MODEL_REVISION = "resolve-before-training"


def _normalize_training_backend(backend: str | None) -> str:
    """Normalize and validate a training deployment backend."""
    value = (backend or "vllm").strip().lower().replace("-", "_")
    if value in {"llamacpp", "llama"}:
        value = "llama_cpp"
    if value not in {"llama_cpp", *_TRAINING_NATIVE_BACKENDS}:
        raise ValueError("backend must be one of: llama_cpp, vllm, nim")
    return value


def _normalize_training_format(backend: str, model_format: str | None) -> str:
    """Normalize and validate the output model format for a backend."""
    value = (model_format or _TRAINING_DEFAULT_FORMAT_BY_BACKEND[backend]).strip().lower().lstrip(".")
    if backend == "llama_cpp":
        if value not in {"", "gguf"}:
            raise ValueError("backend=llama_cpp only supports format=gguf")
        return "gguf"
    if value not in _TRAINING_NATIVE_FORMATS:
        raise ValueError("backend=vllm|nim supports format=safetensors, awq, or gptq")
    return value


def _read_model_revision_manifest(path: Path) -> str | None:
    """Read a pinned model revision from nearby Vetinari model manifests."""
    manifest_names = (".vetinari-download.json", ".vetinari-training-manifest.json")
    roots = [path if path.is_dir() else path.parent]
    roots.extend(parent for parent in roots[0].parents[:3])
    for root in roots:
        for name in manifest_names:
            manifest_path = root / name
            if not manifest_path.exists():
                continue
            try:
                data = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                logger.warning("Could not read native model manifest %s", manifest_path, exc_info=True)
                continue
            for key in ("revision", "base_model_revision", "model_revision"):
                value = data.get(key)
                if isinstance(value, str) and value:
                    return value
    return None


def _native_model_roots() -> list[Path]:
    """Return configured native-model roots while honoring pipeline patch seams."""
    candidates: list[Path] = []

    def add(value: object) -> None:
        """Append a native model root candidate once."""
        if value in (None, ""):
            return
        if not isinstance(value, str | os.PathLike):
            return
        path = Path(value)
        if path not in candidates:
            candidates.append(path)

    active_module = sys.modules.get("vetinari.training.pipeline")
    if active_module is not None:
        add(getattr(active_module, "_NATIVE_MODELS_DIR", None))
        active_gguf_root = getattr(active_module, "_MODELS_DIR", None)
        if isinstance(active_gguf_root, str | os.PathLike) and active_gguf_root != "":
            add(Path(active_gguf_root).parent / "native")

    training_pkg = sys.modules.get("vetinari.training")
    package_module = getattr(training_pkg, "pipeline", None) if training_pkg is not None else None
    if package_module is not None and package_module is not active_module:
        add(getattr(package_module, "_NATIVE_MODELS_DIR", None))
        package_gguf_root = getattr(package_module, "_MODELS_DIR", None)
        if isinstance(package_gguf_root, str | os.PathLike) and package_gguf_root != "":
            add(Path(package_gguf_root).parent / "native")

    add(os.environ.get("VETINARI_NATIVE_MODELS_DIR"))
    add(_NATIVE_MODELS_DIR)

    if _MODELS_DIR not in (None, ""):
        add(Path(_MODELS_DIR).parent / "native")

    return candidates


def _resolve_base_model(base_model: str) -> str:
    """Resolve ``auto`` to a concrete local or remote base model identifier."""
    if base_model != "auto":
        return base_model

    resolved: str = ""

    for native_root in _native_model_roots():
        try:
            if not native_root.exists():
                continue
            native_model_dirs = [p.parent for p in native_root.rglob("config.json") if p.parent.is_dir()]
            if native_model_dirs:
                largest = max(
                    native_model_dirs,
                    key=lambda p: sum(f.stat().st_size for f in p.rglob("*") if f.is_file()),
                )
                resolved = str(largest)
                logger.info("_resolve_base_model: resolved 'auto' -> %s", resolved)
                return resolved
        except OSError as exc:
            logger.warning(
                "_resolve_base_model: could not scan native model dir %s - %s; will try next root",
                native_root,
                exc,
            )

    try:
        from vetinari.adapter_manager import get_adapter_manager

        mgr = get_adapter_manager()
        for name in mgr.list_providers():
            models = mgr.discover_models(name)
            if models:
                resolved = models[0] if isinstance(models[0], str) else str(models[0])
                logger.info("_resolve_base_model: resolved 'auto' -> %s", resolved)
                return resolved
    except (ImportError, RuntimeError, OSError, AttributeError, TypeError, ValueError):
        logger.warning("_resolve_base_model: adapter_manager lookup failed - falling back to default HuggingFace model")

    resolved = _DEFAULT_TRAINING_BASE_MODEL
    logger.info("_resolve_base_model: resolved 'auto' -> %s", resolved)
    return resolved


def _resolve_model_revision(base_model: str, model_revision: str | None = None) -> str | None:
    """Resolve the immutable revision used by generated training loaders."""
    if model_revision:
        return model_revision.strip()
    if base_model == _DEFAULT_TRAINING_BASE_MODEL:
        raise ValueError(f"default training base model {base_model!r} requires an explicit immutable model_revision")

    model_path = Path(base_model)
    if model_path.exists():
        return _read_model_revision_manifest(model_path)

    if "/" not in base_model or "\\" in base_model:
        return None

    try:
        HfApi = import_module("huggingface_hub").HfApi
    except ImportError:
        logger.warning(
            "[TrainingPipeline] huggingface_hub is unavailable; remote base model %s will load without a pinned revision",
            base_model,
        )
        return None

    try:
        info = HfApi().model_info(repo_id=base_model, timeout=MODEL_DISCOVERY_TIMEOUT)
    except (RuntimeError, OSError, ValueError, TypeError, AttributeError) as exc:
        logger.warning(
            "[TrainingPipeline] could not resolve immutable revision for %s: %s",
            base_model,
            exc,
        )
        return None

    revision = getattr(info, "sha", None)
    return str(revision) if revision else None


def _record_unsupported_cloud_training(config: Mapping[str, Any]) -> TrainingRun:
    """Create a failed run object for cloud training without a provider adapter."""
    provider = str(config.get("provider") or "huggingface").strip() or "huggingface"
    base_model = str(config.get("base_model") or config.get("model_id") or _DEFAULT_TRAINING_BASE_MODEL)
    task_type = str(config.get("task_type") or "cloud")
    now = datetime.now(timezone.utc)
    run_id = f"cloud_unsupported_{provider.lower().replace('-', '_')}_{int(now.timestamp())}"
    return TrainingRun(
        run_id=run_id,
        timestamp=now.isoformat(),
        base_model=base_model,
        task_type=task_type,
        training_examples=0,
        epochs=int(config.get("epochs") or 0),
        success=False,
        backend="cloud",
        model_format=str(config.get("model_format") or "provider-managed"),
        error=(
            f"Cloud training via '{provider}' has no configured provider integration. "
            "Use run() for local QLoRA/DoRA training until a provider adapter is configured."
        ),
    )
