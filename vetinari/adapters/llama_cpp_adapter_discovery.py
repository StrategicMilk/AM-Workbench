"""Model discovery and resolution helpers for the llama.cpp adapter."""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any

from vetinari.adapters.base import ModelInfo
from vetinari.adapters.llama_cpp_model_info import (
    _estimate_memory_gb,
    _friendly_model_name,
    _infer_capabilities,
    _infer_context_window,
    _model_id_from_path,
)

logger = logging.getLogger(__name__)


class _LlamaCppDiscoverySupport:
    """Discovery, capability, and model-path methods for llama.cpp adapters."""

    _discovered_models: list[ModelInfo]
    _loaded_models: dict[str, Any]
    _models_dir: Path
    _registry_lock: threading.Lock
    models: list[ModelInfo]

    def discover_models(self) -> list[ModelInfo]:
        """Scan models directory for .gguf files and build model info list.

        Returns:
            List of ModelInfo for each discovered .gguf file.
        """
        models: list[ModelInfo] = []

        if not self._models_dir.exists():
            logger.warning("Models directory does not exist: %s", self._models_dir)
            return models

        gguf_files = sorted(self._models_dir.rglob("*.gguf"))
        if not gguf_files:
            logger.warning("No .gguf files found in %s", self._models_dir)
            return models

        for gguf_path in gguf_files:
            model_id = _model_id_from_path(gguf_path)
            memory_gb = _estimate_memory_gb(gguf_path)
            capabilities = _infer_capabilities(model_id)
            context_len = _infer_context_window(model_id)
            display_name = _friendly_model_name(model_id)

            info = ModelInfo(
                id=model_id,
                name=display_name,
                provider="local",
                endpoint=str(gguf_path),
                capabilities=capabilities,
                context_len=context_len,
                memory_gb=int(memory_gb),
                version="",
                latency_estimate_ms=500,
                throughput_tokens_per_sec=100.0,
                cost_per_1k_tokens=0.0,
                free_tier=True,
                tags=["local", "gguf", "llama-cpp"],
            )
            models.append(info)
            logger.info(
                "Discovered local model: %s (%.1f GB, ctx=%d, caps=%s)",
                model_id,
                memory_gb,
                context_len,
                capabilities,
            )

        self._discovered_models = models
        self.models = models
        return models

    def get_capabilities(self) -> dict[str, list[str]]:
        """Get capabilities of all discovered local models.

        Returns:
            Dict mapping model_id to list of capability tags.
        """
        if not self._discovered_models:
            self.discover_models()
        return {m.id: m.capabilities for m in self._discovered_models}

    def _resolve_model_path(self, model_id: str) -> Path | None:
        """Resolve a model ID to a .gguf file path.

        Checks loaded models first, then discovered models, then does
        an exact filename search in the models directory.

        Args:
            model_id: Exact model identifier or filename.

        Returns:
            Path to the .gguf file, or None if no exact match is available.
        """
        with self._registry_lock:
            loaded = self._loaded_models.get(model_id)
            if loaded is not None:
                return Path(loaded.file_path)

        for model in self._discovered_models:
            if model.id == model_id:
                return Path(model.endpoint)

        if self._models_dir.exists():
            requested_name = Path(model_id).name
            suffix = "" if requested_name.lower().endswith(".gguf") else ".gguf"
            candidate = self._models_dir / f"{requested_name}{suffix}"
            if candidate.exists():
                return candidate

        return None

    def _resolve_model_path_with_outcome(self, model_id: str) -> tuple[Path | None, str]:
        """Resolve a model request without substring or fallback substitution."""
        path = self._resolve_model_path(model_id)
        return path, "exact" if path is not None else "not_found"
