"""Prompt Evolver - Vetinari Self-Improvement Subsystem.

A/B tests prompt variations for each agent and promotes variants that
achieve statistically better quality scores.
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from pathlib import Path
from typing import Any

from vetinari.learning.prompt_evolver_lifecycle import (
    _get_stats,
    _record_result,
    _register_baseline,
    _select_prompt,
)
from vetinari.learning.prompt_evolver_models import PromptVariant
from vetinari.learning.prompt_evolver_promotion import (
    _check_promotion,
    _check_shadow_test_results,
    _promote_variant_to_method_library,
    _validate_variant_with_benchmark,
)
from vetinari.learning.prompt_evolver_scope import (
    _build_level_hint,
    _evolve_per_level,
    _generate_variant_from_trace,
    _synthesize_scope_guidelines,
)
from vetinari.learning.prompt_evolver_state import load_prompt_variants, prompt_variant_state_path, save_prompt_variants
from vetinari.learning.prompt_evolver_stats import test_significance
from vetinari.learning.prompt_evolver_variants import _generate_variant, _resolve_effective_operator

logger = logging.getLogger(__name__)

__all__ = ["PromptEvolver", "PromptVariant", "get_prompt_evolver", "test_significance"]

# Preserve the historical introspection and pickle path for the re-exported model.
PromptVariant.__module__ = __name__


class PromptEvolver:
    """Automatically evolves agent system prompts through A/B testing.

    Strategy:
    1. Baseline prompt is the current system prompt for each agent.
    2. When quality degrades, generate new variants.
    3. Route a fraction of tasks to variant prompts.
    4. After enough trials, promote variants that pass all gates.
    5. Store all variants and their performance in memory.
    """

    MIN_TRIALS = 30
    VARIANT_FRACTION = 0.2
    MIN_IMPROVEMENT = 0.05
    MIN_EFFECT_SIZE = 0.3
    P_VALUE_THRESHOLD = 0.05
    MAX_CONCURRENT_TESTS = 2
    BENCHMARK_PASS_THRESHOLD = 0.6

    def __init__(self, adapter_manager: Any | None = None, improvement_log: Any | None = None) -> None:
        """Initialize the prompt evolver and load persisted variants.

        Args:
            adapter_manager: Optional inference adapter manager for LLM fallback mutation.
            improvement_log: Optional kaizen improvement log for operator feedback records.
        """
        self._adapter_manager = adapter_manager
        self._variants: dict[str, list[PromptVariant]] = {}
        self._score_history: dict[str, deque[float]] = {}
        self._variant_operators: dict[str, tuple[Any, str, str]] = {}
        self._variant_improvements: dict[str, str] = {}
        self._operator_selector: Any | None = None
        self._prompt_mutator: Any | None = None
        self._improvement_log = improvement_log
        self._lock = threading.Lock()
        self._load_variants()

    @staticmethod
    def _get_state_path() -> Path:
        """Resolve path for the prompt variant state file.

        Returns:
            Path to ``prompt_variants.json``.
        """
        return prompt_variant_state_path()

    def register_baseline(self, agent_type: str, prompt_text: str) -> None:
        """Register the current system prompt as the baseline.

        Args:
            agent_type: Agent type owning the prompt.
            prompt_text: Prompt text to treat as the promoted baseline.
        """
        _register_baseline(self, agent_type, prompt_text)

    def select_prompt(self, agent_type: str) -> tuple[str, str]:
        """Select which prompt to use for this invocation.

        Args:
            agent_type: Agent type requesting a prompt.

        Returns:
            Tuple of prompt text and variant id.
        """
        return _select_prompt(self, agent_type)

    def record_result(self, agent_type: str, variant_id: str, quality: float) -> None:
        """Record a quality result for a variant and trigger promotion checks.

        Args:
            agent_type: Agent type that produced the result.
            variant_id: Variant id that produced the result.
            quality: Quality score to record.
        """
        _record_result(self, agent_type, variant_id, quality)

    def promote_variant_to_method_library(
        self,
        agent_type: str,
        variant_id: str,
        *,
        method_library: Any | None = None,
        project_id: str = "default",
        provenance_ref: str,
        consent_ref: str,
        safety_ref: str,
        confidence: float,
    ) -> Any:
        """Persist a promoted prompt variant as a measured MethodLibrary card.

        Args:
            agent_type: Agent type owning the variant.
            variant_id: Prompt variant id to promote.
            method_library: Optional method-library override for tests.
            project_id: Project scope for the method card.
            provenance_ref: Provenance evidence reference.
            consent_ref: Consent evidence reference.
            safety_ref: Safety evidence reference.
            confidence: Promotion confidence score.

        Returns:
            Stored method card.
        """
        return _promote_variant_to_method_library(
            self,
            agent_type,
            variant_id,
            method_library=method_library,
            project_id=project_id,
            provenance_ref=provenance_ref,
            consent_ref=consent_ref,
            safety_ref=safety_ref,
            confidence=confidence,
        )

    def _check_promotion(self, agent_type: str) -> None:
        """Decide whether to promote, deprecate, or keep testing variants."""
        _check_promotion(self, agent_type)

    @staticmethod
    def _test_significance(baseline_scores: list[float], variant_scores: list[float]) -> tuple[bool, float]:
        """Run the statistical promotion gate for a prompt variant."""
        return test_significance(baseline_scores, variant_scores)

    def check_shadow_test_results(self) -> None:
        """Finalize promotion for variants that passed shadow testing."""
        _check_shadow_test_results(self)

    def _validate_variant_with_benchmark(self, variant: PromptVariant) -> bool:
        """Run a fail-closed benchmark check before promoting a variant."""
        return _validate_variant_with_benchmark(self, variant)

    def generate_variant(self, agent_type: str, baseline_prompt: str, mode: str = "default") -> str | None:
        """Generate an improved prompt variant using structured operators.

        Args:
            agent_type: Agent type whose prompt is being evolved.
            baseline_prompt: Baseline prompt text to mutate.
            mode: Agent mode used for operator selection.

        Returns:
            Generated variant text, or ``None`` when generation fails.
        """
        return _generate_variant(self, agent_type, baseline_prompt, mode)

    def _generate_variant_llm(self, agent_type: str, baseline_prompt: str) -> str | None:
        """Generate a variant using LLM inference fallback."""
        from vetinari.learning.prompt_evolver_generation import generate_variant_llm

        generated = generate_variant_llm(self, agent_type, baseline_prompt)
        return generated if isinstance(generated, str) or generated is None else None

    def generate_variant_from_trace(
        self,
        agent_type: str,
        baseline_prompt: str,
        failed_trace: dict[str, Any],
    ) -> str | None:
        """Generate a targeted prompt variant by diagnosing a failed execution trace.

        Args:
            agent_type: Agent type that produced the failing trace.
            baseline_prompt: Current instruction text to improve.
            failed_trace: Diagnostic trace used by the prompt optimizer.

        Returns:
            Generated variant text, or ``None`` when no improvement is available.
        """
        return _generate_variant_from_trace(self, agent_type, baseline_prompt, failed_trace)

    def evolve_per_level(
        self,
        agent_type: str,
        level: str = "default",
        failed_traces: list[dict[str, Any]] | None = None,
    ) -> dict[str, str | None]:
        """Evolve the prompt for a given agent type and execution-level context.

        Args:
            agent_type: Agent type to evolve.
            level: Execution mode or scope name.
            failed_traces: Optional failed traces used for trace-based diagnosis.

        Returns:
            Dict with ``variant_id`` and ``evolved_prompt`` keys.
        """
        return _evolve_per_level(self, agent_type, level, failed_traces)

    @staticmethod
    def _build_level_hint(agent_type: str, level: str) -> str:
        """Build an agent- and level-specific hint to seed prompt mutation."""
        return _build_level_hint(agent_type, level)

    def synthesize_scope_guidelines(self, agent_type: str, level: str = "default") -> str:
        """Synthesize failure-informed guidelines for an agent in a given scope.

        Args:
            agent_type: Agent whose failure history should be analyzed.
            level: Execution scope used to label the guidelines.

        Returns:
            Guideline text, or ``""`` when there is not enough signal.
        """
        return _synthesize_scope_guidelines(self, agent_type, level)

    @staticmethod
    def _resolve_effective_operator(operator: Any, prompt: str, agent_type: str, mode: str) -> Any:
        """Return an operator that will actually change the given prompt."""
        return _resolve_effective_operator(operator, prompt, agent_type, mode)

    def _get_operator_selector(self) -> Any:
        """Return the shared OperatorSelector singleton."""
        from vetinari.learning.prompt_evolver_generation import get_operator_selector

        return get_operator_selector(self)

    def _get_prompt_mutator(self) -> Any:
        """Lazy-load the PromptMutator singleton."""
        from vetinari.learning.prompt_evolver_generation import get_prompt_mutator

        return get_prompt_mutator(self)

    def _update_operator_feedback(self, variant_id: str, quality_delta: float) -> None:
        """Feed quality delta back to OperatorSelector."""
        from vetinari.learning.prompt_evolver_generation import update_operator_feedback

        update_operator_feedback(self, variant_id, quality_delta)

    def _record_improvement(self, variant_id: str, operator: Any, agent_type: str, mode: str) -> None:
        """Create an ImprovementRecord for a new variant."""
        from vetinari.learning.prompt_evolver_generation import record_improvement

        record_improvement(self, variant_id, operator, agent_type, mode)

    def _get_baseline_quality(self, agent_type: str) -> float:
        """Get current baseline quality for an agent type."""
        from vetinari.learning.prompt_evolver_generation import get_baseline_quality

        return float(get_baseline_quality(self, agent_type))

    def _update_improvement_observation(self, variant_id: str, quality_delta: float) -> None:
        """Record an observation on the linked ImprovementRecord."""
        from vetinari.learning.prompt_evolver_generation import update_improvement_observation

        update_improvement_observation(self, variant_id, quality_delta)

    def get_stats(self, agent_type: str) -> dict[str, Any]:
        """Get evolution statistics for an agent type.

        Args:
            agent_type: Agent type to report on.

        Returns:
            Dict of prompt evolution summary statistics.
        """
        return _get_stats(self, agent_type)

    def _load_variants(self) -> None:
        """Load persisted prompt variants into memory."""
        self._variants.update(load_prompt_variants(PromptVariant, logger))
        for variants in self._variants.values():
            for variant in variants:
                if variant.score_history:
                    self._score_history[variant.variant_id] = deque(variant.score_history[-500:], maxlen=500)

    def _save_variants(self) -> None:
        """Persist prompt variants to disk."""
        save_prompt_variants(self._variants, logger)


_prompt_evolver: PromptEvolver | None = None
_prompt_evolver_lock = threading.Lock()


def get_prompt_evolver() -> PromptEvolver:
    """Return the module-level PromptEvolver singleton, creating it if needed.

    Returns:
        Shared PromptEvolver instance loaded with persisted variant state.
    """
    global _prompt_evolver
    if _prompt_evolver is None:
        with _prompt_evolver_lock:
            if _prompt_evolver is None:
                _prompt_evolver = PromptEvolver()
    return _prompt_evolver
