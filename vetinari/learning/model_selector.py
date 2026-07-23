"""Thompson Sampling Model Selector - Vetinari Self-Improvement Subsystem.

Implements Bayesian bandit-style model selection that naturally balances
exploration (trying less-used models) with exploitation (using proven ones).

Each model+task_type pair maintains a Beta distribution:
  - alpha = successes (weighted by quality scores)
  - beta  = failures

When selecting a model, we sample from each distribution and pick the highest.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import math
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from vetinari.learning.thompson_arms import ThompsonBetaArm, ThompsonTaskContext
from vetinari.learning.thompson_persistence import prune_stale_arms

from .model_selector_methods import _ThompsonSelectorMixin

logger = logging.getLogger(__name__)

__all__ = [
    "ThompsonBetaArm",
    "ThompsonSamplingSelector",
    "ThompsonTaskContext",
    "get_model_selector",
    "reset_thompson_selector",
]

_MODEL_ARM_KEY_PREFIX = "model-json:"
_STRUCTURED_ARM_PREFIXES = ("strategy:", "mode_", "tier_", "ctx_")
_REWARD_TOKENS_REF = 1_000
_REWARD_LATENCY_REF_MS = 10_000


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


def _shape_reward_quality(
    quality_score: float,
    *,
    confidence: float | None,
    input_tokens: int | None,
    output_tokens: int | None,
    latency_ms: int | None,
) -> float:
    """Shape quality with bounded confidence, token, and latency factors.

    The pinned formula is ``clamp01(quality_score * w_conf * w_tok * w_lat)``.
    ``w_conf = 0.9 + 0.2 * clamp01(confidence)``. Token and latency factors
    use ``1 - 0.1 * log10(observed/reference)`` with
    ``_REWARD_TOKENS_REF=1000`` and ``_REWARD_LATENCY_REF_MS=10000``. Every
    factor is clamped to ``w_* in [0.9, 1.1]``. A missing dimension is neutral,
    and an all-None enrichment tuple returns ``quality_score`` bit-for-bit.
    """
    if confidence is None and input_tokens is None and output_tokens is None and latency_ms is None:
        return quality_score

    w_conf = 1.0 if confidence is None else 0.9 + 0.2 * _clamp(confidence, 0.0, 1.0)

    total_tokens = (input_tokens or 0) + (output_tokens or 0)
    if (input_tokens is None and output_tokens is None) or total_tokens <= 0:
        w_tok = 1.0
    else:
        w_tok = _clamp(1.0 - 0.1 * math.log10(total_tokens / _REWARD_TOKENS_REF), 0.9, 1.1)

    if latency_ms is None or latency_ms <= 0:
        w_lat = 1.0
    else:
        w_lat = _clamp(1.0 - 0.1 * math.log10(latency_ms / _REWARD_LATENCY_REF_MS), 0.9, 1.1)

    return _clamp(quality_score * w_conf * w_tok * w_lat, 0.0, 1.0)


def _make_arm_key(model_id: str, task_type: str) -> str:
    """Return a persistence key that does not alias colon-bearing model IDs."""
    if (":" in model_id or ":" in task_type) and not model_id.startswith(_STRUCTURED_ARM_PREFIXES):
        return _MODEL_ARM_KEY_PREFIX + json.dumps([model_id, task_type], separators=(",", ":"), ensure_ascii=True)
    return f"{model_id}:{task_type}"


def _parse_arm_key(key: str) -> tuple[str, str] | None:
    """Parse a persisted arm key, including the colon-safe JSON format."""
    if key.startswith(_MODEL_ARM_KEY_PREFIX):
        try:
            model_id, task_type = json.loads(key[len(_MODEL_ARM_KEY_PREFIX) :])
        except (TypeError, ValueError, json.JSONDecodeError):
            logger.warning("Ignoring malformed Thompson arm key: %r", key)
            return None
        return str(model_id), str(task_type)

    if ":" not in key:
        return None
    model_id, task_type = key.rsplit(":", 1)
    return model_id, task_type


@dataclass
class TaskContext:
    """Features that inform model selection — the "context" in contextual bandit.

    Args:
        task_type: Type of task (code, research, architecture, review, etc.).
        estimated_complexity: Complexity rating 1-10 from intake.
        prompt_length: Token count in the task description.
        domain: Domain (python, javascript, infrastructure, etc.).
        requires_reasoning: Whether multi-step logic is needed.
        requires_creativity: Whether open-ended generation is needed.
        requires_precision: Whether exact syntax/structured output is needed.
        file_count: Number of files in scope.
    """

    task_type: str = "general"
    estimated_complexity: int = 5
    prompt_length: int = 0
    domain: str = "general"
    requires_reasoning: bool = False
    requires_creativity: bool = False
    requires_precision: bool = False
    file_count: int = 0

    def __repr__(self) -> str:
        return f"TaskContext(task_type={self.task_type!r}, estimated_complexity={self.estimated_complexity!r}, domain={self.domain!r})"

    def to_bucket(self) -> int:
        """Hash context features into a bucket for Thompson arm lookup.

        Returns ~50 buckets. Enough signal to distinguish simple and complex
        coding contexts without making arms too sparse.

        Returns:
            Bucket index (0-49).
        """
        complexity_bin = "lo" if self.estimated_complexity <= 3 else ("mid" if self.estimated_complexity <= 7 else "hi")
        key = f"{self.task_type}:{complexity_bin}:{self.domain}:{self.requires_reasoning}"
        # Use hashlib for deterministic hashing across process restarts.
        # Python's built-in hash() is randomised by PYTHONHASHSEED, so two
        # processes with the same context key would map to different buckets,
        # breaking cross-restart arm persistence.
        return int(hashlib.md5(key.encode(), usedforsecurity=False).hexdigest(), 16) % 50


class ThompsonSamplingSelector(_ThompsonSelectorMixin):
    """Thompson Sampling model selector for exploration-exploitation balance.

    Automatically:
    - Explores new/untested models (slightly skeptical Beta(2,2) prior)
    - Exploits well-performing models for their appropriate task types
    - Incorporates cost as a penalty on expected return
    - Persists arm states to survive restarts
    """

    COST_WEIGHT = 0.15  # How much to penalize expensive models
    MIN_ARMS = 1
    MAX_ARMS = 500  # Cap on total arms to prevent unbounded memory growth

    # Cold-start priors: empty dict because _get_or_create_arm() uses
    # BenchmarkSeeder._get_informed_prior() for real models on first observation.
    # Previous cloud API model names (claude-sonnet-4-20250514 etc.) never matched
    # local GGUF model IDs, creating phantom arms. (Decision: SESSION-2-M1 fix 14)
    BENCHMARK_PRIORS: dict[str, tuple[float, float]] = {}

    PERIODIC_SAVE_INTERVAL = 25  # Save state to disk every N observations (frequent saves prevent data loss on crash)

    def __init__(self):
        # Key: "model_id:task_type" for legacy/simple IDs, or a JSON tuple
        # for colon-bearing model IDs.
        self._arms: dict[str, ThompsonBetaArm] = {}
        self._lock = threading.Lock()
        self._update_count: int = 0  # Observations since last periodic save
        self._load_state()
        with self._lock:
            pruned = prune_stale_arms(self._arms)
        if pruned:
            logger.debug("Pruned %d stale Thompson arms at startup", pruned)
        self._seed_from_benchmarks()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def select_model(
        self,
        task_type: str,
        candidate_models: list[str],
        cost_per_model: dict[str, float] | None = None,
    ) -> str:
        """Select the best model for a task type using Thompson Sampling.

        Args:
            task_type: Task type string.
            candidate_models: List of available model IDs.
            cost_per_model: Optional cost estimates per model (higher cost = penalty).

        Returns:
            Selected model ID, or "default" when candidates is empty.
        """
        with self._lock:
            if not candidate_models:
                return "default"

            cost_per_model = cost_per_model or {}
            max_cost = max(cost_per_model.values(), default=1.0)
            if max_cost == 0.0:
                max_cost = 1.0  # Avoid division by zero in cost normalization

            best_model = candidate_models[0]
            best_score = -1.0

            for model_id in candidate_models:
                arm = self._get_or_create_arm(model_id, task_type)
                sampled = arm.sample()

                # Additive penalty preserves exploration properties (multiplicative distorts Beta)
                cost = cost_per_model.get(model_id, max_cost * 0.5)
                adjusted = sampled - self.COST_WEIGHT * (cost / max_cost)

                if logger.isEnabledFor(logging.DEBUG):
                    logger.debug(
                        "[Thompson] %s/%s: sample=%.3f adjusted=%.3f (alpha=%.1f, beta=%.1f)",
                        model_id,
                        task_type,
                        sampled,
                        adjusted,
                        arm.alpha,
                        arm.beta,
                    )

                if adjusted > best_score:
                    best_score = adjusted
                    best_model = model_id

            logger.info("[Thompson] Selected %s for %s (score=%.3f)", best_model, task_type, best_score)
            return best_model

    def update(
        self,
        model_id: str,
        task_type: str,
        quality_score: float,
        success: bool,
        *,
        confidence: float | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        latency_ms: int | None = None,
    ) -> None:
        """Update a model arm after observing an outcome; auto-saves every PERIODIC_SAVE_INTERVAL updates.

        Args:
            model_id: The model that was used.
            task_type: The task type.
            quality_score: Observed quality score 0.0-1.0.
            success: Whether the task succeeded.
            confidence: Optional typed inference confidence.
            input_tokens: Optional exact prompt token count.
            output_tokens: Optional exact completion token count.
            latency_ms: Optional inference latency in milliseconds.
        """
        shaped_quality = _shape_reward_quality(
            quality_score,
            confidence=confidence,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            latency_ms=latency_ms,
        )
        with self._lock:
            arm = self._get_or_create_arm(model_id, task_type)
            arm.update(shaped_quality, success)
            self._update_count += 1
            self._save_state()
            if self._update_count % self.PERIODIC_SAVE_INTERVAL == 0:
                logger.info(
                    "[Thompson] Periodic state save after %d observations",
                    self._update_count,
                )

    # Benchmark results are 3x more reliable than single-task outcomes
    BENCHMARK_WEIGHT_MULTIPLIER = 3

    def update_from_benchmark(
        self,
        model_id: str,
        pass_rate: float,
        n_trials: int,
        task_type: str = "general",
    ) -> None:
        """Update arms from benchmark results (weighted 3x vs single-task observations).

        Args:
            model_id: The model that was benchmarked.
            pass_rate: Fraction of benchmark cases passed (0.0-1.0).
            n_trials: Number of benchmark trials run.
            task_type: Task type the benchmark covers.

        Raises:
            ValueError: If ``n_trials`` is not positive or ``pass_rate`` is outside [0.0, 1.0].
        """
        if n_trials <= 0:
            raise ValueError("benchmark feedback requires n_trials > 0")
        if not 0.0 <= pass_rate <= 1.0:
            raise ValueError("benchmark pass_rate must be between 0.0 and 1.0")
        pass_rate = max(0.0, min(1.0, pass_rate))  # Clamp to prevent Beta corruption

        with self._lock:
            arm = self._get_or_create_arm(model_id, task_type)
            w = self.BENCHMARK_WEIGHT_MULTIPLIER

            successes = pass_rate * n_trials
            failures = n_trials - successes

            arm.alpha += successes * w
            arm.beta += failures * w
            arm.total_pulls += n_trials
            arm.last_updated = datetime.now(timezone.utc).isoformat()
            self._save_state()

        logger.info(
            "[Thompson] Benchmark update for %s/%s: "
            "pass_rate=%.3f, n_trials=%d, "
            "weighted_successes=%.1f, weighted_failures=%.1f, "
            "new_mean=%.3f",
            model_id,
            task_type,
            pass_rate,
            n_trials,
            successes * w,
            failures * w,
            arm.mean,
        )

    def get_rankings(self, task_type: str) -> list[tuple[str, float]]:
        """Return (model_id, mean) tuples for all arms matching task_type, sorted by expected value.

        Args:
            task_type: The task type string to filter arms by.

        Returns:
            List sorted from highest to lowest expected value; empty when no arms exist.
        """
        arms = [(arm.model_id, arm.mean) for arm in self._arms.values() if arm.task_type == task_type]
        arms.sort(key=lambda x: x[1], reverse=True)
        return arms

    def strategy_arm_snapshot(self) -> list[ThompsonBetaArm]:
        """Return a stable snapshot of strategy arms for read-only analyzers.

        Returns:
            Strategy-scoped Thompson arms copied from the selector state.
        """
        with self._lock:
            return [arm for arm in self._arms.values() if str(arm.model_id).startswith("strategy:")]


# Singleton


_thompson_selector: ThompsonSamplingSelector | None = None
_thompson_selector_lock = threading.Lock()
_thompson_selector_save_callback: Any | None = None


def get_thompson_selector() -> ThompsonSamplingSelector:
    """Return the singleton ThompsonSamplingSelector instance (thread-safe).

    Registers atexit handler and shutdown callback on first creation
    to persist Thompson state on process exit.

    Returns:
        The shared ThompsonSamplingSelector instance.
    """
    global _thompson_selector, _thompson_selector_save_callback
    if _thompson_selector is None:
        with _thompson_selector_lock:
            if _thompson_selector is None:
                _thompson_selector = ThompsonSamplingSelector()
                _thompson_selector_save_callback = _thompson_selector._save_state
                # Register atexit handler for graceful persistence
                import atexit

                atexit.register(_thompson_selector_save_callback)
                # Also register with shutdown.py callback system
                try:
                    from vetinari.shutdown import register_callback

                    register_callback("Thompson Sampling state", _thompson_selector_save_callback)
                except Exception:
                    logger.warning("shutdown.py unavailable, atexit-only Thompson persistence")
    return _thompson_selector


def get_model_selector() -> ThompsonSamplingSelector:
    """Backward-compatible alias for the Thompson selector singleton."""
    return get_thompson_selector()


def reset_thompson_selector(*, save: bool = False) -> None:
    """Reset the Thompson selector singleton and unregister shutdown callbacks.

    This is intended for tests and process-lifecycle cleanup that need to drop
    per-environment database paths without leaving an atexit callback pointing
    at stale state.
    """
    global _thompson_selector, _thompson_selector_save_callback
    with _thompson_selector_lock:
        selector = _thompson_selector
        callback = _thompson_selector_save_callback
        if save and selector is not None:
            with contextlib.suppress(Exception):
                selector._save_state()
        if callback is not None:
            import atexit

            with contextlib.suppress(Exception):
                atexit.unregister(callback)
            with contextlib.suppress(Exception):
                from vetinari.shutdown import unregister_callback

                unregister_callback("Thompson Sampling state", callback)
        _thompson_selector = None
        _thompson_selector_save_callback = None


# ── Module-level convenience function for curriculum integration ──────────


def get_skill_rankings() -> list[dict[str, Any]]:
    """Return task-type skill rankings for TrainingCurriculum integration.

    Returns:
        List of ``{"task_type": str, "score": float}`` dicts sorted by mean
        expected value. Empty list when no arms have been observed.
    """
    try:
        selector = get_thompson_selector()
    except Exception:
        logger.warning("Could not get ThompsonSamplingSelector for skill rankings")
        return []

    task_types = [
        "coding",
        "planning",
        "analysis",
        "research",
        "architecture",
        "review",
        "documentation",
        "testing",
        "refactoring",
        "general",
    ]
    rankings: list[dict[str, Any]] = []
    for tt in task_types:
        ranked = selector.get_rankings(tt)
        if ranked:
            best_score = ranked[0][1]  # (model_id, score) tuples
            rankings.append({"task_type": tt, "score": best_score})
    return rankings
