"""Pure model-profile calculation helpers."""

from __future__ import annotations

import re

from vetinari.models.model_profiler_schemas import FAMILY_PATTERNS as _FAMILY_PATTERNS
from vetinari.models.model_profiler_schemas import GPU_SAFETY_MARGIN as _GPU_SAFETY_MARGIN
from vetinari.models.model_profiler_schemas import KV_BYTES_PER_TOKEN as _KV_BYTES_PER_TOKEN
from vetinari.models.model_profiler_schemas import RUNTIME_OVERHEAD_GB as _RUNTIME_OVERHEAD_GB


def detect_family(architecture: str) -> str:
    """Map a GGUF general.architecture string to a canonical model family.

    Args:
        architecture: The architecture string from GGUF metadata
            (e.g. ``"llama"``, ``"qwen2"``, ``"gpt2"``).

    Returns:
        Canonical family name, or ``"unknown"`` if not recognized.
    """
    lower = architecture.lower()
    for pattern, family in _FAMILY_PATTERNS:
        if re.search(pattern, lower):
            return str(family)
    return "unknown"


def calculate_optimal_context(
    free_vram_gb: float,
    model_vram_gb: float,
    kv_per_token: float,
    trained_limit: int,
) -> int:
    """Calculate the optimal context length that fits in available VRAM.

    Formula: ``n_ctx = (free_vram - model_vram - overhead) / kv_per_token``,
    capped at the model's trained context limit.

    Args:
        free_vram_gb: Available VRAM in GB after other allocations.
        model_vram_gb: VRAM consumed by the model weights.
        kv_per_token: Bytes of KV cache per token (depends on quant type
            and number of KV heads).
        trained_limit: Maximum context length the model was trained on.

    Returns:
        Optimal context length in tokens, minimum 2048.
    """
    available_gb = free_vram_gb - model_vram_gb - _RUNTIME_OVERHEAD_GB
    if available_gb <= 0:
        return min(2048, trained_limit) if trained_limit > 0 else 2048

    available_bytes = available_gb * (1024**3)
    if kv_per_token <= 0:
        kv_per_token = 1.0  # Conservative fallback

    n_ctx = int(available_bytes / kv_per_token)

    # Cap at trained limit
    if trained_limit > 0:
        n_ctx = min(n_ctx, trained_limit)

    aligned = max(2048, (n_ctx // 256) * 256)
    if trained_limit > 0:
        return min(aligned, trained_limit)
    return aligned


def calculate_gpu_layers(
    free_vram_gb: float,
    bytes_per_layer: float,
    num_layers: int,
    kv_reserve_gb: float = 0.0,
    expert_count: int = 0,
    expert_used_count: int = 0,
) -> int:
    """Calculate how many layers can fit on GPU, with MoE adjustment.

    For MoE models, the effective VRAM cost per layer is adjusted by the
    ratio of used experts to total experts:
    ``effective_cost = cost * (0.4 + 0.6 * used / total)``.

    Args:
        free_vram_gb: Available VRAM in GB.
        bytes_per_layer: VRAM cost per layer in bytes.
        num_layers: Total number of layers in the model.
        kv_reserve_gb: VRAM reserved for KV cache.
        expert_count: Total number of experts (0 for non-MoE).
        expert_used_count: Number of experts activated per token.

    Returns:
        Number of layers to place on GPU. -1 means all layers fit.
    """
    available_gb = free_vram_gb * (1 - _GPU_SAFETY_MARGIN) - kv_reserve_gb - _RUNTIME_OVERHEAD_GB
    if available_gb <= 0:
        return 0

    available_bytes = available_gb * (1024**3)

    # MoE adjustment: only active experts consume full compute per layer
    effective_bytes_per_layer = bytes_per_layer
    if expert_count > 1 and expert_used_count > 0:
        moe_factor = 0.4 + 0.6 * (expert_used_count / expert_count)
        effective_bytes_per_layer = bytes_per_layer * moe_factor

    if effective_bytes_per_layer <= 0:
        return -1  # Cannot estimate; offload all

    layers_fit = int(available_bytes / effective_bytes_per_layer)
    if layers_fit >= num_layers:
        return -1  # All layers fit on GPU

    return max(0, layers_fit)


def estimate_kv_per_token(
    head_count_kv: int,
    embedding_length: int,
    head_count: int,
    kv_quant: str = "q8_0",
) -> float:
    """Estimate KV cache bytes per token based on model architecture.

    Formula: ``2 * num_kv_heads * head_dim * bytes_per_element``
    (factor of 2 for K + V caches).

    Args:
        head_count_kv: Number of key-value attention heads.
        embedding_length: Model embedding dimension.
        head_count: Number of attention heads (used to derive head_dim).
        kv_quant: Quantization type for KV cache (default q8_0).

    Returns:
        Bytes per token for the KV cache.
    """
    if head_count <= 0 or head_count_kv <= 0:
        return 512.0  # Conservative fallback

    head_dim = embedding_length / head_count if head_count > 0 else 128
    bytes_per_element = _KV_BYTES_PER_TOKEN.get(kv_quant, 1.0)

    # K cache + V cache = 2 * kv_heads * head_dim * bytes
    return float(2 * head_count_kv * head_dim * bytes_per_element)


# ── Unknown-family learning protocol ─────────────────────────────────────────
