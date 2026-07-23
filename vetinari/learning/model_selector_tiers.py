"""Thompson Sampling tier-selection helpers for intake routing.

Extracted from model_selector.py (Department 3 tier routing) to keep that
file within the 550-line limit.  Functions accept a
``selector: ThompsonSamplingSelector`` as first argument.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

logger = logging.getLogger(__name__)


if TYPE_CHECKING:
    from vetinari.learning.model_selector import ThompsonSamplingSelector


# Minimum arm observations before Thompson Sampling overrides rule-based tier routing.
TIER_MIN_PULLS = 10

__all__ = [
    "TIER_MIN_PULLS",
    "get_arm_state",
    "has_sufficient_data",
    "select_tier",
    "update_tier",
]


def has_sufficient_data(selector: ThompsonSamplingSelector, pattern_key: str) -> bool:
    """Check if enough data exists to override rule-based tier selection.

    Returns True if ANY tier arm has >= TIER_MIN_PULLS observations,
    indicating sufficient data to make a tier selection decision.

    Args:
        selector: ThompsonSamplingSelector instance providing arms and lock.
        pattern_key: Normalized pattern hash from IntakeFeatures.

    Returns:
        True when any tier arm (express, standard, or custom) has >= TIER_MIN_PULLS observations.
    """
    with selector._lock:
        for tier_value in ("express", "standard", "custom"):
            key = f"tier_{tier_value}:{pattern_key}"
            arm = selector._arms.get(key)
            if arm is not None and arm.total_pulls >= TIER_MIN_PULLS:
                return True
        return False


def select_tier(selector: ThompsonSamplingSelector, pattern_key: str) -> str:
    """Select the best tier for a request pattern using Thompson Sampling.

    Args:
        selector: ThompsonSamplingSelector instance providing arms and lock.
        pattern_key: Normalized pattern hash from IntakeFeatures.

    Returns:
        The selected tier value string: "express", "standard", or "custom".
    """
    with selector._lock:
        best_tier = "standard"
        best_score = -1.0

        for tier_value in ("express", "standard", "custom"):
            arm = selector._get_or_create_arm(f"tier_{tier_value}", pattern_key)
            sampled = arm.sample()

            if sampled > best_score:
                best_score = sampled
                best_tier = tier_value

        logger.info(
            "[Thompson] Selected tier %s for pattern %s (score=%.3f)",
            best_tier,
            pattern_key[:12],
            best_score,
        )
        return best_tier


def update_tier(
    selector: ThompsonSamplingSelector,
    pattern_key: str,
    tier_used: str,
    quality_score: float,
    rework_count: int = 0,
) -> None:
    """Update a tier arm after task completion.

    Applies a rework penalty: each rework cycle reduces effective quality by
    0.15 (capped at 0.6 total reduction), rewarding tiers that produce clean
    first-pass results.

    Args:
        selector: ThompsonSamplingSelector instance providing arms and lock.
        pattern_key: Normalized pattern hash from IntakeFeatures.
        tier_used: The tier that was used ("express", "standard", "custom").
        quality_score: Quality score 0.0-1.0 from the Quality agent.
        rework_count: Number of rework cycles needed.
    """
    rework_penalty = min(rework_count * 0.15, 0.6)
    effective_quality = max(0.0, quality_score - rework_penalty)
    success = effective_quality >= 0.5

    with selector._lock:
        arm = selector._get_or_create_arm(f"tier_{tier_used}", pattern_key)
        arm.update(effective_quality, success)
        selector._save_state()

    logger.info(
        "[Thompson] Tier update: pattern=%s tier=%s quality=%.2f rework=%d effective=%.2f",
        pattern_key[:12],
        tier_used,
        quality_score,
        rework_count,
        effective_quality,
    )


def get_arm_state(selector: ThompsonSamplingSelector, model_id: str, task_type: str) -> dict[str, Any]:
    """Return the current Beta distribution state for a model+task_type arm.

    Creates the arm with an informed prior if it does not yet exist.

    Args:
        selector: ThompsonSamplingSelector instance providing arms and lock.
        model_id: The model identifier.
        task_type: The task type string.

    Returns:
        Dict with keys: model_id, task_type, alpha, beta, mean, total_pulls.
    """
    arm = selector._get_or_create_arm(model_id, task_type)
    return {
        "model_id": arm.model_id,
        "task_type": arm.task_type,
        "alpha": arm.alpha,
        "beta": arm.beta,
        "mean": arm.mean,
        "total_pulls": arm.total_pulls,
    }
