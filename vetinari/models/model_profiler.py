"""ModelProfiler — GGUF metadata reader and optimal parameter calculator.

Reads GGUF file headers via the ``gguf`` package (memory-mapped, instant) to
extract architecture metadata, then computes optimal n_ctx, n_gpu_layers,
sampling parameters, and Llama() constructor kwargs for each model.

Results are cached to ``~/.vetinari/model_configs/{model_id}.json`` for
persistence across sessions.

All constants, data classes, and calculation functions live in
``vetinari.models.model_profiler_data`` and are re-exported from here so that
existing ``from vetinari.models.model_profiler import ...`` imports continue
to work unchanged.

Usage::

    from vetinari.models.model_profiler import get_model_profiler

    profiler = get_model_profiler()
    profile = profiler.profile_model(Path(DEFAULT_MODELS_DIR) / "qwen2.5-coder-7b-q8_0.gguf")
    # -> ModelProfile(family="qwen2", n_ctx=16384, n_gpu_layers=-1, ...)
"""

from __future__ import annotations

import importlib
import logging
import threading
from pathlib import Path
from typing import Any

# Re-export everything callers may import from this module
from vetinari.models.model_profiler_data import (
    _CHAT_FORMAT_MAP,
    _DEFAULT_TEMPERATURES,
    _ROPE_FREQ_BASE_OVERRIDES,
    _TEMPERATURE_MATRIX,
    GGUFMetadata,
    ModelProfile,
    _load_cached_profile,
    _save_profile,
    build_model_artifact_identity,
    calculate_gpu_layers,
    calculate_optimal_context,
    detect_family,
    estimate_kv_per_token,
    model_profile_cache_id,
    read_metadata,
)

logger = logging.getLogger(__name__)


# -- ModelProfiler class -------------------------------------------------------


class ModelProfiler:
    """Profiles GGUF models to compute optimal Llama() constructor parameters.

    Reads GGUF metadata, calculates resource-aware n_ctx and n_gpu_layers,
    selects chat format and rope frequency, and caches results to disk.
    """

    def __init__(self, vram_gb: float | None = None, ram_gb: float | None = None):
        """Set up the profiler with system resource limits.

        Args:
            vram_gb: Available GPU VRAM in GB. When None, auto-detected via
                pynvml or defaults to 24 GB.
            ram_gb: Available system RAM in GB. When None, auto-detected via
                psutil or defaults to 32 GB.
        """
        self._vram_gb = vram_gb if vram_gb is not None else self._detect_vram()
        self._ram_gb = ram_gb if ram_gb is not None else self._detect_ram()
        self._cache: dict[str, ModelProfile] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _detect_vram() -> float:
        """Auto-detect available GPU VRAM via pynvml."""
        try:
            pynvml: Any = importlib.import_module("pynvml")

            pynvml.nvmlInit()
            try:
                handle = pynvml.nvmlDeviceGetHandleByIndex(0)
                mem = pynvml.nvmlDeviceGetMemoryInfo(handle)
                return float(mem.free) / (1024**3)
            finally:
                pynvml.nvmlShutdown()
        except Exception:
            logger.warning("VRAM auto-detection failed; defaulting to 24 GB")
            return 24.0

    @staticmethod
    def _detect_ram() -> float:
        """Auto-detect available system RAM via psutil."""
        try:
            import psutil

            return float(psutil.virtual_memory().available) / (1024**3)
        except Exception:
            logger.warning("RAM auto-detection failed; defaulting to 32 GB")
            return 32.0

    def profile_model(self, model_path: Path, force_refresh: bool = False) -> ModelProfile:
        """Profile a GGUF model and return optimal parameters.

        Reads GGUF metadata, computes n_ctx, n_gpu_layers, chat format, rope
        frequency, and sampling defaults. Results are cached in memory and
        persisted to ``~/.vetinari/model_configs/``.

        Args:
            model_path: Path to a ``.gguf`` file.
            force_refresh: When True, skip cache and re-profile.

        Returns:
            Fully computed ModelProfile with constructor kwargs and sampling
            defaults.
        """
        model_id = model_path.stem
        artifact_identity = build_model_artifact_identity(model_path)
        cache_id = model_profile_cache_id(model_path, artifact_identity["artifact_sha256"])
        cached_profile = self._cached_profile(cache_id, model_id, force_refresh=force_refresh)
        if cached_profile is not None:
            return cached_profile

        profile = self._build_fresh_profile(model_path, model_id, artifact_identity)
        self._cache_profile(cache_id, profile)
        logger.info(
            "Profiled model %s: family=%s, ctx=%d, gpu_layers=%d, batch=%d, chat=%s",
            model_id,
            profile.family,
            profile.n_ctx,
            profile.n_gpu_layers,
            profile.n_batch,
            profile.chat_format,
        )
        return profile

    def _cached_profile(self, cache_id: str, model_id: str, *, force_refresh: bool) -> ModelProfile | None:
        """Return an in-memory or disk cached profile when valid."""
        if force_refresh:
            return None
        with self._lock:
            if cache_id in self._cache:
                return self._cache[cache_id]
        cached = _load_cached_profile(cache_id)
        if cached is None:
            return None
        try:
            profile = self._profile_from_dict(cached, model_id)
        except Exception:
            logger.warning("Disk cache invalid for %s; re-profiling", cache_id)
            return None
        with self._lock:
            self._cache[cache_id] = profile
        return profile

    def _build_fresh_profile(
        self,
        model_path: Path,
        model_id: str,
        artifact_identity: dict[str, Any],
    ) -> ModelProfile:
        """Read GGUF metadata and compute a fresh model profile."""
        metadata = read_metadata(model_path)
        family = detect_family(metadata.architecture)

        if metadata.block_count > 0 and metadata.file_size_gb > 0:
            bytes_per_layer = (metadata.file_size_gb * (1024**3)) / metadata.block_count
        else:
            bytes_per_layer = 0.5 * (1024**3)

        kv_per_token = estimate_kv_per_token(
            head_count_kv=metadata.head_count_kv,
            embedding_length=metadata.embedding_length,
            head_count=metadata.head_count,
            kv_quant="q8_0",
        )
        trained_limit = metadata.context_length if metadata.context_length > 0 else 8192
        n_ctx = calculate_optimal_context(
            free_vram_gb=self._vram_gb,
            model_vram_gb=metadata.file_size_gb * 1.1,
            kv_per_token=kv_per_token,
            trained_limit=trained_limit,
        )
        kv_reserve_gb = (n_ctx * kv_per_token) / (1024**3)
        n_gpu_layers = calculate_gpu_layers(
            free_vram_gb=self._vram_gb,
            bytes_per_layer=bytes_per_layer,
            num_layers=metadata.block_count,
            kv_reserve_gb=kv_reserve_gb,
            expert_count=metadata.expert_count,
            expert_used_count=metadata.expert_used_count,
        )
        n_batch = self._compute_batch_size(n_ctx, metadata.file_size_gb)
        chat_format = _CHAT_FORMAT_MAP.get(family)
        rope_freq_base = _ROPE_FREQ_BASE_OVERRIDES.get(family)
        base_temps = _TEMPERATURE_MATRIX.get(family, _DEFAULT_TEMPERATURES).copy()
        return ModelProfile(
            model_id=model_id,
            family=family,
            metadata=metadata,
            n_ctx=n_ctx,
            n_gpu_layers=n_gpu_layers,
            artifact_path=artifact_identity["artifact_path"],
            artifact_sha256=artifact_identity["artifact_sha256"],
            artifact_size_bytes=artifact_identity["artifact_size_bytes"],
            artifact_mtime_ns=artifact_identity["artifact_mtime_ns"],
            n_batch=n_batch,
            use_mlock=True,
            chat_format=chat_format,
            rope_freq_base=rope_freq_base,
            flash_attn=True,
            kv_type="q8_0",
            base_temperatures=base_temps,
        )

    def _cache_profile(self, cache_id: str, profile: ModelProfile) -> None:
        """Persist a computed profile to memory and disk caches."""
        with self._lock:
            self._cache[cache_id] = profile
        _save_profile(cache_id, profile.to_dict())

    @staticmethod
    def _compute_batch_size(n_ctx: int, file_size_gb: float) -> int:
        """Compute optimal batch size based on context and model size.

        Larger batch sizes improve throughput but use more memory. The formula
        balances throughput against memory pressure.

        Args:
            n_ctx: Context window size.
            file_size_gb: Model file size in GB (proxy for memory pressure).

        Returns:
            Batch size (256, 512, or 1024).
        """
        if file_size_gb > 20:
            return 256  # Large models: conserve memory
        if n_ctx >= 32768:
            return 256  # Long context: conserve memory
        if file_size_gb < 5:
            return 1024  # Small models: maximize throughput
        return 512  # Default

    @staticmethod
    def _profile_from_dict(data: dict[str, Any], model_id: str) -> ModelProfile:
        """Reconstruct a ModelProfile from a cached dict.

        Args:
            data: Serialized profile dict from disk cache.
            model_id: Model identifier.

        Returns:
            Reconstructed ModelProfile instance.
        """
        meta_dict = data.get("metadata", {})
        metadata = GGUFMetadata(**{k: v for k, v in meta_dict.items() if k in GGUFMetadata.__dataclass_fields__})

        return ModelProfile(
            model_id=model_id,
            family=data.get("family", "unknown"),
            metadata=metadata,
            n_ctx=data.get("n_ctx", 8192),
            n_gpu_layers=data.get("n_gpu_layers", -1),
            artifact_path=data.get("artifact_path", ""),
            artifact_sha256=data.get("artifact_sha256", ""),
            artifact_size_bytes=data.get("artifact_size_bytes", 0),
            artifact_mtime_ns=data.get("artifact_mtime_ns", 0),
            n_batch=data.get("n_batch", 512),
            use_mlock=data.get("use_mlock", True),
            chat_format=data.get("chat_format"),
            seed=data.get("seed", -1),
            rope_freq_base=data.get("rope_freq_base"),
            flash_attn=data.get("flash_attn", True),
            kv_type=data.get("kv_type", "q8_0"),
            base_temperatures=data.get("base_temperatures", {}),
        )

    def invalidate(self, model_id: str) -> None:
        """Remove a model from the in-memory cache, forcing re-profile on next access.

        Args:
            model_id: Model identifier to invalidate.
        """
        with self._lock:
            self._cache.pop(model_id, None)
            for cache_id, profile in list(self._cache.items()):
                if profile.model_id == model_id:
                    self._cache.pop(cache_id, None)


# -- Singleton -----------------------------------------------------------------

_profiler: ModelProfiler | None = None
_profiler_lock = threading.Lock()


def get_model_profiler(vram_gb: float | None = None, ram_gb: float | None = None) -> ModelProfiler:
    """Return the singleton ModelProfiler, creating it if necessary.

    Args:
        vram_gb: Override VRAM (only used on first call).
        ram_gb: Override RAM (only used on first call).

    Returns:
        The shared ModelProfiler instance.
    """
    global _profiler
    if _profiler is None:
        with _profiler_lock:
            if _profiler is None:
                _profiler = ModelProfiler(vram_gb=vram_gb, ram_gb=ram_gb)
    return _profiler


def reset_model_profiler() -> None:
    """Reset the singleton ModelProfiler (for testing)."""
    global _profiler
    with _profiler_lock:
        _profiler = None
