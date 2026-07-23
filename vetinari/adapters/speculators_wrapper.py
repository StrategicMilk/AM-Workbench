"""Speculative decoding configuration helper for vLLM adapters."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SpeculativePair:
    """Draft/target pair used for speculative decoding."""

    target_id: str
    draft_id: str
    method: str = "eagle3"
    num_speculative_tokens: int = 4

    def __repr__(self) -> str:
        return (
            "SpeculativePair("
            f"target_id={self.target_id!r}, draft_id={self.draft_id!r}, "
            f"method={self.method!r}, num_speculative_tokens={self.num_speculative_tokens!r})"
        )


def build_speculative_config(pair: SpeculativePair, *, tokenizer_compatible: bool = True) -> dict[str, object]:
    """Build vLLM speculative-decoding parameters for a validated pair.

    Returns:
        Dict of vLLM speculative-decoding parameters.

    Raises:
        ValueError: If the draft and target tokenizers are incompatible.
    """
    if not tokenizer_compatible:
        raise ValueError(f"Tokenizer mismatch between {pair.target_id} and {pair.draft_id}")
    return {
        "provider_type": "vllm",
        "speculative_model": pair.draft_id,
        "target_model": pair.target_id,
        "speculative_method": pair.method,
        "num_speculative_tokens": pair.num_speculative_tokens,
    }
