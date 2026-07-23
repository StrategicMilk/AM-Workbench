"""Lifecycle helpers for prompt variant selection and result recording."""

from __future__ import annotations

import secrets
from collections import deque
from typing import TYPE_CHECKING, Any

from vetinari.learning.prompt_evolver_models import PromptVariant
from vetinari.types import PromptVersionStatus

if TYPE_CHECKING:
    from vetinari.learning.prompt_evolver import PromptEvolver


def _register_baseline(evolver: PromptEvolver, agent_type: str, prompt_text: str) -> None:
    """Register or update the baseline prompt for an agent type."""
    with evolver._lock:
        if agent_type not in evolver._variants:
            evolver._variants[agent_type] = []

        for variant in evolver._variants[agent_type]:
            if variant.is_baseline:
                if variant.prompt_text != prompt_text:
                    variant.prompt_text = prompt_text
                    evolver._save_variants()
                return

        evolver._variants[agent_type].append(
            PromptVariant(
                variant_id=f"{agent_type}_baseline",
                agent_type=agent_type,
                prompt_text=prompt_text,
                is_baseline=True,
                status=PromptVersionStatus.PROMOTED.value,
            )
        )
        evolver._save_variants()


def _select_prompt(evolver: PromptEvolver, agent_type: str) -> tuple[str, str]:
    """Select a prompt and variant id for one agent invocation."""
    with evolver._lock:
        variants = evolver._variants.get(agent_type, [])
        if not variants:
            return "", "none"

        promoted = [variant for variant in variants if variant.status == PromptVersionStatus.PROMOTED.value]
        testing = [variant for variant in variants if variant.status == PromptVersionStatus.TESTING.value]

        if testing and (secrets.randbelow(10_000) / 10_000) < evolver.VARIANT_FRACTION:
            variant = testing[secrets.randbelow(len(testing))]
            return variant.prompt_text, variant.variant_id

        if promoted:
            best = max(promoted, key=lambda variant: variant.avg_quality if variant.trials > 0 else 0.5)
            return best.prompt_text, best.variant_id

        return "", "default"


def _record_result(evolver: PromptEvolver, agent_type: str, variant_id: str, quality: float) -> None:
    """Record a quality result and run promotion/shadow-test maintenance."""
    with evolver._lock:
        variants = evolver._variants.get(agent_type, [])
        for variant in variants:
            if variant.variant_id == variant_id:
                variant.record(quality)
                if variant_id not in evolver._score_history:
                    evolver._score_history[variant_id] = deque(maxlen=500)
                evolver._score_history[variant_id].append(quality)
                variant.score_history = list(evolver._score_history[variant_id])
                evolver._save_variants()
                break

        evolver._check_promotion(agent_type)

    evolver.check_shadow_test_results()


def _get_stats(evolver: PromptEvolver, agent_type: str) -> dict[str, Any]:
    """Build prompt evolution statistics for one agent type."""
    variants = evolver._variants.get(agent_type, [])
    return {
        "agent_type": agent_type,
        "total_variants": len(variants),
        "promoted": [
            {"id": variant.variant_id, "quality": round(variant.avg_quality, 3), "trials": variant.trials}
            for variant in variants
            if variant.status == PromptVersionStatus.PROMOTED.value
        ],
        "testing": len([variant for variant in variants if variant.status == PromptVersionStatus.TESTING.value]),
        "deprecated": len([variant for variant in variants if variant.status == PromptVersionStatus.DEPRECATED.value]),
    }
