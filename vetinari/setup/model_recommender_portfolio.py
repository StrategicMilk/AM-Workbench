"""Static model portfolio defaults for setup recommendations."""

from __future__ import annotations

from vetinari.setup.model_recommender_types import SetupModelRecommendation

# The 2026-06-09 AM Engine decision removed vLLM/NIM setup backends from defaults.
_PORTFOLIO_MODEL_DEFAULTS: dict[str, SetupModelRecommendation] = {
    "grunt_qwen_1_5b_gguf": SetupModelRecommendation(
        name="Qwen 2.5 1.5B Q4_K_M",
        repo_id="Qwen/Qwen2.5-1.5B-Instruct-GGUF",
        filename="qwen2.5-1.5b-instruct-q4_k_m.gguf",
        size_gb=1.1,
        quantization="Q4_K_M",
        parameter_count="1.5B",
        reason="Fast classification, routing, and extraction on a low-overhead llama.cpp sidecar",
        is_primary=True,
        best_for=("classification", "extraction", "general"),
    ),
    "worker_qwen_coder_7b_gguf": SetupModelRecommendation(
        name="Qwen 2.5 Coder 7B Q4_K_M",
        repo_id="Qwen/Qwen2.5-Coder-7B-Instruct-GGUF",
        filename="qwen2.5-coder-7b-instruct-q4_k_m.gguf",
        size_gb=4.4,
        quantization="Q4_K_M",
        parameter_count="7B",
        reason="Coding workhorse fallback - GGUF for llama.cpp",
        best_for=("coding", "review", "documentation"),
    ),
    "worker_qwen_coder_7b_awq": SetupModelRecommendation(
        name="Qwen 2.5 Coder 7B AWQ",
        repo_id="Qwen/Qwen2.5-Coder-7B-Instruct-AWQ",
        filename="",
        size_gb=5.1,
        quantization="AWQ",
        parameter_count="7B",
        reason="Coding workhorse primary for GPU-native vLLM deployments",
        model_format="awq",
        backend="vllm",
        gpu_only=True,
        best_for=("coding", "review", "documentation"),
    ),
    "thinker_qwen_14b_gguf": SetupModelRecommendation(
        name="Qwen 2.5 14B Q4_K_M",
        repo_id="Qwen/Qwen2.5-14B-Instruct-GGUF",
        filename="qwen2.5-14b-instruct-q4_k_m.gguf",
        size_gb=8.7,
        quantization="Q4_K_M",
        parameter_count="14B",
        reason="Reasoning and planning fallback - GGUF for llama.cpp",
        best_for=("reasoning", "planning", "research"),
    ),
    "thinker_qwen_72b_gguf": SetupModelRecommendation(
        name="Qwen 2.5 72B Q4_K_M (CPU offload)",
        repo_id="bartowski/Qwen2.5-72B-Instruct-GGUF",
        filename="Qwen2.5-72B-Instruct-Q4_K_M.gguf",
        size_gb=42.0,
        quantization="Q4_K_M",
        parameter_count="72B",
        reason="Top-tier reasoning - uses VRAM+RAM via CPU offload, slower but best quality",
        model_format="gguf",
        backend="llama_cpp",
        gpu_only=False,
        best_for=("reasoning", "planning", "research", "security", "creative"),
    ),
}
