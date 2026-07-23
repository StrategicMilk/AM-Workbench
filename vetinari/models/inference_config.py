"""Inference configuration and budget forcing for LLM calls.

Implements s1-style budget forcing: thinking token budgets are assigned per
tier (Express / Standard / Custom) and scaled by task complexity (1-10).
The singleton ``BudgetPolicy`` is the central access point for all inference
parameter decisions.

Usage::

    from vetinari.models.inference_config import get_budget_policy

    policy = get_budget_policy()
    cfg = policy.get_config("standard", complexity=7)
    # cfg.thinking_budget -> 4096
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass

logger = logging.getLogger(__name__)


# ── Constants ──────────────────────────────────────────────────────────────────

# Base thinking-token budgets per tier (s1-style defaults).
_DEFAULT_TIER_BUDGETS: dict[str, int] = {
    "express": 1024,
    "standard": 4096,
    "custom": 16384,
}

# Complexity bands determine the budget multiplier applied on top of tier base.
# Bands are inclusive: [low_min, low_max], [mid_min, mid_max], [high_min, high_max]
_LOW_COMPLEXITY_MAX: int = 3  # complexity 1-3 → 50 % of base
_HIGH_COMPLEXITY_MIN: int = 8  # complexity 8-10 → 150 % of base

_DEFAULT_MAX_TOKENS: int = 4096
_DEFAULT_TEMPERATURE: float = 0.7

# ── Dataclasses ────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ResolvedInferenceConfig:
    """Resolved inference parameters for a single LLM call.

    Attributes:
        thinking_budget: Maximum number of thinking tokens allowed, or None to
            disable extended thinking entirely.
        max_tokens: Maximum number of output tokens, or None to use the model
            default.
        temperature: Sampling temperature in [0.0, 2.0].
        tier: The tier label this config was derived from (e.g. "standard").
    """

    thinking_budget: int | None
    max_tokens: int | None
    temperature: float
    tier: str

    def __repr__(self) -> str:
        """Show key identifying fields for debugging."""
        return (
            f"InferenceConfig(tier={self.tier!r},"
            f" thinking_budget={self.thinking_budget!r},"
            f" max_tokens={self.max_tokens!r})"
        )


# ── BudgetPolicy ───────────────────────────────────────────────────────────────


class BudgetPolicy:
    """Maps (tier, complexity) pairs to fully resolved ``InferenceConfig`` objects.

    Tier budget thresholds and per-call defaults are configurable at construction
    time; if omitted the class falls back to hardcoded defaults so it is always
    safe to call without external configuration.

    Args:
        tier_budgets: Mapping of tier name (lowercase) to base thinking-token
            budget.  Keys must include at least ``express``, ``standard``, and
            ``custom``.  Any missing key falls back to the hardcoded default.
        max_tokens: Default max output tokens applied to all returned configs.
        temperature: Default temperature applied to all returned configs.
    """

    def __init__(
        self,
        tier_budgets: dict[str, int] | None = None,
        max_tokens: int | None = _DEFAULT_MAX_TOKENS,
        temperature: float = _DEFAULT_TEMPERATURE,
    ) -> None:
        """Initialise the policy with optional overrides for tier budgets."""
        merged: dict[str, int] = dict(_DEFAULT_TIER_BUDGETS)
        if tier_budgets:
            merged.update({k.lower(): v for k, v in tier_budgets.items()})
        self._tier_budgets: dict[str, int] = merged
        self._max_tokens: int | None = max_tokens
        self._temperature: float = temperature
        logger.info(
            "BudgetPolicy initialised with tier budgets: %s",
            self._tier_budgets,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_config(self, tier: str, complexity: int) -> ResolvedInferenceConfig:
        """Return an ``InferenceConfig`` for the given tier and complexity.

        The thinking budget is computed as:
        - complexity 1-3 (low):  base_budget * 0.5
        - complexity 4-7 (mid):  base_budget * 1.0
        - complexity 8-10 (high): base_budget * 1.5

        Unknown tier names fall back to the "standard" budget with a warning.
        Complexity values outside [1, 10] are clamped silently.

        Args:
            tier: One of ``"express"``, ``"standard"``, or ``"custom"``
                (case-insensitive).  Unknown values trigger a warning and use
                the "standard" fallback.
            complexity: Task complexity score in the range 1-10 (inclusive).

        Returns:
            A fully populated ``InferenceConfig`` with scaled thinking budget.
        """
        normalised_tier = tier.lower().strip()
        if normalised_tier not in self._tier_budgets:
            logger.warning("Unknown tier %r — falling back to 'standard'", tier)
            normalised_tier = "standard"

        base_budget = self._tier_budgets[normalised_tier]
        clamped = max(1, min(10, complexity))
        multiplier = self._complexity_multiplier(clamped)
        scaled_budget = int(base_budget * multiplier)

        logger.debug(
            "tier=%s complexity=%d base=%d multiplier=%.1f scaled=%d",
            normalised_tier,
            clamped,
            base_budget,
            multiplier,
            scaled_budget,
        )

        return ResolvedInferenceConfig(
            thinking_budget=scaled_budget,
            max_tokens=self._max_tokens,
            temperature=self._temperature,
            tier=normalised_tier,
        )

    def available_tiers(self) -> list[str]:
        """Return the list of configured tier names.

        Returns:
            Sorted list of lowercase tier name strings.
        """
        return sorted(self._tier_budgets.keys())

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _complexity_multiplier(complexity: int) -> float:
        """Map a clamped complexity score to a budget multiplier.

        Args:
            complexity: Integer in [1, 10].

        Returns:
            0.5 for low complexity, 1.0 for mid, 1.5 for high.
        """
        if complexity <= _LOW_COMPLEXITY_MAX:
            return 0.5
        if complexity >= _HIGH_COMPLEXITY_MIN:
            return 1.5
        return 1.0


# ── Singleton accessor ─────────────────────────────────────────────────────────

_policy_lock = threading.Lock()
_policy_instance: BudgetPolicy | None = None


def get_budget_policy(
    tier_budgets: dict[str, int] | None = None,
    max_tokens: int | None = _DEFAULT_MAX_TOKENS,
    temperature: float = _DEFAULT_TEMPERATURE,
) -> BudgetPolicy:
    """Return the process-wide ``BudgetPolicy`` singleton.

    The first call constructs the instance using the supplied arguments; all
    subsequent calls return the cached instance regardless of the arguments
    passed.  This matches the standard Vetinari singleton pattern used by
    ``get_model_registry`` and ``get_model_router``.

    Args:
        tier_budgets: Optional override mapping of tier → base thinking budget.
            Only used when constructing the singleton for the first time.
        max_tokens: Default max output tokens.  Only used on first construction.
        temperature: Default sampling temperature.  Only used on first
            construction.

    Returns:
        The singleton ``BudgetPolicy`` instance.
    """
    global _policy_instance
    if _policy_instance is None:
        with _policy_lock:
            if _policy_instance is None:
                _policy_instance = BudgetPolicy(
                    tier_budgets=tier_budgets,
                    max_tokens=max_tokens,
                    temperature=temperature,
                )
    return _policy_instance


def _reset_policy_for_testing() -> None:
    """Reset the singleton so tests can re-initialise it cleanly.

    This function is intentionally private and exists only for test isolation.
    Production code MUST NOT call it.
    """
    global _policy_instance
    with _policy_lock:
        _policy_instance = None


# ── Backward compatibility alias ──────────────────────────────────────────
InferenceConfig = ResolvedInferenceConfig
