"""Delegated Thompson selector methods kept out of model_selector.py."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

logger = logging.getLogger(__name__)


class _ThompsonSelectorMixin:
    """Tier, mode, contextual, and persistence helpers for Thompson selector."""

    if TYPE_CHECKING:
        BENCHMARK_PRIORS: Any
        MAX_ARMS: Any
        _arms: Any

    TIER_MIN_PULLS = 10  # See model_selector_tiers.TIER_MIN_PULLS

    def has_sufficient_data(self, pattern_key: str) -> bool:
        """Check if enough tier data exists to override rule-based routing. See ``model_selector_tiers``.

        Returns:
            True when all arms for the pattern key have been pulled at least TIER_MIN_PULLS times.
        """
        from vetinari.learning.model_selector_tiers import has_sufficient_data as _has_sufficient_data

        return _has_sufficient_data(self, pattern_key)

    def select_tier(self, pattern_key: str) -> str:
        """Select best tier via Thompson Sampling. See ``model_selector_tiers``.

        Returns:
            The tier identifier (e.g. "fast", "balanced", "quality") with the highest sampled reward.
        """
        from vetinari.learning.model_selector_tiers import select_tier as _select_tier

        return _select_tier(self, pattern_key)

    def update_tier(self, pattern_key: str, tier_used: str, quality_score: float, rework_count: int = 0) -> None:
        """Update tier arm after task completion. See ``model_selector_tiers``.

        Args:
            pattern_key: The request pattern key identifying the tier bandit context.
            tier_used: The tier name that was actually used (e.g. "fast", "quality").
            quality_score: Observed quality score for the completed task, 0.0-1.0.
            rework_count: Number of rework iterations required; used to penalise the reward.
        """
        from vetinari.learning.model_selector_tiers import update_tier as _update_tier

        _update_tier(self, pattern_key, tier_used, quality_score, rework_count)

    def get_arm_state(self, model_id: str, task_type: str) -> dict[str, Any]:
        """Return current Beta distribution state for an arm. See ``model_selector_tiers``.

        Args:
            model_id: Identifier of the model whose arm state to retrieve.
            task_type: Task domain for the arm (e.g. "coding", "summarisation").

        Returns:
            Dict with alpha, beta, n_pulls, and mean_reward for the specified model/task arm.
        """
        from vetinari.learning.model_selector_tiers import get_arm_state as _get_arm_state

        return _get_arm_state(self, model_id, task_type)

    # ------------------------------------------------------------------
    # Mode selection for multi-mode agents (Department 6, connection #77)
    # ------------------------------------------------------------------

    def select_mode(
        self,
        agent_type: str,
        task_type: str,
        candidate_modes: list[str],
    ) -> str:
        """Select best agent mode via Thompson Sampling. See ``model_selector_contextual``.

        Args:
            agent_type: The agent performing the task (e.g. "worker", "inspector").
            task_type: Task domain used to scope the bandit arms.
            candidate_modes: List of mode names to choose among (e.g. ["fast", "deep"]).

        Returns:
            The mode name from candidate_modes with the highest sampled reward.
        """
        from vetinari.learning.model_selector_contextual import select_mode as _select_mode

        return _select_mode(self, agent_type, task_type, candidate_modes)

    def update_mode(
        self,
        agent_type: str,
        task_type: str,
        mode: str,
        quality_score: float,
        success: bool,
    ) -> None:
        """Update mode arm after observing an outcome. See ``model_selector_contextual``.

        Args:
            agent_type: The agent whose mode arm to update.
            task_type: Task domain used to scope the bandit arms.
            mode: The mode name that was actually used.
            quality_score: Observed quality score for the completed task, 0.0-1.0.
            success: Whether the task completed without rework or failure.
        """
        from vetinari.learning.model_selector_contextual import update_mode as _update_mode

        _update_mode(self, agent_type, task_type, mode, quality_score, success)

    def has_mode_data(self, agent_type: str, task_type: str) -> bool:
        """Check if sufficient mode data exists for Thompson override. See ``model_selector_contextual``.

        Args:
            agent_type: The agent type to check mode data for.
            task_type: Task domain to scope the arm lookup.

        Returns:
            True when all candidate mode arms for this agent/task pair have been pulled enough times.
        """
        from vetinari.learning.model_selector_contextual import has_mode_data as _has_mode_data

        return _has_mode_data(self, agent_type, task_type)

    def select_strategy(
        self,
        agent_type: str,
        mode: str,
        strategy_key: str,
    ) -> str | int | float:
        """Select best strategy value via Thompson Sampling. See ``model_selector_contextual``.

        Args:
            agent_type: The agent whose strategy to select.
            mode: Current execution mode scoping this strategy decision.
            strategy_key: The strategy parameter name (e.g. "temperature_bucket", "depth").

        Returns:
            The strategy value (string, int, or float) with the highest sampled reward for this key.
        """
        from vetinari.learning.model_selector_contextual import select_strategy as _select_strategy

        return _select_strategy(self, agent_type, mode, strategy_key)

    def update_strategy(
        self,
        agent_type: str,
        mode: str,
        strategy_key: str,
        value: str | float,
        quality_score: float,
    ) -> None:
        """Update a strategy arm after observing an outcome. See ``model_selector_contextual``.

        Args:
            agent_type: The agent whose strategy arm to update.
            mode: Execution mode scoping this strategy arm.
            strategy_key: The strategy parameter name being updated.
            value: The actual value that was used in this execution.
            quality_score: Observed quality score for the outcome, 0.0-1.0.
        """
        from vetinari.learning.model_selector_contextual import update_strategy as _update_strategy

        _update_strategy(self, agent_type, mode, strategy_key, value, quality_score)

    DECAY_FACTOR = 0.995  # See model_selector_contextual.DECAY_FACTOR

    def select_model_contextual(
        self,
        task_context: Any,
        candidate_models: list[str],
        cost_per_model: dict[str, float] | None = None,
    ) -> str:
        """Select best model via context-aware Thompson Sampling. See ``model_selector_contextual``.

        Args:
            task_context: Structured context describing the current task (type, complexity, etc.).
            candidate_models: List of model_ids to select among.
            cost_per_model: Optional mapping of model_id to relative cost weight for penalisation.

        Returns:
            The model_id from candidate_models with the highest context-adjusted sampled reward.
        """
        from vetinari.learning.model_selector_contextual import select_model_contextual as _select_contextual

        return _select_contextual(self, task_context, candidate_models, cost_per_model)

    def update_contextual(
        self,
        task_context: Any,
        model_id: str,
        quality_score: float,
        success: bool,
    ) -> None:
        """Update contextual arm with exponential decay. See ``model_selector_contextual``.

        Args:
            task_context: Structured context describing the completed task.
            model_id: The model_id that was actually used for this task.
            quality_score: Observed quality score for the outcome, 0.0-1.0.
            success: Whether the task completed without rework or failure.
        """
        from vetinari.learning.model_selector_contextual import update_contextual as _update_contextual

        _update_contextual(self, task_context, model_id, quality_score, success)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _get_or_create_arm(self, model_id: str, task_type: str) -> Any:
        """Return existing arm or create one; evicts LRU when at MAX_ARMS capacity."""
        key = self._arm_key(model_id, task_type)
        if key not in self._arms:
            # Evict LRU arm if at capacity
            if len(self._arms) >= self.MAX_ARMS:
                self._evict_lru_arm()
            alpha, beta = self._get_informed_prior(model_id, task_type)
            from .thompson_arms import ThompsonBetaArm

            self._arms[key] = ThompsonBetaArm(
                model_id=model_id,
                task_type=task_type,
                alpha=alpha,
                beta=beta,
            )
        return self._arms[key]

    @staticmethod
    def _arm_key(model_id: str, task_type: str) -> str:
        """Return the stable key for a model/task arm."""
        from .model_selector import _make_arm_key

        return _make_arm_key(model_id, task_type)

    def _evict_lru_arm(self) -> None:
        """Remove the arm with the oldest last_updated timestamp."""
        if not self._arms:
            return
        lru_key = min(self._arms, key=lambda k: self._arms[k].last_updated)
        logger.debug("[Thompson] Evicting LRU arm %s to stay within MAX_ARMS=%d", lru_key, self.MAX_ARMS)
        del self._arms[lru_key]

    def _seed_from_benchmarks(self) -> None:
        """Seed arms from BENCHMARK_PRIORS on cold start; skipped if arms already exist."""
        if self._arms:
            return
        from .model_selector import _parse_arm_key
        from .thompson_arms import ThompsonBetaArm

        seeded = 0
        for key, (alpha, beta) in self.BENCHMARK_PRIORS.items():
            if key in self._arms:
                continue
            parts = _parse_arm_key(key)
            if parts is None:
                continue
            self._arms[key] = ThompsonBetaArm(
                model_id=parts[0],
                task_type=parts[1],
                alpha=alpha,
                beta=beta,
            )
            seeded += 1
        if seeded:
            logger.info("[Thompson] Cold-start: seeded %d arms from BENCHMARK_PRIORS", seeded)

    @staticmethod
    def _get_informed_prior(model_id: str, task_type: str) -> tuple:
        """Get informed prior from BenchmarkSeeder, fallback to Beta(1,1)."""
        try:
            from vetinari.learning.benchmark_seeder import get_benchmark_seeder

            return get_benchmark_seeder().get_prior(model_id, task_type)
        except Exception:
            logger.warning("BenchmarkSeeder unavailable for %s:%s, using uninformed prior", model_id, task_type)
            return (1.0, 1.0)

    def _get_state_dir(self) -> str:
        """Return .vetinari state dir path. See ``model_selector_persistence``."""
        from vetinari.learning.model_selector_persistence import get_state_dir

        return get_state_dir(self)

    def _load_state(self) -> None:
        """Load arm states from SQLite, falling back to JSON, then prune stale arms."""
        from vetinari.learning.model_selector_persistence import load_state
        from vetinari.learning.thompson_persistence import prune_stale_arms

        load_state(self)
        prune_stale_arms(self._arms)

    def _load_state_from_db(self) -> int:
        """Load arm states from SQLite table. See ``model_selector_persistence``."""
        from vetinari.learning.model_selector_persistence import load_state_from_db

        return load_state_from_db(self)

    def _migrate_from_json(self) -> None:
        """One-time migration from legacy JSON. See ``model_selector_persistence``."""
        from vetinari.learning.model_selector_persistence import migrate_from_json

        migrate_from_json(self)

    def _save_state(self) -> None:
        """Persist arm states to SQLite with JSON fallback. See ``model_selector_persistence``."""
        from vetinari.learning.model_selector_persistence import save_state

        save_state(self)
