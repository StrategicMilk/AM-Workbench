"""Modality-specific setup model recommendation catalogs."""

from __future__ import annotations

from datetime import date

from vetinari.setup.model_recommender_types import Modality, SetupModelRecommendation, SourceCitation

_VERIFIED_ON = date(2026, 4, 25)
_VLLM_SOURCE = SourceCitation(
    url="https://github.com/vllm-project/vllm/pull/22131",
    retrieved_on=_VERIFIED_ON,
    fetched_in_session=True,
)


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


_QWEN_VL_TRADEOFF = (
    "No official AWQ/GPTQ quants - community AWQ build, vLLM serving requires verification before promotion "
    "(see SESSION-07 capability maturity gate)."
)
_DEVSTRAL_NOTE = (
    "Requires vllm/vllm-openai:>=v0.15.1 (Mistral3ForConditionalGeneration). NVIDIA NGC "
    "nvcr.io/nvidia/vllm:25.09-py3 does NOT support this model class as of 2026-04-25."
)

_MODALITY_TIERS: dict[Modality, list[SetupModelRecommendation]] = {
    Modality.VISION: [
        _rec(
            model_id="Qwen/Qwen3-VL-32B-Instruct",
            name="Qwen3-VL 32B Instruct",
            modality=Modality.VISION,
            backend="vllm",
            quant="awq_int4",
            vram=28.0,
            tradeoffs=(_QWEN_VL_TRADEOFF,),
        ),
        _rec(
            model_id="Qwen/Qwen3-VL-8B-Instruct",
            name="Qwen3-VL 8B Instruct",
            modality=Modality.VISION,
            backend="vllm",
            quant="awq_int4",
            vram=9.0,
        ),
    ],
    Modality.AUDIO_ASR: [
        _rec(
            model_id="openai/whisper-large-v3",
            name="Whisper Large v3",
            modality=Modality.AUDIO_ASR,
            backend="faster_whisper",
            quant="int8_float16",
            vram=4.0,
        ),
    ],
    Modality.AUDIO_TTS: [],
    Modality.AUDIO_UNDERSTANDING: [
        _rec(
            model_id="Qwen/Qwen3-Omni-30B-A3B",
            name="Qwen3 Omni 30B-A3B",
            modality=Modality.AUDIO_UNDERSTANDING,
            backend="vllm",
            quant="awq_int4",
            vram=30.0,
        ),
    ],
    Modality.IMAGE_GENERATION: [
        _rec(
            model_id="black-forest-labs/FLUX.2-dev",
            name="FLUX.2 dev",
            modality=Modality.IMAGE_GENERATION,
            backend="comfyui",
            quant="fp8",
            vram=24.0,
            license="blocked:non-commercial-dev-license",
        ),
        _rec(
            model_id="black-forest-labs/FLUX.1-dev",
            name="FLUX.1 dev",
            modality=Modality.IMAGE_GENERATION,
            backend="comfyui",
            quant="fp8",
            vram=18.0,
        ),
        _rec(
            model_id="stabilityai/stable-diffusion-3.5-large",
            name="Stable Diffusion 3.5 Large",
            modality=Modality.IMAGE_GENERATION,
            backend="comfyui",
            quant="fp16",
            vram=16.0,
        ),
    ],
    Modality.VIDEO_GENERATION: [
        _rec(
            model_id="tencent/HunyuanVideo-1.5",
            name="HunyuanVideo 1.5",
            modality=Modality.VIDEO_GENERATION,
            backend="comfyui",
            quant="fp8",
            vram=30.0,
            license="custom:tencent-proprietary-review-required",
        ),
        _rec(
            model_id="THUDM/CogVideoX-5b",
            name="CogVideoX 5B",
            modality=Modality.VIDEO_GENERATION,
            backend="comfyui",
            quant="fp16",
            vram=14.0,
        ),
        _rec(
            model_id="Wan-AI/Wan2.1-14B",
            name="Wan 2.1 14B",
            modality=Modality.VIDEO_GENERATION,
            backend="comfyui",
            quant="fp16",
            vram=48.0,
            cloud_only=False,
        ),
    ],
    Modality.EMBEDDING: [
        _rec(
            model_id="Qwen/Qwen3-Embedding-8B",
            name="Qwen3 Embedding 8B",
            modality=Modality.EMBEDDING,
            backend="vllm",
            quant="fp16",
            vram=16.0,
        ),
        _rec(
            model_id="nvidia/llama-embed-nemotron-8b",
            name="Llama Embed Nemotron 8B",
            modality=Modality.EMBEDDING,
            backend="nim",
            quant="fp16",
            vram=16.0,
            license="custom:llama-3-community-license",
        ),
    ],
    Modality.RERANKER: [
        _rec(
            model_id="Qwen/Qwen3-Reranker-8B",
            name="Qwen3 Reranker 8B",
            modality=Modality.RERANKER,
            backend="vllm",
            quant="fp16",
            vram=16.0,
        ),
    ],
    Modality.DRAFT_SPECULATIVE: [
        _rec(
            model_id="Qwen/Qwen3-1.7B",
            name="Qwen3 1.7B draft",
            modality=Modality.DRAFT_SPECULATIVE,
            backend="vllm",
            quant="fp16",
            vram=4.0,
            swap_in_for="Qwen/Qwen3.6-35B-A3B",
        ),
    ],
    Modality.THREE_D: [
        _rec(
            model_id="tencent/Hunyuan3D-2.5",
            name="Hunyuan3D 2.5",
            modality=Modality.THREE_D,
            backend="comfyui",
            quant="fp16",
            vram=18.0,
            license="custom:tencent-proprietary-review-required",
        ),
    ],
    Modality.CLOUD_OVERFLOW: [
        _rec(
            model_id="zai-org/GLM-5.1",
            name="GLM 5.1",
            modality=Modality.CLOUD_OVERFLOW,
            backend="openai",
            quant="hosted",
            vram=None,
            cloud_only=True,
        ),
        _rec(
            model_id="deepseek-ai/DeepSeek-V4-Pro",
            name="DeepSeek V4 Pro",
            modality=Modality.CLOUD_OVERFLOW,
            backend="openai",
            quant="hosted",
            vram=None,
            cloud_only=True,
        ),
        _rec(
            model_id="meta-llama/Llama-4-Maverick",
            name="Llama 4 Maverick",
            modality=Modality.CLOUD_OVERFLOW,
            backend="openai",
            quant="hosted",
            vram=None,
            cloud_only=True,
        ),
        _rec(
            model_id="mistralai/Mistral-Small-4",
            name="Mistral Small 4",
            modality=Modality.CLOUD_OVERFLOW,
            backend="openai",
            quant="hosted",
            vram=None,
            cloud_only=True,
        ),
    ],
}

_CURRENT_NATIVE_TEXT_MODELS = [
    _rec(
        model_id="google/gemma-4-26B-A4B-it",
        name="Gemma 4 26B-A4B-it",
        modality=Modality.TEXT,
        backend="vllm",
        quant="awq_int4",
        vram=24.0,
        best_for=("planning",),
    ),
    _rec(
        model_id="Qwen/Qwen3.6-35B-A3B",
        name="Qwen3.6 35B-A3B",
        modality=Modality.TEXT,
        backend="vllm",
        quant="gptq_int4",
        vram=30.0,
        best_for=("agentic",),
    ),
    _rec(
        model_id="mistralai/Devstral-Small-2-24B-Instruct-2512",
        name="Devstral Small 2 24B",
        modality=Modality.TEXT,
        backend="vllm",
        quant="awq_int4",
        vram=22.0,
        best_for=("coding",),
        tradeoffs=(_DEVSTRAL_NOTE,),
        requires_upstream_image=True,
        license="review-required:mistral-ai-model-license",
    ),
    _rec(
        model_id="deepseek-ai/DeepSeek-R1-Distill-Qwen-32B",
        name="DeepSeek R1 Distill Qwen 32B",
        modality=Modality.TEXT,
        backend="vllm",
        quant="gptq_int4",
        vram=29.0,
        best_for=("deep_audit",),
    ),
    _rec(
        model_id="Qwen/Qwen3-8B-Instruct",
        name="Qwen3 8B Instruct",
        modality=Modality.TEXT,
        backend="vllm",
        quant="awq_int4",
        vram=8.0,
        best_for=("fast_check",),
        license="blocked:primary-source-inaccessible-2026-05-08",
    ),
]
