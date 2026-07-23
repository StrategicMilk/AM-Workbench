"""Model recommendation engine for native and GGUF model selection.

This is step 2 of the setup pipeline: Hardware Detection ->
**Model Recommendation** -> Init Wizard -> Configuration.

Maps detected hardware capabilities to backend-aware model choices,
considering VRAM tiers, quantization levels, and use-case fitness.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import Any

from vetinari.knowledge import get_quant_recommendation
from vetinari.setup.model_recommender_catalog import (
    _CPU_OFFLOAD_MODELS,
    _MODALITY_TIERS,
    _VRAM_TIERS,
    catalog_is_stale,
    catalog_refresh_due_on,
)
from vetinari.setup.model_recommender_portfolio import (
    _PORTFOLIO_MODEL_DEFAULTS as _PORTFOLIO_MODEL_DEFAULTS,
)
from vetinari.setup.model_recommender_types import Modality, SetupModelRecommendation
from vetinari.system.hardware_detect import GpuVendor, HardwareProfile

logger = logging.getLogger(__name__)


# Native serving backends that can use GPU-oriented model formats in the
# setup recommender. vLLM remains a runtime/server adapter elsewhere, but it is
# not a default model recommendation backend because arbitrary native snapshot
# rows do not prove deployable vLLM compatibility.
_NATIVE_BACKENDS = ("amw_engine", "nim")
_LLAMA_CPP_ALIASES = {"llama_cpp", "llama-cpp", "llama", "local"}


def _normalize_recommendation_backend(backend: str) -> str:
    normalized = backend.strip().lower().replace("-", "_")
    if normalized in _LLAMA_CPP_ALIASES:
        return "llama_cpp"
    return normalized


def _estimate_model_budget_gb(hardware: HardwareProfile) -> float:
    vram = hardware.effective_vram_gb
    if hardware.gpu_vendor == GpuVendor.APPLE and vram == 0:
        vram = round(hardware.ram_gb * 0.75 * 0.9, 1)
    if vram == 0 and hardware.ram_gb > 0:
        vram = round(hardware.ram_gb * 0.4, 1)
    return vram


def _default_backend_order(hardware: HardwareProfile) -> list[str]:
    if not hardware.has_gpu:
        return ["llama_cpp"]
    if hardware.gpu_vendor == GpuVendor.NVIDIA and hardware.cuda_available:
        return ["amw_engine", "nim", "llama_cpp"]
    return ["llama_cpp"]


def _resolve_recommendation_backends(
    hardware: HardwareProfile,
    available_backends: list[str] | None,
) -> list[str]:
    raw_backends = _default_backend_order(hardware) if available_backends is None else available_backends
    resolved: list[str] = []
    for backend in raw_backends:
        normalized = _normalize_recommendation_backend(backend)
        if normalized in {"nim", "amw_engine", "llama_cpp"} and normalized not in resolved:
            resolved.append(normalized)
    return resolved or ["llama_cpp"]


def _backend_label(backend: str) -> str:
    if backend == "nim":
        return "NIM"
    if backend == "vllm":
        return "vLLM"
    if backend == "amw_engine":
        return "AM Engine"
    return "llama.cpp"


def _native_model_for_backend(
    model: SetupModelRecommendation,
    backend: str,
    *,
    primary_backend: str | None = None,
) -> SetupModelRecommendation:
    label = _backend_label(backend)
    reason = f"{model.reason} Native serving via {label}."
    native_format = model.model_format if model.model_format != "gguf" else "safetensors"
    native_filename = model.filename if model.model_format != "gguf" else ""
    return replace(
        model,
        filename=native_filename,
        backend=backend,
        model_format=native_format,
        reason=reason,
        is_primary=model.is_primary and (primary_backend is None or backend == primary_backend),
    )


def _default_promotion_allowed(model: SetupModelRecommendation) -> bool:
    notes = model.co_residency_notes.lower()
    blocked_tokens = (
        "license=blocked:",
        "license=non-commercial",
        "license=noncommercial",
        "license=custom",
        "license=gated",
        "license=other",
        "license=review-required",
    )
    return not any(token in notes for token in blocked_tokens)


class ModelRecommender:
    """Recommends models based on detected hardware and intended use cases.

    Uses a VRAM-to-model matrix to select models that will fit in available
    GPU memory with headroom for the OS and inference overhead.

    The factory pipeline runs multiple agents in parallel - so users benefit
    from a *portfolio* of models: small efficient ones for grunt work
    (classification, routing, extraction) and larger ones for complex reasoning.
    """

    def __init__(self, vram_tiers: list[dict[str, Any]] | None = None) -> None:
        self._tiers = vram_tiers or _VRAM_TIERS

    def recommend_models(self, hardware: HardwareProfile) -> list[SetupModelRecommendation]:
        """Return recommended models for the given hardware profile.

        Selects the VRAM tier matching the hardware's effective VRAM, then
        returns all models in that tier.  For Apple Silicon with unified
        memory, uses the estimated VRAM share.

        Args:
            hardware: Detected hardware profile.

        Returns:
            List of ModelRecommendation objects, primary first.
        """
        if catalog_is_stale():
            logger.warning(
                "Static model recommendation catalog is past its refresh date (%s); refresh sources before release",
                catalog_refresh_due_on().isoformat(),
            )
        vram = hardware.effective_vram_gb

        # Apple Silicon with no discrete GPU uses unified memory estimate
        if hardware.gpu_vendor == GpuVendor.APPLE and vram == 0:
            vram = round(hardware.ram_gb * 0.75 * 0.9, 1)

        # CPU-only fallback: use RAM / 2 as rough VRAM equivalent
        if vram == 0 and hardware.ram_gb > 0:
            vram = round(hardware.ram_gb * 0.4, 1)

        for tier in self._tiers:
            if tier["min_vram_gb"] <= vram < tier["max_vram_gb"]:
                models: list[SetupModelRecommendation] = list(tier["models"])
                logger.info(
                    "Recommended %d models for %.1f GB effective VRAM (tier: %s)",
                    len(models),
                    vram,
                    tier["label"],
                )
                return models

        # Fallback to smallest tier
        return list(self._tiers[0]["models"])

    def get_tier_label(self, hardware: HardwareProfile) -> str:
        """Return the human-readable label for the matched VRAM tier.

        Args:
            hardware: Detected hardware profile.

        Returns:
            Tier label string (e.g. "8-16 GB VRAM").
        """
        vram = hardware.effective_vram_gb
        if vram == 0 and hardware.ram_gb > 0:
            vram = round(hardware.ram_gb * 0.4, 1)

        for tier in self._tiers:
            if tier["min_vram_gb"] <= vram < tier["max_vram_gb"]:
                return str(tier["label"])
        return str(self._tiers[0]["label"])

    def recommend_for_task(
        self,
        hardware: HardwareProfile,
        task_type: str,
    ) -> list[SetupModelRecommendation]:
        """Recommend models optimised for a specific task type.

        Enriches the standard VRAM-tier recommendations with task-specific
        quantization advice from quantization.yaml.  When the preferred quant
        for the task matches a recommendation, the reason string is annotated
        so the user understands why that model was selected.

        Args:
            hardware: Detected hardware profile.
            task_type: Task type string (e.g., ``"coding"``, ``"reasoning"``).

        Returns:
            List of ModelRecommendation objects, primary first, with
            task-aware reason annotations where applicable.
        """
        implied = {
            "vision": Modality.VISION,
            "image_generation": Modality.IMAGE_GENERATION,
            "video_generation": Modality.VIDEO_GENERATION,
            "embedding": Modality.EMBEDDING,
            "reranker": Modality.RERANKER,
            "audio_asr": Modality.AUDIO_ASR,
            "audio_tts": Modality.AUDIO_TTS,
            "audio_understanding": Modality.AUDIO_UNDERSTANDING,
        }.get(task_type)
        if implied is not None:
            return self.recommend_for_modality(implied, hardware)

        base = self.recommend_models(hardware)
        rec = get_quant_recommendation(task_type, hardware.effective_vram_gb or None)

        if not rec:
            return base

        task_rec = rec.get("task_recommendation", {})
        preferred_quant = task_rec.get("preferred", "").lower()
        notes = task_rec.get("notes", "")

        if not preferred_quant:
            return base

        enriched: list[SetupModelRecommendation] = []
        for model in base:
            quant_lower = model.quantization.lower()
            if quant_lower == preferred_quant:
                annotation = f" (recommended quant for {task_type}"
                if notes:
                    annotation += f": {notes}"
                annotation += ")"
                enriched.append(replace(model, reason=model.reason + annotation))
            else:
                enriched.append(model)

        logger.info(
            "Task-aware recommendation for %s: preferred quant=%s, %d models annotated",
            task_type,
            preferred_quant,
            sum(1 for m in enriched if "(recommended quant" in m.reason),
        )
        return enriched

    @staticmethod
    def recommend_portfolio(
        hardware: HardwareProfile,
        available_backends: list[str] | None = None,
        modalities: set[Modality] | None = None,
    ) -> dict[str, list[SetupModelRecommendation]]:
        """Recommend a complete model portfolio organized by use case.

        Vetinari's factory pipeline runs multiple agents in parallel, so users
        benefit from having models at different size tiers:

        - **grunt**: Small, fast models (1-3B) for classification, routing,
          extraction - the bulk of pipeline operations.
        - **worker**: Medium models (7-14B) for coding, review, documentation -
          the main workhorse models.
        - **thinker**: Large models (32-72B+) for deep reasoning, planning,
          architecture - complex tasks that benefit from scale.

        When AM Engine or NIM is available, native formats are preferred for
        models that fit in VRAM. GGUF remains available for
        llama.cpp sidecars, fallback, and CPU-offloaded large models.

        Args:
            hardware: Detected hardware profile.
            available_backends: List of available backends.
            modalities: Optional set of modalities to filter recommendations by;
                ``None`` means no modality filter (all modalities considered).

        Returns:
            Dict mapping use-case role to recommended models.
        """
        backends = _resolve_recommendation_backends(hardware, available_backends)
        vram = _estimate_model_budget_gb(hardware)
        native_backend = next((backend for backend in backends if backend in _NATIVE_BACKENDS), None)
        has_native_backend = False

        portfolio: dict[str, list[SetupModelRecommendation]] = {
            "grunt": [],
            "worker": [],
            "thinker": [],
        }

        # Grunt models: small, low-overhead llama.cpp sidecars are still useful.
        portfolio["grunt"] = [_PORTFOLIO_MODEL_DEFAULTS["grunt_qwen_1_5b_gguf"]]

        # Worker models: main coding/review workhorses
        worker_recs: list[SetupModelRecommendation] = []
        if "llama_cpp" in backends and vram >= 4:
            worker_recs.append(
                replace(_PORTFOLIO_MODEL_DEFAULTS["worker_qwen_coder_7b_gguf"], is_primary=not has_native_backend),
            )
        portfolio["worker"] = worker_recs

        portfolio["thinker"] = _portfolio_thinker_recommendations(
            hardware, backends, native_backend, has_native_backend, vram
        )

        logger.info(
            "Portfolio recommendation: grunt=%d, worker=%d, thinker=%d (vram=%.1fGB, backends=%s)",
            len(portfolio["grunt"]),
            len(portfolio["worker"]),
            len(portfolio["thinker"]),
            vram,
            backends,
        )
        return portfolio

    @staticmethod
    def recommend_for_modality(
        modality: Modality | str,
        hardware: HardwareProfile,
        max_results: int = 5,
    ) -> list[SetupModelRecommendation]:
        """Return ordered modality recommendations that fit the hardware budget.

        Args:
            modality: Modality enum or string key.
            hardware: Detected hardware profile.
            max_results: Maximum recommendations to return.

        Returns:
            Ordered recommendations that fit the detected hardware budget.
        """
        modality_key = modality if isinstance(modality, Modality) else Modality(str(modality))
        vram = _estimate_model_budget_gb(hardware)
        results: list[SetupModelRecommendation] = []
        for entry in _MODALITY_TIERS.get(modality_key, []):
            if not _default_promotion_allowed(entry):
                continue
            loaded = entry.vram_gb_loaded if entry.vram_gb_loaded is not None else entry.size_gb
            if entry.cloud_only or entry.swap_in_for or loaded <= vram:
                results.append(entry)
            if len(results) >= max_results:
                break
        return results

    def recommend_models_multi_format(
        self,
        hardware: HardwareProfile,
        available_backends: list[str] | None = None,
    ) -> list[SetupModelRecommendation]:
        """Recommend models across all formats based on available backends.

        When AM Engine or NIM is available, native model entries that fit in
        VRAM are promoted ahead of GGUF alternatives.

        Ordering priority:
        1. Native models that fit in VRAM
        2. GGUF models for the VRAM tier (versatile, support CPU offload)
        3. CPU-offload GGUF models (larger than VRAM, slower but more capable)

        Args:
            hardware: Detected hardware profile.
            available_backends: List of available backends (e.g. ``["llama_cpp",
                "amw_engine"]``). Defaults to hardware-capable native backends first
                when not specified.

        Returns:
            List of ModelRecommendation objects across all formats.
        """
        backends = _resolve_recommendation_backends(hardware, available_backends)
        gguf_recs = list(self.recommend_models(hardware)) if "llama_cpp" in backends else []

        has_gpu_backend = any(backend in backends for backend in _NATIVE_BACKENDS)

        if not has_gpu_backend:
            # No native GPU backend - just add CPU-offload options if enough RAM.
            result = list(gguf_recs)
            if "llama_cpp" in backends and hardware.ram_gb >= 32:
                vram = _estimate_model_budget_gb(hardware)
                result.extend(m for m in _CPU_OFFLOAD_MODELS if m.size_gb <= (vram + hardware.ram_gb * 0.5))
            return result

        vram = _estimate_model_budget_gb(hardware)

        # Collect native models that fit in VRAM; these get top priority.
        gpu_recs: list[SetupModelRecommendation] = []
        first_native_backend = next((backend for backend in backends if backend in _NATIVE_BACKENDS), None)
        for backend in (backend for backend in backends if backend in _NATIVE_BACKENDS):
            # Legacy native tier catalog was deleted; reuse the maintained VRAM tiers.
            for entry in _VRAM_TIERS:
                if entry["min_vram_gb"] <= vram < entry["max_vram_gb"]:
                    gpu_recs.extend(
                        _native_model_for_backend(m, backend, primary_backend=first_native_backend)
                        for m in entry["models"]
                        if m.size_gb <= vram and _default_promotion_allowed(m)
                    )
                    break

        # Demote GGUF primary flags when native options exist.
        if gpu_recs:
            gguf_recs = [replace(r, is_primary=False) for r in gguf_recs]

        # Build ordered result: GPU-optimized first, then GGUF, then offload
        result: list[SetupModelRecommendation] = []
        result.extend(gpu_recs)
        result.extend(gguf_recs)

        if "llama_cpp" in backends and hardware.ram_gb >= 32:
            for model in _CPU_OFFLOAD_MODELS:
                if model.size_gb <= (vram + hardware.ram_gb * 0.5):
                    result.append(model)

        logger.info(
            "Multi-format recommendations: %d total (%d native, %d GGUF, backends=%s)",
            len(result),
            len(gpu_recs),
            len(gguf_recs),
            backends,
        )
        return result

    @staticmethod
    def suggest_kv_cache_quant(
        hardware: HardwareProfile,
        context_length: int = 8192,
    ) -> str:
        """Suggest a KV cache quantization type based on available VRAM and context length.

        Uses a simple heuristic: estimate how much VRAM the KV cache would consume
        at f16 precision, then downgrade to q8_0 or q4_0 when the budget is tight.
        This is a lightweight recommendation for the setup wizard - production inference
        uses ``VRAMManager.recommend_kv_quant_for_context`` which reads live VRAM state.

        Args:
            hardware: Detected hardware profile with VRAM information.
            context_length: Desired context window in tokens (default: 8192).

        Returns:
            One of "f16", "q8_0", or "q4_0".
        """
        # Bytes per token for f16 KV cache (worst case estimate)
        _F16_BYTES_PER_TOKEN = 2048
        kv_f16_gb = context_length * _F16_BYTES_PER_TOKEN / (1024**3)

        vram = hardware.effective_vram_gb
        if hardware.gpu_vendor == GpuVendor.APPLE and vram == 0:
            vram = round(hardware.ram_gb * 0.75 * 0.9, 1)
        if vram == 0 and hardware.ram_gb > 0:
            vram = round(hardware.ram_gb * 0.4, 1)

        # Assume ~80% of VRAM is already committed to model weights; the rest is for KV cache
        available_for_kv = vram * 0.20

        if kv_f16_gb > available_for_kv * 0.75:
            return "q4_0"
        if kv_f16_gb > available_for_kv * 0.50:
            return "q8_0"
        return "f16"


def _portfolio_thinker_recommendations(
    hardware: HardwareProfile,
    backends: list[str],
    native_backend: str | None,
    has_native_backend: bool,
    vram: float,
) -> list[SetupModelRecommendation]:
    """Return reasoning/planning portfolio recommendations."""
    thinker_recs: list[SetupModelRecommendation] = []
    if "llama_cpp" in backends and vram >= 8:
        thinker_recs.append(
            replace(_PORTFOLIO_MODEL_DEFAULTS["thinker_qwen_14b_gguf"], is_primary=not has_native_backend)
        )
    if "llama_cpp" in backends and hardware.ram_gb >= 32 and vram >= 8:
        thinker_recs.append(_portfolio_cpu_offload_thinker())
    return thinker_recs


def _portfolio_cpu_offload_thinker() -> SetupModelRecommendation:
    """Return the CPU-offloaded large reasoning recommendation."""
    base = _PORTFOLIO_MODEL_DEFAULTS["thinker_qwen_72b_gguf"]
    return SetupModelRecommendation(
        name=base.name,
        repo_id=base.repo_id,
        filename=base.filename,
        size_gb=42.0,
        quantization="Q4_K_M",
        parameter_count="72B",
        reason="Top-tier reasoning - uses VRAM+RAM via CPU offload, slower but best quality",
        model_format="gguf",
        backend="llama_cpp",
        gpu_only=False,
        best_for=("reasoning", "planning", "research", "security", "creative"),
    )


ModelRecommendation = SetupModelRecommendation
