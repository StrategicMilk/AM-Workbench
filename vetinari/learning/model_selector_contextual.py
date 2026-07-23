"""Thompson Sampling contextual, mode, and strategy selection helpers.

Extracted from model_selector.py to keep that file within the 550-line limit.
Functions accept a ``selector: ThompsonSamplingSelector`` as first argument and
call its ``_get_or_create_arm()``, ``_save_state()``, and ``_lock`` directly.

Uses TYPE_CHECKING to import ThompsonSamplingSelector for annotations only —
at runtime the parameter is typed via the forward reference without a real import.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from vetinari.learning.model_selector import ThompsonSamplingSelector, ThompsonTaskContext

from vetinari.constants import STRATEGY_VALUE_SPACES
from vetinari.exceptions import ConfigurationError

logger = logging.getLogger(__name__)


# -- Strategy value spaces (US-507: Meta-Learning Strategy Selection) -----
# Kept here rather than in the class so they are accessible without importing
# the full ThompsonSamplingSelector at sites that only need strategy config.
# Non-stationarity decay factor — slower decay because local models don't
# change often, so older observations remain relevant longer.
DECAY_FACTOR = 0.995

__all__ = [
    "DECAY_FACTOR",
    "STRATEGY_VALUE_SPACES",
    "has_mode_data",
    "select_mode",
    "select_model_contextual",
    "select_strategy",
    "update_contextual",
    "update_mode",
    "update_strategy",
]


def select_mode(
    selector: ThompsonSamplingSelector,
    agent_type: str,
    task_type: str,
    candidate_modes: list[str],
) -> str:
    """Select the best mode for an agent using Thompson Sampling.

    An arm is maintained for every (agent_type, mode, task_type) tuple, keyed
    as ``mode_{agent_type}_{mode}:{task_type}``.

    Args:
        selector: ThompsonSamplingSelector instance providing arms and lock.
        agent_type: The agent type (e.g., "WORKER", "INSPECTOR").
        task_type: The task type (e.g., "coding", "review").
        candidate_modes: List of available mode strings.

    Returns:
        Selected mode string; returns "default" when candidates is empty.
    """
    with selector._lock:
        if not candidate_modes:
            return "default"

        best_mode = candidate_modes[0]
        best_score = -1.0

        for mode in candidate_modes:
            arm = selector._get_or_create_arm(f"mode_{agent_type}_{mode}", task_type)
            sampled = arm.sample()

            logger.debug(
                "[Thompson] Mode %s/%s/%s: sample=%.3f (alpha=%.1f, beta=%.1f)",
                agent_type,
                task_type,
                mode,
                sampled,
                arm.alpha,
                arm.beta,
            )

            if sampled > best_score:
                best_score = sampled
                best_mode = mode

        logger.info(
            "[Thompson] Selected mode %s for %s/%s (score=%.3f)",
            best_mode,
            agent_type,
            task_type,
            best_score,
        )
        return best_mode


def update_mode(
    selector: ThompsonSamplingSelector,
    agent_type: str,
    task_type: str,
    mode: str,
    quality_score: float,
    success: bool,
) -> None:
    """Update a mode arm after observing an outcome.

    Args:
        selector: ThompsonSamplingSelector instance providing arms and lock.
        agent_type: The agent type.
        task_type: The task type.
        mode: The mode that was used.
        quality_score: Observed quality score 0.0-1.0.
        success: Whether the task succeeded.
    """
    with selector._lock:
        arm = selector._get_or_create_arm(f"mode_{agent_type}_{mode}", task_type)
        arm.update(quality_score, success)
        selector._update_count += 1
        if selector._update_count % selector.PERIODIC_SAVE_INTERVAL == 0:
            selector._save_state()


def has_mode_data(selector: ThompsonSamplingSelector, agent_type: str, task_type: str) -> bool:
    """Check if sufficient mode data exists for Thompson override.

    Args:
        selector: ThompsonSamplingSelector instance providing arms and lock.
        agent_type: The agent type.
        task_type: The task type.

    Returns:
        True if at least one mode arm has >= TIER_MIN_PULLS observations.
    """
    with selector._lock:
        for key, arm in selector._arms.items():
            if (
                key.startswith(f"mode_{agent_type}_")
                and key.endswith(f":{task_type}")
                and arm.total_pulls >= selector.TIER_MIN_PULLS
            ):
                return True
        return False


def select_strategy(
    selector: ThompsonSamplingSelector,
    agent_type: str,
    mode: str,
    strategy_key: str,
) -> str | int | float:
    """Select the best strategy value using Thompson Sampling.

    Each strategy key has a predefined set of valid values (see
    STRATEGY_VALUE_SPACES).  An arm is maintained for every
    (agent_type, mode, strategy_key, value) tuple, prefixed with
    ``strategy:`` to keep them separate from model arms.

    Args:
        selector: ThompsonSamplingSelector instance providing arms and lock.
        agent_type: The agent type (e.g., "WORKER", "INSPECTOR").
        mode: The agent mode (e.g., "code_review", "build").
        strategy_key: One of the keys in STRATEGY_VALUE_SPACES.

    Returns:
        The selected strategy value from the valid value space.

    Raises:
        ConfigurationError: If strategy_key is not in STRATEGY_VALUE_SPACES.
    """
    if strategy_key not in STRATEGY_VALUE_SPACES:
        raise ConfigurationError(
            f"Unknown strategy_key {strategy_key!r}. Valid keys: {list(STRATEGY_VALUE_SPACES.keys())}",
        )

    values = STRATEGY_VALUE_SPACES[strategy_key]

    with selector._lock:
        best_value = values[0]
        best_score = -1.0

        for value in values:
            arm_model_id = f"strategy:{agent_type}:{mode}:{strategy_key}:{value}"
            arm = selector._get_or_create_arm(arm_model_id, "strategy")
            sampled = arm.sample()

            logger.debug(
                "[Thompson] Strategy %s/%s/%s=%s: sample=%.3f (alpha=%.1f, beta=%.1f)",
                agent_type,
                mode,
                strategy_key,
                value,
                sampled,
                arm.alpha,
                arm.beta,
            )

            if sampled > best_score:
                best_score = sampled
                best_value = value

        logger.info(
            "[Thompson] Selected strategy %s=%s for %s/%s (score=%.3f)",
            strategy_key,
            best_value,
            agent_type,
            mode,
            best_score,
        )
        return best_value


def update_strategy(
    selector: ThompsonSamplingSelector,
    agent_type: str,
    mode: str,
    strategy_key: str,
    value: str | float,
    quality_score: float,
) -> None:
    """Update a strategy arm after observing an outcome.

    Quality scores above 0.5 increment alpha by 1; at or below 0.5 increment
    beta by 1.

    Args:
        selector: ThompsonSamplingSelector instance providing arms and lock.
        agent_type: The agent type.
        mode: The agent mode.
        strategy_key: The strategy key that was used.
        value: The strategy value that was used.
        quality_score: Observed quality score 0.0-1.0.

    Raises:
        ConfigurationError: If strategy_key is not in STRATEGY_VALUE_SPACES.
    """
    if strategy_key not in STRATEGY_VALUE_SPACES:
        raise ConfigurationError(
            f"Unknown strategy_key {strategy_key!r}. Valid keys: {list(STRATEGY_VALUE_SPACES.keys())}",
        )

    arm_model_id = f"strategy:{agent_type}:{mode}:{strategy_key}:{value}"
    success = quality_score > 0.5

    with selector._lock:
        arm = selector._get_or_create_arm(arm_model_id, "strategy")
        if success:
            arm.alpha += 1.0
        else:
            arm.beta += 1.0
        arm.total_pulls += 1
        arm.last_updated = datetime.now(timezone.utc).isoformat()

        selector._update_count += 1
        if selector._update_count % selector.PERIODIC_SAVE_INTERVAL == 0:
            selector._save_state()

    logger.info(
        "[Thompson] Strategy update: %s/%s %s=%s quality=%.2f success=%s",
        agent_type,
        mode,
        strategy_key,
        value,
        quality_score,
        success,
    )


def select_model_contextual(
    selector: ThompsonSamplingSelector,
    task_context: ThompsonTaskContext,
    candidate_models: list[str],
    cost_per_model: dict[str, float] | None = None,
) -> str:
    """Select the best model using context-aware Thompson Sampling.

    Uses task context features to bucket arms, providing finer-grained
    selection than basic task_type-only routing.

    Args:
        selector: ThompsonSamplingSelector instance providing arms and lock.
        task_context: Task context features.
        candidate_models: List of available model IDs.
        cost_per_model: Optional cost estimates per model.

    Returns:
        Selected model ID; returns "default" when candidates is empty.
    """
    with selector._lock:
        if not candidate_models:
            return "default"

        bucket = task_context.to_bucket()
        bucket_key = f"ctx_{bucket}"
        cost_per_model = cost_per_model or {}
        max_cost = max(cost_per_model.values(), default=1.0)
        if max_cost == 0.0:
            max_cost = 1.0  # Avoid division by zero in cost normalization

        best_model = candidate_models[0]
        best_score = -1.0

        for model_id in candidate_models:
            arm = selector._get_or_create_arm(model_id, bucket_key)
            sampled = arm.sample()

            cost = cost_per_model.get(model_id, max_cost * 0.5)
            cost_penalty = selector.COST_WEIGHT * (cost / max_cost)
            adjusted = sampled - cost_penalty

            if adjusted > best_score:
                best_score = adjusted
                best_model = model_id

        logger.info(
            "[Thompson] Contextual selected %s for bucket %d/%s (score=%.3f)",
            best_model,
            bucket,
            task_context.task_type,
            best_score,
        )
        return best_model


def update_contextual(
    selector: ThompsonSamplingSelector,
    task_context: ThompsonTaskContext,
    model_id: str,
    quality_score: float,
    success: bool,
) -> None:
    """Update a contextual arm after observing an outcome.

    Applies exponential decay (DECAY_FACTOR) to all other arms in the same
    bucket to handle non-stationarity (quality changes over time).

    Args:
        selector: ThompsonSamplingSelector instance providing arms and lock.
        task_context: The context used during selection.
        model_id: The model that was used.
        quality_score: Observed quality score 0.0-1.0.
        success: Whether the task succeeded.
    """
    with selector._lock:
        bucket = task_context.to_bucket()
        bucket_key = f"ctx_{bucket}"
        arm = selector._get_or_create_arm(model_id, bucket_key)
        arm.update(quality_score, success)

        # Decay other arms in this bucket to handle non-stationarity.
        # Skip the just-updated arm to avoid decaying the fresh observation.
        current_key = selector._arm_key(model_id, bucket_key)
        for key, existing_arm in selector._arms.items():
            if key.endswith(f":{bucket_key}") and key != current_key:
                existing_arm.alpha *= DECAY_FACTOR
                existing_arm.beta *= DECAY_FACTOR
                # Floor at 1.0 to prevent arms from vanishing
                existing_arm.alpha = max(existing_arm.alpha, 1.0)
                existing_arm.beta = max(existing_arm.beta, 1.0)

        selector._update_count += 1
        if selector._update_count % selector.PERIODIC_SAVE_INTERVAL == 0:
            selector._save_state()


def _get_arm_state_summary(selector: Any) -> list[dict[str, Any]]:
    """Return a summary of all arms for debugging/inspection.

    Args:
        selector: ThompsonSamplingSelector instance.

    Returns:
        List of dicts with arm_key, model_id, task_type, alpha, beta,
        total_pulls, and mean fields.
    """
    with selector._lock:
        return [
            {
                "arm_key": key,
                "model_id": arm.model_id,
                "task_type": arm.task_type,
                "alpha": arm.alpha,
                "beta": arm.beta,
                "total_pulls": arm.total_pulls,
                "mean": arm.mean,
            }
            for key, arm in selector._arms.items()
        ]
