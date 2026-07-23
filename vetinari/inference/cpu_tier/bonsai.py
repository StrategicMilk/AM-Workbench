"""Bonsai classifier wrapper for grammar-constrained enum classification."""

from __future__ import annotations

import logging

from vetinari.inference.constrained import ConstrainedDecoder, build_enum_grammar
from vetinari.inference.cpu_tier import CpuTierInterface
from vetinari.inference.request import RoutedInferenceRequest

logger = logging.getLogger(__name__)


class BonsaiClassifier:
    """Small-model classifier with confidence-margin escalation."""

    def __init__(self, cpu_tier: CpuTierInterface, config: dict) -> None:
        self._cpu_tier = cpu_tier
        self._decoder = ConstrainedDecoder(cpu_tier)
        self._escalation_margin = float(config.get("escalation_margin", 0.15))

    def classify(self, request: RoutedInferenceRequest, allowed_values: list[str]) -> tuple[str, bool]:
        """Return the best enum value and whether the caller should escalate.

        Args:
            request: Request object sent through the operation.
            allowed_values: Value processed by the operation.

        Returns:
            tuple[str, bool] value produced by classify().
        """
        build_enum_grammar(allowed_values)
        result = self._decoder.complete(request, allowed_values)
        value = result.text.strip()
        if not result.logprobs:
            logger.warning("bonsai logprobs unavailable; escalating to 7B by default")
            return value, True
        if len(result.logprobs) < 2:
            logger.warning("bonsai logprobs unavailable; escalating to 7B by default")
            return value, True
        top_two = sorted((float(item["logprob"]) for item in result.logprobs), reverse=True)[:2]
        margin = abs(top_two[0] - top_two[1])
        return value, margin < self._escalation_margin


__all__ = ["BonsaiClassifier"]
