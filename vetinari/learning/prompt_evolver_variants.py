"""Variant generation helpers for PromptEvolver."""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any

from vetinari.learning.prompt_evolver_models import PromptVariant
from vetinari.learning.prompt_mutator import MutationOperator

if TYPE_CHECKING:
    from vetinari.learning.prompt_evolver import PromptEvolver

logger = logging.getLogger(__name__)


def _generate_variant(
    evolver: PromptEvolver,
    agent_type: str,
    baseline_prompt: str,
    mode: str = "default",
) -> str | None:
    """Generate an improved prompt variant using structured operators."""
    try:
        selector = evolver._get_operator_selector()
        mutator = evolver._get_prompt_mutator()

        operator = selector.select_operator(agent_type, mode)
        operator = evolver._resolve_effective_operator(operator, baseline_prompt, agent_type, mode)
        mutated = mutator.mutate(baseline_prompt, operator)

        if isinstance(mutated, str) and mutated != baseline_prompt:
            with evolver._lock:
                variant_id = f"{agent_type}_v{len(evolver._variants.get(agent_type, [])) + 1}"
                variant = PromptVariant(
                    variant_id=variant_id,
                    agent_type=agent_type,
                    prompt_text=mutated,
                )
                evolver._variant_operators[variant_id] = (operator, agent_type, mode)
                if agent_type not in evolver._variants:
                    evolver._variants[agent_type] = []
                evolver._variants[agent_type].append(variant)
                evolver._save_variants()
            logger.info(
                "[PromptEvolver] Generated variant %s for %s via %s operator",
                variant_id,
                agent_type,
                operator.value,
            )
            evolver._record_improvement(variant_id, operator, agent_type, mode)
            return mutated
    except Exception:
        logger.warning("Operator-based variant generation failed", exc_info=True)

    return evolver._generate_variant_llm(agent_type, baseline_prompt)


def _resolve_effective_operator(operator: Any, prompt: str, agent_type: str, mode: str) -> Any:
    """Return an operator that will actually change the given prompt."""
    section_dependent = {
        MutationOperator.FORMAT_RESTRUCTURE,
        MutationOperator.CONTEXT_PRUNE,
    }
    if operator not in section_dependent:
        return operator

    section_count = len(re.findall(r"^#{1,3}\s+", prompt, re.MULTILINE))
    required = 2 if operator == MutationOperator.FORMAT_RESTRUCTURE else 3
    if section_count >= required:
        return operator

    substitute = MutationOperator.CONSTRAINT_INJECTION
    logger.debug(
        "Operator %s is a no-op for %s/%s (sections=%d, required=%d) -- substituting %s",
        operator.value,
        agent_type,
        mode,
        section_count,
        required,
        substitute.value,
    )
    return substitute
