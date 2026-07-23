"""Native local inference adapter using llama-cpp-python.

Provides in-process GGUF model loading and inference via llama.cpp,
eliminating the need for an external inference server. This is the
primary local inference backend for Vetinari.

Features:
- Auto-discovery of .gguf files from a configurable directory
- In-process model loading with GPU offload
- LRU model cache with VRAM budget enforcement (via LlamaCppModelCache)
- Capability inference from model filenames
- Streaming and non-streaming inference
- Semantic caching and KV state caching for repeated prompts

Split layout:
    llama_cpp_model_info.py      â€” constants, helpers, _LoadedModel dataclass
    llama_cpp_model_cache.py     â€” LlamaCppModelCache (VRAM budget, LRU, loading)
    llama_cpp_local_adapter.py   â€” LocalInferenceAdapter convenience wrapper
    llama_cpp_lazy_imports.py    â€” lazy-import getters for vetinari sub-packages
    llama_cpp_adapter.py         â€” LlamaCppProviderAdapter (this file) + re-exports
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, cast

from vetinari.config.settings import get_settings
from vetinari.constants import (
    DEFAULT_CONTEXT_LENGTH,
    DEFAULT_GPU_LAYERS,
)
from vetinari.exceptions import ConfigurationError
from vetinari.models.vram_capacity import KV_BYTES_PER_TOKEN
from vetinari.utils.lazy_import import lazy_import

from . import llama_cpp_adapter_helpers as _adapter_helpers
from .base import InferenceRequest, InferenceResponse, ModelInfo, ProviderAdapter, ProviderConfig, ProviderType
from .llama_cpp_adapter_discovery import _LlamaCppDiscoverySupport
from .llama_cpp_adapter_inference import LlamaCppInferenceRuntimeDeps, run_inference
from .llama_cpp_lazy_imports import (
    _get_draft_pair_resolver_fn,
    _get_hash_system_prompt_fn,
    _get_kv_state_cache_fn,
    _get_model_selector_fn,
    _get_semantic_cache_fn,
)
from .llama_cpp_model_cache import LlamaCppModelCache
from .llama_cpp_model_info import (
    _EMPTY_CHOICES_FALLBACK,
    _EMPTY_DELTA_FALLBACK,
    DEFAULT_RAM_BUDGET_GB,
    _estimate_memory_gb,
    _infer_capabilities,
    _infer_context_window,
    _model_id_from_path,
)
from .speculative_decoding import SpeculativeDecodingConfig, detect_speculative_capability

logger = logging.getLogger(__name__)


SYSTEM_PROMPT_BOUNDARY = _adapter_helpers.SYSTEM_PROMPT_BOUNDARY
ADAPTER_CACHE_VERSION = _adapter_helpers.ADAPTER_CACHE_VERSION
_artifact_sha256 = _adapter_helpers._artifact_sha256
_build_completion_kwargs = _adapter_helpers._build_completion_kwargs
_chat_template_validation_metadata = _adapter_helpers._chat_template_validation_metadata
_extract_embedded_chat_template = _adapter_helpers._extract_embedded_chat_template
_semantic_cache_identity = _adapter_helpers._semantic_cache_identity
_validate_loaded_chat_template = _adapter_helpers._validate_loaded_chat_template

# â”€â”€ Optional llama-cpp-python import â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
llama_cpp: Any
llama_cpp, _LLAMA_CPP_AVAILABLE = lazy_import("llama_cpp")

# â”€â”€ Lazy module-level imports for hot-path dependencies (avoids per-call import overhead) â”€â”€
_model_profiler_data: Any = None
_model_profiler_data_loaded = False
_vram_manager_mod: Any = None
_vram_manager_mod_loaded = False
_lazy_loader_lock = threading.RLock()


def _get_model_profiler_data() -> Any:
    global _model_profiler_data, _model_profiler_data_loaded
    if not _model_profiler_data_loaded:
        with _lazy_loader_lock:
            if not _model_profiler_data_loaded:
                _model_profiler_data, _ = lazy_import("vetinari.models.model_profiler_data")
                _model_profiler_data_loaded = True
    return _model_profiler_data


def _get_vram_manager_module() -> Any:
    global _vram_manager_mod, _vram_manager_mod_loaded
    if not _vram_manager_mod_loaded:
        with _lazy_loader_lock:
            if not _vram_manager_mod_loaded:
                _vram_manager_mod, _ = lazy_import("vetinari.models.vram_manager")
                _vram_manager_mod_loaded = True
    return _vram_manager_mod


# VRAM cost of KV cache per token for each quantization type (bytes per token).
_KV_BYTES_PER_TOKEN = KV_BYTES_PER_TOKEN


def _make_inference_runtime_deps() -> LlamaCppInferenceRuntimeDeps:
    """Build the non-streaming inference dependency bundle from facade globals."""
    return LlamaCppInferenceRuntimeDeps(
        llama_cpp_available=_LLAMA_CPP_AVAILABLE,
        get_model_profiler_data=_get_model_profiler_data,
        get_vram_manager_module=_get_vram_manager_module,
        get_semantic_cache_fn=_get_semantic_cache_fn,
        get_kv_state_cache_fn=_get_kv_state_cache_fn,
        get_hash_system_prompt_fn=_get_hash_system_prompt_fn,
        semantic_cache_identity=_semantic_cache_identity,
        validate_loaded_chat_template=_validate_loaded_chat_template,
        chat_template_validation_metadata=_chat_template_validation_metadata,
        model_id_from_path=_model_id_from_path,
        detect_speculative_capability=detect_speculative_capability,
        system_prompt_boundary=SYSTEM_PROMPT_BOUNDARY,
        kv_bytes_per_token=_KV_BYTES_PER_TOKEN,
        logger=logger,
    )


class LlamaCppProviderAdapter(_LlamaCppDiscoverySupport, LlamaCppModelCache, ProviderAdapter):
    """Native local inference adapter using llama-cpp-python.

    Loads GGUF models directly into GPU/CPU memory and runs inference
    in-process via llama.cpp. No external server required.

    Inherits ``LlamaCppModelCache`` for VRAM-budget-aware model
    loading, per-model locking, LRU eviction, and background calibration.

    Configuration via ``ProviderConfig.extra_config``:
        - ``models_dir``: Directory to scan for .gguf files (default: ./models)
        - ``gpu_layers``: Number of layers to offload to GPU (-1 = all)
        - ``context_length``: Default context window size
        - ``ram_budget_gb``: CPU RAM available for offloaded layers (default: 30)
        - ``cpu_offload_enabled``: Allow partial GPU+CPU split loading (default: true)
    """

    def __init__(self, config: ProviderConfig):
        """Validate that this adapter is used only with LOCAL providers, then parse GPU and model-dir settings.

        Args:
            config: Provider configuration. Must have provider_type LOCAL.

        Raises:
            ConfigurationError: If provider_type is not LOCAL.
        """
        if config.provider_type != ProviderType.LOCAL:
            msg = f"LlamaCppProviderAdapter requires ProviderType.LOCAL, got {config.provider_type}"
            raise ConfigurationError(msg)
        super().__init__(config)

        extra = config.extra_config or {}
        from vetinari.constants import OPERATOR_MODELS_CACHE_DIR

        self._models_dir = Path(extra.get("models_dir", OPERATOR_MODELS_CACHE_DIR))
        self._gpu_layers = int(extra.get("gpu_layers", DEFAULT_GPU_LAYERS))
        self._default_context_length = int(extra.get("context_length", DEFAULT_CONTEXT_LENGTH))
        self._memory_budget_gb = config.memory_budget_gb
        # CPU RAM budget for layers offloaded from GPU; 0 disables CPU offload
        self._ram_budget_gb = float(extra.get("ram_budget_gb", DEFAULT_RAM_BUDGET_GB))
        # Whether to allow partial GPU + CPU split-loading for oversized models
        _cpu_offload_raw = extra.get("cpu_offload_enabled", "true")
        self._cpu_offload_enabled = str(_cpu_offload_raw).lower() not in ("false", "0", "no")

        # KV cache quantization â€” read from settings, allow per-adapter override via extra_config
        _settings = get_settings()
        self._cache_type_k: str = str(extra.get("cache_type_k", _settings.local_cache_type_k))
        self._cache_type_v: str = str(extra.get("cache_type_v", _settings.local_cache_type_v))

        # Shared lock protects the _loaded_models dict and _model_locks dict
        self._loaded_models: dict[str, Any] = {}
        self._registry_lock = threading.Lock()
        # Per-model locks allow concurrent loading of different models (5-10x throughput)
        self._model_locks: dict[str, threading.Lock] = {}
        self._discovered_models: list[ModelInfo] = []

        # Background calibration pool â€” max 1 concurrent calibration to avoid VRAM contention
        self._calibration_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="calibrate")

    @staticmethod
    def _get_speculative_config() -> SpeculativeDecodingConfig:
        """Read speculative decoding settings from application config.

        Consults ``VetinariSettings`` for the three speculative decoding fields
        (``speculative_decoding_enabled``, ``speculative_draft_model_id``,
        ``speculative_draft_n_tokens``) and returns a typed config object.

        Returns:
            ``SpeculativeDecodingConfig`` populated from settings.  All fields
            have safe defaults so the result is always valid.
        """
        _settings = get_settings()
        return SpeculativeDecodingConfig(
            enabled=_settings.speculative_decoding_enabled,
            draft_model_id=_settings.speculative_draft_model_id,
            draft_n_tokens=_settings.speculative_draft_n_tokens,
            use_prompt_lookup_fallback=True,  # Always allow PromptLookup fallback
        )

    def health_check(self) -> dict[str, Any]:
        """Check local inference health.

        Verifies llama-cpp-python is installed, GPU offload is available,
        and the models directory contains .gguf files.

        Returns:
            Health status dict with keys: healthy, reason, timestamp,
            and optionally gpu_offload and model_count.
        """
        timestamp = str(time.time())

        if not _LLAMA_CPP_AVAILABLE:
            return {
                "healthy": False,
                "reason": "llama-cpp-python is not installed",
                "timestamp": timestamp,
            }

        gpu_available = False
        try:
            if llama_cpp is not None:
                gpu_available = bool(llama_cpp.llama_supports_gpu_offload())
        except (ImportError, OSError, RuntimeError):
            logger.warning("GPU offload check failed", exc_info=True)

        if not self._models_dir.exists():
            return {
                "healthy": False,
                "reason": f"Models directory does not exist: {self._models_dir}",
                "timestamp": timestamp,
            }

        gguf_count = len(list(self._models_dir.rglob("*.gguf")))
        if gguf_count == 0:
            return {
                "healthy": False,
                "reason": f"No .gguf files found in {self._models_dir}",
                "timestamp": timestamp,
            }

        return {
            "healthy": True,
            "reason": f"OK: {gguf_count} models available, GPU offload={gpu_available}",
            "timestamp": timestamp,
            "gpu_offload": gpu_available,
            "model_count": gguf_count,
        }

    @staticmethod
    def _build_completion_kwargs(
        request: InferenceRequest,
        messages: list[dict[str, str]],
        *,
        stream: bool = False,
    ) -> dict[str, Any]:
        """Build kwargs dict for llama_cpp create_chat_completion."""
        return cast("dict[str, Any]", _build_completion_kwargs(request, messages, stream=stream))

    def infer(self, request: InferenceRequest) -> InferenceResponse:
        """Run inference on a local GGUF model.

        Loads the model if not already cached. Enforces VRAM budget by
        evicting least-recently-used models when necessary.

        Args:
            request: Inference request with model_id, prompt, and parameters.

        Returns:
            InferenceResponse with output text, latency, and token usage.

        Raises:
            RuntimeError: Propagated only when an unexpected internal adapter
                error occurs before an ``InferenceResponse`` can be built.
        """
        return run_inference(self, request, _make_inference_runtime_deps())

    def _record_draft_acceptance(
        self,
        request: InferenceRequest,
        response: InferenceResponse,
        model_id: str,
    ) -> None:
        """Record speculative-decoding draft token acceptance for Thompson Sampling.

        Reads draft acceptance timings from the loaded model's last_timings dict and
        forwards per-token accept/reject events to the draft pair resolver and model
        selector for reward tracking.

        Args:
            request: The original inference request (used for task_type metadata).
            response: The completed inference response (unused; present for symmetry).
            model_id: The model ID whose timings should be read.
        """
        try:
            resolver = _get_draft_pair_resolver_fn()()
            with self._registry_lock:
                loaded = self._loaded_models.get(model_id)
            if loaded and hasattr(loaded.model, "_draft_model"):
                _timings = getattr(loaded.model, "last_timings", None)
                if _timings and "n_draft_accepted" in _timings:
                    _accepted = _timings["n_draft_accepted"]
                    _total = _timings.get("n_draft_total", 1)
                    for _ in range(min(_accepted, _total)):
                        resolver.record_acceptance(model_id, "draft", True)
                    for _ in range(_total - _accepted):
                        resolver.record_acceptance(model_id, "draft", False)

                    _acceptance_rate = _accepted / max(_total, 1)
                    _task_type = request.metadata.get("task_type", "general")
                    try:
                        selector = _get_model_selector_fn()()
                        if hasattr(selector, "record_reward"):
                            selector.record_reward(
                                f"{model_id}:draft:{_task_type}",
                                _acceptance_rate,
                            )
                    except Exception:
                        logger.warning(
                            "Thompson draft reward recording failed â€” acceptance rate not updated", exc_info=True
                        )
        except Exception:
            logger.warning(
                "Draft acceptance recording failed for %s â€” Thompson priors unchanged", model_id, exc_info=True
            )

    def infer_stream(self, request: InferenceRequest) -> Iterator[str]:
        """Stream inference tokens from a local GGUF model.

        Args:
            request: Inference request with model_id, prompt, and parameters.

        Yields:
            Token strings as they are generated.
        """
        if not _LLAMA_CPP_AVAILABLE:
            return

        model_path = self._resolve_model_path(request.model_id)
        if model_path is None:
            return

        model_id = _model_id_from_path(model_path)

        try:
            llm = self._get_or_load_model(model_id, model_path)
        except Exception as exc:
            logger.error("Failed to load model %s for streaming: %s", model_id, exc)
            return

        template_validation = _validate_loaded_chat_template(model_id, llm)
        if template_validation is not None and (
            not template_validation.is_trusted or template_validation.fallback_used
        ):
            logger.warning(
                "Rejecting streaming inference for %s due to untrusted embedded chat template",
                model_id,
            )
            return

        messages: list[dict[str, str]] = []
        if request.system_prompt:
            messages.append({"role": "system", "content": request.system_prompt})
        messages.append({"role": "user", "content": request.prompt})

        try:
            _stream_kwargs = self._build_completion_kwargs(request, messages, stream=True)
            stream = llm.create_chat_completion(**_stream_kwargs)

            for chunk in stream:
                delta = chunk.get("choices", _EMPTY_CHOICES_FALLBACK)[0].get("delta", _EMPTY_DELTA_FALLBACK)
                text = delta.get("content", "")
                if text:
                    yield text

        except Exception as exc:
            logger.error("Streaming failed for model %s: %s", model_id, exc)


__all__ = [
    "LlamaCppProviderAdapter",
    "_estimate_memory_gb",
    "_infer_capabilities",
    "_infer_context_window",
    "_model_id_from_path",
]
