"""Static setup model recommendation catalogs."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from vetinari.setup.model_recommender_modality_catalog import (
    _DEVSTRAL_NOTE as _IMPORTED_DEVSTRAL_NOTE,
)
from vetinari.setup.model_recommender_modality_catalog import (
    _MODALITY_TIERS as _IMPORTED_MODALITY_TIERS,
)
from vetinari.setup.model_recommender_modality_catalog import (
    _QWEN_VL_TRADEOFF as _IMPORTED_QWEN_VL_TRADEOFF,
)
from vetinari.setup.model_recommender_modality_catalog import (
    _VLLM_SOURCE as _IMPORTED_VLLM_SOURCE,
)
from vetinari.setup.model_recommender_types import Modality, SetupModelRecommendation, SourceCitation

_DEVSTRAL_NOTE = _IMPORTED_DEVSTRAL_NOTE
_MODALITY_TIERS = _IMPORTED_MODALITY_TIERS
_QWEN_VL_TRADEOFF = _IMPORTED_QWEN_VL_TRADEOFF
_VLLM_SOURCE = _IMPORTED_VLLM_SOURCE

_VERIFIED_ON = date(2026, 4, 25)
MODEL_RECOMMENDER_CATALOG_REFRESH_INTERVAL_DAYS = 90
_REFRESH_AFTER = _VERIFIED_ON + timedelta(days=MODEL_RECOMMENDER_CATALOG_REFRESH_INTERVAL_DAYS)


def _sources_for(model_id: str, backend: str) -> tuple[SourceCitation, ...]:
    sources = [
        SourceCitation(
            url=f"https://huggingface.co/{model_id}",
            retrieved_on=_VERIFIED_ON,
            fetched_in_session=True,
        )
    ]
    if backend == "vllm":
        sources.append(_VLLM_SOURCE)
    return tuple(sources)


def catalog_refresh_due_on() -> date:
    """Return the date when the static model recommendation catalog must be refreshed."""
    return _REFRESH_AFTER


def catalog_is_stale(as_of: date | None = None) -> bool:
    """Return True once static recommendation evidence has exceeded its freshness window.

    Returns:
        Value produced for the caller.
    """
    checked_at = as_of or date.today()
    return checked_at > _REFRESH_AFTER


def _rec(
    *,
    model_id: str,
    name: str,
    modality: Modality,
    backend: str,
    quant: str,
    vram: float | None,
    license: str = "see-upstream",
    reason: str = "Verified 2026-04-25 catalog recommendation.",
    best_for: tuple[str, ...] = (),
    tradeoffs: tuple[str, ...] = (),
    requires_upstream_image: bool = False,
    cloud_only: bool = False,
    swap_in_for: str | None = None,
) -> SetupModelRecommendation:
    """Build a sourced modality recommendation row."""
    return SetupModelRecommendation(
        name=name,
        repo_id=model_id,
        filename="",
        size_gb=vram or 0.0,
        quantization=quant,
        parameter_count="",
        reason=reason,
        model_format="safetensors",
        backend=backend,
        gpu_only=not cloud_only,
        best_for=best_for,
        modality=modality,
        recommended_backend=backend,
        recommended_quant=quant,
        vram_gb_loaded=vram,
        host_ram_gb_min=16.0,
        verified_on=_VERIFIED_ON,
        sources=_sources_for(model_id, backend),
        requires_upstream_image=requires_upstream_image,
        tradeoffs=tradeoffs,
        cloud_only=cloud_only,
        swap_in_for=swap_in_for,
        co_residency_notes=f"license={license}",
    )


_VRAM_TIERS: list[dict[str, Any]] = [
    {
        "min_vram_gb": 0.0,
        "max_vram_gb": 4.0,
        "label": "CPU-only / < 4 GB VRAM",
        "models": [
            SetupModelRecommendation(
                name="Qwen 2.5 1.5B Q4_K_M",
                repo_id="Qwen/Qwen2.5-1.5B-Instruct-GGUF",
                filename="qwen2.5-1.5b-instruct-q4_k_m.gguf",
                size_gb=1.1,
                quantization="Q4_K_M",
                parameter_count="1.5B",
                reason="Smallest capable model - fits in RAM for CPU inference",
                is_primary=True,
            ),
            SetupModelRecommendation(
                name="TinyLlama 1.1B Q4_K_M",
                repo_id="TheBloke/TinyLlama-1.1B-Chat-v1.0-GGUF",
                filename="tinyllama-1.1b-chat-v1.0.Q4_K_M.gguf",
                size_gb=0.7,
                quantization="Q4_K_M",
                parameter_count="1.1B",
                reason="Ultra-lightweight fallback for very constrained systems",
            ),
        ],
    },
    {
        "min_vram_gb": 4.0,
        "max_vram_gb": 8.0,
        "label": "4-8 GB VRAM",
        "models": [
            SetupModelRecommendation(
                name="Qwen 2.5 7B Q4_K_M",
                repo_id="Qwen/Qwen2.5-7B-Instruct-GGUF",
                filename="qwen2.5-7b-instruct-q4_k_m.gguf",
                size_gb=4.4,
                quantization="Q4_K_M",
                parameter_count="7B",
                reason="Best quality-per-VRAM at 4-bit quantization for 8GB cards",
                is_primary=True,
            ),
            SetupModelRecommendation(
                name="Mistral 7B v0.3 Q4_K_M",
                repo_id="bartowski/Mistral-7B-Instruct-v0.3-GGUF",
                filename="Mistral-7B-Instruct-v0.3-Q4_K_M.gguf",
                size_gb=4.1,
                quantization="Q4_K_M",
                parameter_count="7B",
                reason="Strong instruction following, wide community support",
            ),
        ],
    },
    {
        "min_vram_gb": 8.0,
        "max_vram_gb": 16.0,
        "label": "8-16 GB VRAM",
        "models": [
            SetupModelRecommendation(
                name="Qwen 2.5 7B Q6_K",
                repo_id="Qwen/Qwen2.5-7B-Instruct-GGUF",
                filename="qwen2.5-7b-instruct-q6_k.gguf",
                size_gb=6.0,
                quantization="Q6_K",
                parameter_count="7B",
                reason="Higher quantization - better output quality with 12+ GB VRAM",
                is_primary=True,
            ),
            SetupModelRecommendation(
                name="Llama 3.1 8B Q6_K",
                repo_id="bartowski/Meta-Llama-3.1-8B-Instruct-GGUF",
                filename="Meta-Llama-3.1-8B-Instruct-Q6_K.gguf",
                size_gb=6.6,
                quantization="Q6_K",
                parameter_count="8B",
                reason="Excellent code generation, strong reasoning",
            ),
        ],
    },
    {
        "min_vram_gb": 16.0,
        "max_vram_gb": 24.0,
        "label": "16-24 GB VRAM",
        "models": [
            SetupModelRecommendation(
                name="Qwen 2.5 14B Q4_K_M",
                repo_id="Qwen/Qwen2.5-14B-Instruct-GGUF",
                filename="qwen2.5-14b-instruct-q4_k_m.gguf",
                size_gb=8.7,
                quantization="Q4_K_M",
                parameter_count="14B",
                reason="14B parameters at 4-bit - substantial quality jump over 7B",
                is_primary=True,
            ),
            SetupModelRecommendation(
                name="Codestral 22B Q4_K_M",
                repo_id="bartowski/Codestral-22B-v0.1-GGUF",
                filename="Codestral-22B-v0.1-Q4_K_M.gguf",
                size_gb=12.9,
                quantization="Q4_K_M",
                parameter_count="22B",
                reason="Specialist coding model, excellent for code-heavy workloads",
            ),
        ],
    },
    {
        "min_vram_gb": 24.0,
        "max_vram_gb": 999.0,
        "label": "24+ GB VRAM",
        "models": [
            SetupModelRecommendation(
                name="Qwen 2.5 14B Q6_K",
                repo_id="Qwen/Qwen2.5-14B-Instruct-GGUF",
                filename="qwen2.5-14b-instruct-q6_k.gguf",
                size_gb=12.0,
                quantization="Q6_K",
                parameter_count="14B",
                reason="14B at 6-bit - best quality for 24GB+ cards",
                is_primary=True,
            ),
            SetupModelRecommendation(
                name="Qwen 2.5 32B Q4_K_M",
                repo_id="Qwen/Qwen2.5-32B-Instruct-GGUF",
                filename="qwen2.5-32b-instruct-q4_k_m.gguf",
                size_gb=19.8,
                quantization="Q4_K_M",
                parameter_count="32B",
                reason="32B parameters - top-tier reasoning and instruction following",
            ),
        ],
    },
]


# CPU Offload Models (llama-cpp only, GGUF, larger than VRAM)
# These models are too large for most GPUs but can run via llama-cpp's CPU
# offload (partial GPU layers + RAM).  Slower but dramatically more capable.

_CPU_OFFLOAD_MODELS: list[SetupModelRecommendation] = [
    SetupModelRecommendation(
        name="Qwen 2.5 72B Q4_K_M (CPU offload)",
        repo_id="bartowski/Qwen2.5-72B-Instruct-GGUF",
        filename="Qwen2.5-72B-Instruct-Q4_K_M.gguf",
        size_gb=42.0,
        quantization="Q4_K_M",
        parameter_count="72B",
        reason="72B model - requires VRAM+RAM split via llama-cpp CPU offload, slower but top-tier reasoning",
        model_format="gguf",
        backend="llama_cpp",
        gpu_only=False,
    ),
    SetupModelRecommendation(
        name="Llama 3.3 70B Q4_K_M (CPU offload)",
        repo_id="bartowski/Llama-3.3-70B-Instruct-GGUF",
        filename="Llama-3.3-70B-Instruct-Q4_K_M.gguf",
        size_gb=40.0,
        quantization="Q4_K_M",
        parameter_count="70B",
        reason="70B flagship - requires CPU offload, excellent for complex reasoning",
        model_format="gguf",
        backend="llama_cpp",
        gpu_only=False,
    ),
]
