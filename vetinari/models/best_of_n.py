"""Best-of-N selection with verifier scoring.

Generates N candidate responses for a prompt and returns the highest-scoring
candidate according to a caller-supplied scorer function.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


# Default number of candidates to generate per service tier.
_DEFAULT_N_BY_TIER: dict[str, int] = {
    "custom": 4,  # High-effort tier: broadest candidate pool
    "standard": 2,  # Mid-tier: balanced cost vs quality
    "express": 1,  # Low-latency tier: passthrough, no scoring overhead
}

_STANDARD_TIER_TASK_TYPES: frozenset[str] = frozenset({
    "analysis",
    "general",
    "implementation",
    "planning",
    "research",
    "review",
})


def get_n_for_tier(tier: str) -> int:
    """Return the default candidate count N for a given service tier.

    Args:
        tier: Service tier name. Recognised values are ``"custom"``,
            ``"standard"``, and ``"express"``. Known production task-type
            labels such as ``"analysis"`` and ``"implementation"`` resolve
            to the standard tier. Unrecognised labels fall back to ``1``
            (passthrough) so callers never receive an error for unknown tier
            names.

    Returns:
        Integer candidate count N for the tier.
    """
    if not isinstance(tier, str):
        # Non-string tier (None, int, etc.) — return safe passthrough value rather than crashing.
        logger.debug("get_n_for_tier received non-string tier %r — returning 1 (passthrough)", tier)
        return 1
    normalised = tier.strip().lower()
    service_tier = "standard" if normalised in _STANDARD_TIER_TASK_TYPES else normalised
    n = _DEFAULT_N_BY_TIER.get(service_tier, 1)
    logger.debug("Tier %r maps to N=%d candidates", tier, n)
    return n


@dataclass(frozen=True, slots=True)
class SelectionResult:
    """Holds the winner and scoring metadata from a Best-of-N run.

    Attributes:
        candidate: The highest-scoring generated candidate string.
        score: Quality score (0.0-1.0) assigned by the scorer.
        n_generated: Total number of candidates that were generated.
    """

    candidate: str
    score: float
    n_generated: int


@dataclass(frozen=True, slots=True)
class BestOfNSelector:
    """Generates N candidates for a prompt and returns the highest-scoring one.

    The selector is intentionally decoupled from any LLM: callers supply
    both a generation function and a scoring function so this class can
    be tested and reused without network access.

    Attributes:
        generate_fn: Callable that accepts a prompt string and returns a
            single candidate string. Called N times per selection request.

    Example:
        >>> selector = BestOfNSelector(generate_fn=my_llm_call)
        >>> best = selector.generate_and_select(prompt, n=4, scorer=my_scorer)
    """

    generate_fn: Callable[[str], str]

    def generate_and_select(
        self,
        prompt: str,
        n: int,
        scorer: Callable[[str], float],
        tie_breaker: Callable[[str], tuple[Any, ...]] | None = None,
    ) -> str:
        """Generate N candidates and return the one with the highest score.

        When ``n`` is 1 the single candidate is returned immediately without
        invoking the scorer, eliminating unnecessary overhead for express-tier
        requests.

        Args:
            prompt: The input prompt passed to ``generate_fn`` for each
                candidate generation call.
            n: Number of candidates to generate. Must be >= 1.
            scorer: Callable that accepts a candidate string and returns a
                float quality score in the range 0.0-1.0. Higher is better.
            tie_breaker: Optional deterministic key used when scores are
                equal. Higher keys win. When omitted, equal scores keep the
                first generated candidate.

        Returns:
            The candidate string with the highest scorer value. When multiple
            candidates share the highest score the first one encountered is
            returned.

        Raises:
            ValueError: If ``n`` is less than 1.
        """
        if n < 1:
            raise ValueError(f"n must be >= 1, got {n}")

        logger.debug("Generating %d candidate(s) for prompt (len=%d)", n, len(prompt))

        if n == 1:
            candidate = self.generate_fn(prompt)
            logger.debug("N=1 passthrough — returning single candidate without scoring")
            return candidate

        candidates: list[str] = []
        for i in range(n):
            candidate = self.generate_fn(prompt)
            candidates.append(candidate)
            logger.debug("Generated candidate %d/%d", i + 1, n)

        best_candidate = candidates[0]
        best_score = scorer(candidates[0])
        best_key = tie_breaker(candidates[0]) if tie_breaker is not None else ()
        logger.debug("Candidate 1/%d scored %.4f", n, best_score)

        for idx, candidate in enumerate(candidates[1:], start=2):
            score = scorer(candidate)
            logger.debug("Candidate %d/%d scored %.4f", idx, n, score)
            key = tie_breaker(candidate) if tie_breaker is not None else ()
            if score > best_score or (score == best_score and tie_breaker is not None and key > best_key):
                best_score = score
                best_candidate = candidate
                best_key = key

        logger.info(
            "Best-of-%d selection complete — winner score=%.4f",
            n,
            best_score,
        )
        return best_candidate
