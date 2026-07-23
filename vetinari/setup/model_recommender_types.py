"""Shared setup model recommendation data types."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import Enum
from typing import Any


class Modality(str, Enum):
    """Catalog modality dimension used by model recommendations."""

    TEXT = "text"
    VISION = "vision"
    AUDIO_ASR = "audio_asr"
    AUDIO_TTS = "audio_tts"
    AUDIO_UNDERSTANDING = "audio_understanding"
    IMAGE_GENERATION = "image_generation"
    VIDEO_GENERATION = "video_generation"
    EMBEDDING = "embedding"
    RERANKER = "reranker"
    DRAFT_SPECULATIVE = "draft_speculative"
    THREE_D = "three_d"
    CLOUD_OVERFLOW = "cloud_overflow"


@dataclass(frozen=True, slots=True)
class SourceCitation:
    """Fetched source backing a recommendation row."""

    url: str
    retrieved_on: date
    fetched_in_session: bool = True

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dictionary."""
        return {
            "url": self.url,
            "retrieved_on": self.retrieved_on.isoformat(),
            "fetched_in_session": self.fetched_in_session,
        }


@dataclass(frozen=True, slots=True)
class SetupModelRecommendation:
    """A recommended model for a given hardware profile.

    Attributes:
        name: Human-readable model name (e.g. "Llama 3.1 8B Q6_K").
        repo_id: HuggingFace repository ID for download.
        filename: Exact GGUF filename within the repository.
        size_gb: Approximate file size in gigabytes.
        quantization: Quantization level (e.g. "Q4_K_M", "Q6_K", "AWQ").
        parameter_count: Model parameter count (e.g. "7B", "14B").
        reason: Why this model is recommended for the detected hardware.
        is_primary: Whether this is the top recommendation for the tier.
        model_format: Serialization format — "gguf", "safetensors", "awq", or "gptq". Empty
            string means GGUF (the default llama-cpp format).
        backend: Inference backend — "nim", "vllm", or "llama_cpp". Empty string means
            llama_cpp (the default local backend).
        gpu_only: True when the model cannot run on CPU (AWQ/GPTQ via vLLM).
        best_for: Task categories this model excels at (e.g. "coding", "reasoning").
            Empty tuple means general-purpose with no specific strength.
    """

    name: str
    repo_id: str
    filename: str
    size_gb: float
    quantization: str
    parameter_count: str
    reason: str
    is_primary: bool = False
    model_format: str = "gguf"  # gguf, safetensors, awq, gptq
    backend: str = "llama_cpp"  # llama_cpp, vllm, nim
    gpu_only: bool = False  # True = must fit entirely in VRAM (no CPU offload)
    best_for: tuple[str, ...] = ()  # Task types this model excels at (coding, reasoning, etc.)
    modality: Modality = Modality.TEXT
    recommended_backend: str = ""
    recommended_quant: str = ""
    vram_gb_loaded: float | None = None
    host_ram_gb_min: float = 0.0
    verified_on: date | None = None
    sources: tuple[SourceCitation, ...] = ()
    requires_upstream_image: bool = False
    tradeoffs: tuple[str, ...] = ()
    co_residency_notes: str = ""
    swap_in_for: str | None = None
    cloud_only: bool = False

    def __repr__(self) -> str:
        return "ModelRecommendation(...)"

    @property
    def model_id(self) -> str:
        """Return the canonical model identifier used by newer catalog callers."""
        return self.repo_id

    def to_dict(self) -> dict[str, Any]:
        """Serialize to plain dictionary.

        Returns:
            Dictionary representation of the recommendation.
        """
        return {
            "name": self.name,
            "repo_id": self.repo_id,
            "filename": self.filename,
            "size_gb": self.size_gb,
            "quantization": self.quantization,
            "parameter_count": self.parameter_count,
            "reason": self.reason,
            "is_primary": self.is_primary,
            "model_format": self.model_format,
            "backend": self.backend,
            "gpu_only": self.gpu_only,
            "best_for": list(self.best_for),
            "model_id": self.model_id,
            "modality": self.modality.value,
            "recommended_backend": self.recommended_backend or self.backend,
            "recommended_quant": self.recommended_quant or self.quantization,
            "vram_gb_loaded": self.vram_gb_loaded if self.vram_gb_loaded is not None else self.size_gb,
            "host_ram_gb_min": self.host_ram_gb_min,
            "verified_on": self.verified_on.isoformat() if self.sources and self.verified_on else None,
            "sources": [source.to_dict() for source in self.sources],
            "requires_upstream_image": self.requires_upstream_image,
            "tradeoffs": list(self.tradeoffs),
            "co_residency_notes": self.co_residency_notes,
            "swap_in_for": self.swap_in_for,
            "cloud_only": self.cloud_only,
        }


# ── VRAM-to-Model Matrix ─────────────────────────────────────────────────────
# Each tier defines models suitable for a VRAM range.  The first model in each
# tier is the primary recommendation.
