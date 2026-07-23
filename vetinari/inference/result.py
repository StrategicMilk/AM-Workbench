"""Inference result contracts and training-corpus safety gates."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class NoCapacityError(RuntimeError):
    """Raised when no compute target can satisfy an inference request."""

    def __init__(
        self,
        message: str = "no compute capacity available",
        *,
        capability: str = "",
        budget_s: float | None = None,
    ) -> None:
        self.capability = capability
        self.budget_s = budget_s
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class InferenceResult:
    """Result returned by an inference tier.

    Attributes:
        text: Generated output text.
        tokens_out: Number of output tokens.
        model_id: Producing model identifier.
        compute_tier: Provenance tag such as ``gpu_llamacpp`` or ``cpu_bonsai``.
        quality_floor: Quality level used for routing.
        is_fallback: Whether the result came from fallback behavior.
        logprobs: Optional token logprob records from the model adapter.
    """

    text: str
    tokens_out: int
    model_id: str
    compute_tier: str
    quality_floor: str
    is_fallback: bool
    logprobs: list[dict[str, Any]] | None = None

    def safe_for_training_corpus(self) -> bool:
        """Return whether this output may enter the training corpus.

        Returns:
            bool value produced by safe_for_training_corpus().
        """
        if self.is_fallback:
            return False
        trusted_tiers = {"gpu_llamacpp", "gpu_vllm", "cloud_verified", "cpu_background", "cpu_bonsai"}
        if self.compute_tier not in trusted_tiers:
            return False
        if self.compute_tier == "cpu_bonsai":
            return self.quality_floor == "premium"
        if self.compute_tier == "cpu_background":
            return self.quality_floor != "draft"
        return True

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"InferenceResult(text={self.text!r}, tokens_out={self.tokens_out!r}, model_id={self.model_id!r})"


__all__ = ["InferenceResult", "NoCapacityError"]
