"""Model loading orchestration for :mod:`llama_cpp_model_cache`."""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from .llama_cpp_model_info import _LoadedModel

logger = logging.getLogger(__name__)


def get_or_load_model(cache: Any, model_id: str, model_path: Path) -> Any:
    """Get a cached llama.cpp model or load it from disk.

    Args:
        cache: LlamaCppModelCache instance that owns registries and settings.
        model_id: Model identifier.
        model_path: Path to the GGUF model file.

    Returns:
        Loaded llama.cpp model instance.
    """
    cache_hit = _lookup_cached_model(cache, model_id)
    if cache_hit is not None:
        return cache_hit

    model_lock = cache._get_model_lock(model_id)
    with model_lock:
        cache_hit = _lookup_cached_model(cache, model_id)
        if cache_hit is not None:
            return cache_hit

        vram_mgr, slot_acquired = _acquire_load_slot(model_id)
        reservation_placed = False
        try:
            llm, memory_needed, context_length, reservation_placed = _load_uncached_model(
                cache,
                model_id,
                model_path,
                vram_mgr,
            )
            reservation_placed = _register_loaded_model(
                cache,
                model_id,
                model_path,
                llm,
                memory_needed,
                context_length,
                vram_mgr,
            )
            return llm
        finally:
            _release_pending_vram(model_id, vram_mgr, reservation_placed, slot_acquired)


def _lookup_cached_model(cache: Any, model_id: str) -> Any | None:
    """Return a cached model and update LRU state when present."""
    with cache._registry_lock:
        if model_id in cache._loaded_models:
            loaded = cache._loaded_models[model_id]
            loaded.last_used = time.time()
            return loaded.model
    return None


def _acquire_load_slot(model_id: str) -> tuple[Any | None, bool]:
    """Acquire a concurrent-load slot when the VRAM manager is available."""
    try:
        from vetinari.models.vram_manager import get_vram_manager

        vram_mgr = get_vram_manager()
        slot_acquired = vram_mgr.acquire_load_slot()
        if not slot_acquired:
            logger.warning(
                "Load slot timeout for %s — proceeding without a VRAM concurrency slot; "
                "this model will contend for VRAM with already-loaded models and inference may slow or fail.",
                model_id,
            )
        return vram_mgr, slot_acquired
    except Exception:
        logger.warning("VRAMManager load slot unavailable for %s; proceeding without concurrency control", model_id)
        return None, False


def _load_uncached_model(
    cache: Any, model_id: str, model_path: Path, vram_mgr: Any | None
) -> tuple[Any, float, int, bool]:
    """Load one model after validation, budget checks, and constructor setup."""
    cache_mod = _cache_module()
    cache_mod.validate_gguf_file(model_path)
    _verify_model_signature(model_path)
    with cache._registry_lock:
        memory_needed = cache_mod._estimate_memory_gb(model_path)
        gpu_layers = cache._compute_gpu_layers(model_id, model_path, memory_needed)
        cache._ensure_vram_budget(memory_needed, gpu_layers)

    reservation_placed = _reserve_vram(vram_mgr, model_id, memory_needed)
    try:
        llama_kwargs, context_length = _build_llama_kwargs(cache, model_id, model_path, memory_needed, gpu_layers)
        llama_kwargs["type_k"] = cache_mod._resolve_kv_quant_type(cache._cache_type_k, cache_mod.llama_cpp)
        llama_kwargs["type_v"] = cache_mod._resolve_kv_quant_type(cache._cache_type_v, cache_mod.llama_cpp)
        _attach_speculative_decoding(cache, model_id, model_path, llama_kwargs, cache_mod.llama_cpp)

        if cache_mod.llama_cpp is None:
            raise RuntimeError("llama-cpp-python is not available")
        llm = cache_mod._create_llama_instance(cache_mod.llama_cpp, model_path, llama_kwargs)
        return llm, memory_needed, context_length, reservation_placed
    except Exception:
        if reservation_placed and vram_mgr is not None:
            try:
                vram_mgr.release_reservation(model_id)
            except Exception:
                logger.warning("Could not release VRAM reservation for %s after load failure", model_id)
        raise


def _verify_model_signature(model_path: Path) -> None:
    """Require a detached signature before loading a local GGUF model."""
    signature_candidates = (
        model_path.with_suffix(model_path.suffix + ".minisig"),
        model_path.with_suffix(model_path.suffix + ".sig"),
        model_path.with_suffix(model_path.suffix + ".asc"),
    )
    if not any(path.exists() and path.stat().st_size > 0 for path in signature_candidates):
        raise RuntimeError(f"Missing detached signature for local model artifact: {model_path}")


def _reserve_vram(vram_mgr: Any | None, model_id: str, memory_needed: float) -> bool:
    """Reserve VRAM before model construction when possible."""
    if vram_mgr is None:
        return False
    try:
        if not vram_mgr.reserve(model_id, memory_needed):
            logger.warning("VRAM reservation for %s denied (%.1f GB requested); proceeding", model_id, memory_needed)
        return True
    except Exception:
        logger.warning("VRAM reservation call failed for %s; proceeding without reservation", model_id)
        return False


def _build_llama_kwargs(
    cache: Any,
    model_id: str,
    model_path: Path,
    memory_needed: float,
    gpu_layers: int,
) -> tuple[dict[str, Any], int]:
    """Build llama.cpp constructor arguments from profiler or defaults."""
    try:
        profiler = _cache_module()._get_model_profiler_fn()()
        profile = profiler.profile_model(model_path)
        llama_kwargs = profile.get_llama_kwargs()
        llama_kwargs["n_gpu_layers"] = gpu_layers
        context_length = llama_kwargs.get("n_ctx", cache._default_context_length)
        logger.info(
            "Loading model %s via ModelProfiler (family=%s, ctx=%d, gpu_layers=%d, batch=%d)",
            model_id,
            profile.family,
            context_length,
            gpu_layers,
            llama_kwargs.get("n_batch", 512),
        )
        return llama_kwargs, context_length
    except Exception:
        logger.debug("ModelProfiler unavailable; using default loading for %s", model_id, exc_info=True)
        context_length = _cache_module()._infer_context_window(model_id) or cache._default_context_length
        llama_kwargs = {"n_gpu_layers": gpu_layers, "n_ctx": context_length, "flash_attn": True, "verbose": False}
        logger.info(
            "Loading model %s from %s (est. %.1f GB, ctx=%d, gpu_layers=%d)",
            model_id,
            model_path,
            memory_needed,
            context_length,
            gpu_layers,
        )
        return llama_kwargs, context_length


def _attach_speculative_decoding(
    cache: Any, model_id: str, model_path: Path, llama_kwargs: dict[str, Any], llama_cpp: Any
) -> None:
    """Attach draft-model or prompt-lookup speculative decoding when available."""
    try:
        config = cache._get_speculative_config()
    except AttributeError:
        config = None
    if config is None or not getattr(config, "enabled", False):
        return
    draft_attached = False
    if getattr(config, "draft_model_id", None):
        try:
            resolver = _cache_module()._get_draft_pair_resolver_fn()()
            available_gguf = sorted(cache._models_dir.rglob("*.gguf")) if cache._models_dir.exists() else []
            pair = resolver.find_pair(model_path, available_gguf)
            if pair is not None:
                draft_model = pair.to_llama_draft_model()
                if draft_model is not None:
                    llama_kwargs["draft_model"] = draft_model
                    draft_attached = True
                    logger.info(
                        "Speculative decoding (draft model): %s -> %s (%.1fx)",
                        model_id,
                        pair.draft_model_path.stem,
                        pair.size_ratio,
                    )
        except Exception:
            logger.warning("Draft pair resolution failed for %s", model_id, exc_info=True)

    if (
        llama_cpp is not None
        and not draft_attached
        and getattr(config, "use_prompt_lookup_fallback", False)
        and hasattr(llama_cpp, "LlamaPromptLookupDecoding")
    ):
        try:
            draft_n_tokens = int(getattr(config, "draft_n_tokens", 5))
            llama_kwargs["draft_model"] = llama_cpp.LlamaPromptLookupDecoding(num_pred_tokens=draft_n_tokens)
            logger.info("Speculative decoding (PromptLookup): %s with num_pred_tokens=%d", model_id, draft_n_tokens)
        except Exception:
            logger.warning("PromptLookupDecoding not available for %s; speculative decoding disabled", model_id)


def _register_loaded_model(
    cache: Any,
    model_id: str,
    model_path: Path,
    llm: Any,
    memory_needed: float,
    context_length: int,
    vram_mgr: Any | None,
) -> bool:
    """Store a loaded model and start trusted post-load work."""
    cache_mod = _cache_module()
    with cache._registry_lock:
        cache._loaded_models[model_id] = _LoadedModel(
            model=llm,
            model_id=model_id,
            file_path=model_path,
            memory_gb=memory_needed,
            context_length=context_length,
        )
    reservation_pending = False
    if vram_mgr is not None:
        try:
            vram_mgr.register_load(model_id, memory_needed)
        except Exception:
            logger.warning("VRAMManager register_load failed for %s; VRAM budget may be stale", model_id)
            reservation_pending = True

    logger.info("Model %s loaded successfully", model_id)
    if cache_mod._has_trusted_chat_template(model_id, llm):
        cache._warm_up_model(model_id, llm)
        cache._calibration_pool.submit(_background_calibrate, model_id, llm)
    else:
        logger.warning("Skipping background calibration for %s because embedded chat template is untrusted", model_id)
    return reservation_pending


def _background_calibrate(model_id: str, llm: Any) -> None:
    """Run model calibration in the background without blocking inference."""
    try:
        calibrate_model, seed_thompson_priors, load_cached_profile, save_profile = (
            _cache_module()._get_calibration_fns()
        )
        cached = load_cached_profile(model_id)
        if cached is None or "calibration" not in cached:
            cal_result = calibrate_model(model_id, llm)
            seed_thompson_priors(model_id, cal_result)
            profile_data = cached if cached is not None else {}
            profile_data["calibration"] = cal_result.to_dict()
            save_profile(model_id, profile_data)
            logger.info("Background calibration completed for %s", model_id)
    except Exception:
        logger.warning("Background calibration failed for %s", model_id, exc_info=True)


def _release_pending_vram(model_id: str, vram_mgr: Any | None, reservation_placed: bool, slot_acquired: bool) -> None:
    """Release pending VRAM reservation and load slot after load attempts."""
    if reservation_placed and vram_mgr is not None:
        try:
            vram_mgr.release_reservation(model_id)
        except Exception:
            logger.warning("Could not release VRAM reservation for %s after load failure", model_id)
    if slot_acquired and vram_mgr is not None:
        vram_mgr.release_load_slot()


def _cache_module() -> Any:
    """Return the loaded cache module to access lazy hooks without import cycles."""
    from vetinari.adapters import llama_cpp_model_cache

    return llama_cpp_model_cache
