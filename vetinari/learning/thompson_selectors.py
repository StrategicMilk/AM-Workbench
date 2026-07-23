"""Thompson Sampling selection helpers — arm management and routing strategies.

Extracted from ``model_selector.py`` to keep that file under the 550-line
ceiling.  All functions operate on the arms dict passed by
``ThompsonSamplingSelector``, which retains the lock.

Public API:
    ``_get_or_create_arm``       — get or create a BetaArm with LRU eviction
    ``has_sufficient_tier_data`` — check if tier data is sufficient
    ``has_sufficient_mode_data`` — check if mode data is sufficient
    ``select_tier``              — select intake tier via Thompson Sampling
    ``update_tier``              — update tier arm after task completion
    ``select_mode``              — select agent mode via Thompson Sampling
    ``update_mode``              — update mode arm after task completion
    ``select_strategy``          — select strategy value via Thompson Sampling
    ``update_strategy``          — update strategy arm after task completion
    ``STRATEGY_VALUE_SPACES``    — valid value spaces for strategy selection
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from vetinari.exceptions import ConfigurationError

logger = logging.getLogger(__name__)


if TYPE_CHECKING:
    from vetinari.learning.thompson_arms import ThompsonBetaArm


_MODEL_ARM_KEY_PREFIX = "model-json:"
_STRUCTURED_ARM_PREFIXES = ("strategy:", "mode_", "tier_", "ctx_")

# ── Strategy value spaces ────────────────────────────────────────────────────
# Who writes: thompson_selectors.select_strategy / update_strategy
# Who reads: ThompsonSamplingSelector.STRATEGY_VALUE_SPACES (re-exported)
STRATEGY_VALUE_SPACES: dict[str, list[str | int | float]] = {
    "prompt_template_variant": ["standard", "concise", "detailed", "structured"],
    "context_window_size": [2048, 4096, 8192, 16384],
    "temperature": [0.0, 0.3, 0.5, 0.7, 1.0],
    "decomposition_granularity": ["coarse", "medium", "fine"],
}

# Minimum observations before Thompson override of rule-based selection
_TIER_MIN_PULLS = 10

# ── Arm management ───────────────────────────────────────────────────────────


def _get_or_create_arm(
    arms: dict[str, ThompsonBetaArm],
    model_id: str,
    task_type: str,
    max_arms: int,
    get_prior: Callable[[str, str], tuple[float, float]],
) -> ThompsonBetaArm:
    """Get or create a Beta arm for a model+task_type pair.

    Evicts the least-recently-updated arm when ``max_arms`` is exceeded.
    Caller is responsible for holding the selector lock.

    Args:
        arms: The shared arms dict (mutated in place).
        model_id: The model identifier.
        task_type: The task type string.
        max_arms: Maximum number of arms before eviction.
        get_prior: Callable(model_id, task_type) -> (alpha, beta).

    Returns:
        The existing or newly created BetaArm.
    """
    from vetinari.learning.thompson_arms import ThompsonBetaArm

    if (":" in model_id or ":" in task_type) and not model_id.startswith(_STRUCTURED_ARM_PREFIXES):
        key = _MODEL_ARM_KEY_PREFIX + json.dumps([model_id, task_type], separators=(",", ":"), ensure_ascii=True)
    else:
        key = f"{model_id}:{task_type}"
    if key not in arms:
        if len(arms) >= max_arms:
            _evict_lru_arm(arms, max_arms)
        alpha, beta = get_prior(model_id, task_type)
        arms[key] = ThompsonBetaArm(model_id=model_id, task_type=task_type, alpha=alpha, beta=beta)
    return arms[key]


def _evict_lru_arm(arms: dict[str, ThompsonBetaArm], max_arms: int) -> None:
    """Remove the least-recently-updated arm to stay within max_arms.

    Args:
        arms: The shared arms dict (mutated in place).
        max_arms: The max-arms cap for logging.
    """
    if not arms:
        return
    lru_key = min(arms, key=lambda k: arms[k].last_updated)
    logger.debug("[Thompson] Evicting LRU arm %s to stay within MAX_ARMS=%d", lru_key, max_arms)
    del arms[lru_key]


# ── Tier selection ───────────────────────────────────────────────────────────


def has_sufficient_tier_data(arms: dict[str, ThompsonBetaArm], pattern_key: str) -> bool:
    """Check whether enough tier observations exist to override rule-based routing.

    All three tier arms must have >= _TIER_MIN_PULLS observations before Thompson
    Sampling overrides rule-based routing.  A single well-explored arm is not
    sufficient — the sampler needs comparable evidence on every tier to make a
    meaningful relative comparison.

    Args:
        arms: The shared arms dict.
        pattern_key: Normalised pattern hash from IntakeFeatures.

    Returns:
        True only when EVERY tier arm has >= _TIER_MIN_PULLS observations.
    """
    for tier_value in ("express", "standard", "custom"):
        key = f"tier_{tier_value}:{pattern_key}"
        arm = arms.get(key)
        if arm is None or arm.total_pulls < _TIER_MIN_PULLS:
            return False
    return True


def select_tier(
    arms: dict[str, ThompsonBetaArm],
    pattern_key: str,
    max_arms: int,
    get_prior: Callable[[str, str], tuple[float, float]],
) -> str:
    """Select the best intake tier for a pattern using Thompson Sampling.

    Args:
        arms: The shared arms dict.
        pattern_key: Normalised pattern hash from IntakeFeatures.
        max_arms: Maximum number of arms before eviction.
        get_prior: Callable(model_id, task_type) -> (alpha, beta).

    Returns:
        Selected tier string — one of "express", "standard", "custom".
    """
    best_tier = "standard"
    best_score = -1.0
    for tier_value in ("express", "standard", "custom"):
        arm = _get_or_create_arm(arms, f"tier_{tier_value}", pattern_key, max_arms, get_prior)
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
    arms: dict[str, ThompsonBetaArm],
    pattern_key: str,
    tier_used: str,
    quality_score: float,
    rework_count: int,
    max_arms: int,
    get_prior: Callable[[str, str], tuple[float, float]],
) -> None:
    """Update a tier arm after task completion.

    Higher quality + lower rework = higher reward for that tier.
    Each rework cycle reduces effective quality by 0.15 (capped at 0.6).

    Args:
        arms: The shared arms dict.
        pattern_key: The normalised pattern hash.
        tier_used: The tier that was used ("express", "standard", "custom").
        quality_score: Quality score 0.0-1.0 from Quality agent.
        rework_count: Number of rework cycles needed.
        max_arms: Maximum number of arms before eviction.
        get_prior: Callable(model_id, task_type) -> (alpha, beta).
    """
    # Clamp incoming quality_score to [0.0, 1.0] — callers sometimes pass
    # raw scores from quality agents that can exceed 1.0 (e.g. weighted sums).
    # Values outside this range would corrupt the Beta distribution parameters.
    quality_score = min(max(0.0, quality_score), 1.0)
    rework_penalty = min(rework_count * 0.15, 0.6)
    effective_quality = max(0.0, quality_score - rework_penalty)
    success = effective_quality >= 0.5
    arm = _get_or_create_arm(arms, f"tier_{tier_used}", pattern_key, max_arms, get_prior)
    arm.update(effective_quality, success)
    logger.info(
        "[Thompson] Tier update: pattern=%s tier=%s quality=%.2f rework=%d effective=%.2f",
        pattern_key[:12],
        tier_used,
        quality_score,
        rework_count,
        effective_quality,
    )


# ── Mode selection ───────────────────────────────────────────────────────────


def has_sufficient_mode_data(arms: dict[str, ThompsonBetaArm], agent_type: str, task_type: str) -> bool:
    """Check whether enough mode observations exist to override rule-based routing.

    Args:
        arms: The shared arms dict.
        agent_type: The agent type string.
        task_type: The task type string.

    Returns:
        True if at least one mode arm has >= _TIER_MIN_PULLS observations.
    """
    for key, arm in arms.items():
        if (
            key.startswith(f"mode_{agent_type}_")
            and key.endswith(f":{task_type}")
            and arm.total_pulls >= _TIER_MIN_PULLS
        ):
            return True
    return False


def select_mode(
    arms: dict[str, ThompsonBetaArm],
    agent_type: str,
    task_type: str,
    candidate_modes: list[str],
    max_arms: int,
    get_prior: Callable[[str, str], tuple[float, float]],
) -> str:
    """Select the best mode for an agent using Thompson Sampling.

    Args:
        arms: The shared arms dict.
        agent_type: The agent type (e.g., "WORKER", "INSPECTOR").
        task_type: The task type (e.g., "coding", "review").
        candidate_modes: List of available mode strings.
        max_arms: Maximum number of arms before eviction.
        get_prior: Callable(model_id, task_type) -> (alpha, beta).

    Returns:
        Selected mode string, or "default" if candidate_modes is empty.
    """
    if not candidate_modes:
        return "default"
    best_mode = candidate_modes[0]
    best_score = -1.0
    for mode in candidate_modes:
        arm = _get_or_create_arm(arms, f"mode_{agent_type}_{mode}", task_type, max_arms, get_prior)
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
    arms: dict[str, ThompsonBetaArm],
    agent_type: str,
    task_type: str,
    mode: str,
    quality_score: float,
    success: bool,
    max_arms: int,
    get_prior: Callable[[str, str], tuple[float, float]],
) -> None:
    """Update a mode arm after observing an outcome.

    Args:
        arms: The shared arms dict.
        agent_type: The agent type string.
        task_type: The task type string.
        mode: The mode that was used.
        quality_score: Observed quality score 0.0-1.0.
        success: Whether the task succeeded.
        max_arms: Maximum number of arms before eviction.
        get_prior: Callable(model_id, task_type) -> (alpha, beta).
    """
    arm = _get_or_create_arm(arms, f"mode_{agent_type}_{mode}", task_type, max_arms, get_prior)
    arm.update(quality_score, success)


# ── Strategy selection ────────────────────────────────────────────────────────


def select_strategy(
    arms: dict[str, ThompsonBetaArm],
    agent_type: str,
    mode: str,
    strategy_key: str,
    max_arms: int,
    get_prior: Callable[[str, str], tuple[float, float]],
) -> str | int | float:
    """Select the best strategy value using Thompson Sampling.

    Args:
        arms: The shared arms dict.
        agent_type: The agent type (e.g., "WORKER", "INSPECTOR").
        mode: The agent mode (e.g., "code_review", "build").
        strategy_key: One of the keys in STRATEGY_VALUE_SPACES.
        max_arms: Maximum number of arms before eviction.
        get_prior: Callable(model_id, task_type) -> (alpha, beta).

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
    best_value: str | int | float = values[0]
    best_score = -1.0
    for value in values:
        arm_id = f"strategy:{agent_type}:{mode}:{strategy_key}:{value}"
        arm = _get_or_create_arm(arms, arm_id, "strategy", max_arms, get_prior)
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
    arms: dict[str, ThompsonBetaArm],
    agent_type: str,
    mode: str,
    strategy_key: str,
    value: str | float,
    quality_score: float,
    max_arms: int,
    get_prior: Callable[[str, str], tuple[float, float]],
) -> None:
    """Update a strategy arm after observing an outcome.

    Delegates to ``ThompsonBetaArm.update()`` so strategy arms use the same
    quality-weighted Beta update contract as model, tier, and mode arms.

    Args:
        arms: The shared arms dict.
        agent_type: The agent type string.
        mode: The agent mode string.
        strategy_key: The strategy key that was used.
        value: The strategy value that was used.
        quality_score: Observed quality score 0.0-1.0.
        max_arms: Maximum number of arms before eviction.
        get_prior: Callable(model_id, task_type) -> (alpha, beta).

    Raises:
        ConfigurationError: If strategy_key is not in STRATEGY_VALUE_SPACES.
    """
    if strategy_key not in STRATEGY_VALUE_SPACES:
        raise ConfigurationError(
            f"Unknown strategy_key {strategy_key!r}. Valid keys: {list(STRATEGY_VALUE_SPACES.keys())}",
        )
    # Clamp incoming quality_score to [0.0, 1.0] — callers sometimes pass
    # raw scores from quality agents that can exceed 1.0 (e.g. weighted sums).
    # Values outside this range would corrupt the Beta distribution parameters.
    quality_score = min(max(0.0, quality_score), 1.0)
    arm_id = f"strategy:{agent_type}:{mode}:{strategy_key}:{value}"
    arm = _get_or_create_arm(arms, arm_id, "strategy", max_arms, get_prior)
    success = quality_score > 0.5
    arm.update(quality_score, success)
    logger.info(
        "[Thompson] Strategy update: %s/%s %s=%s quality=%.2f success=%s",
        agent_type,
        mode,
        strategy_key,
        value,
        quality_score,
        success,
    )


# Convenience alias kept for internal callers that reference Any-typed return
_AnySelectionResult = Any
