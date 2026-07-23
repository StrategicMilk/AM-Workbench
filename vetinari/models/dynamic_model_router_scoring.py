"""Scoring and confidence mixin for the dynamic model router."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from vetinari.models.model_router_scoring import assess_warm_model_bonus, calculate_confidence
from vetinari.models.model_router_types import ModelInfo, TaskType
from vetinari.types import ModelProvider

if TYPE_CHECKING:
    from vetinari.awareness.confidence import ConfidenceResult, UnknownSituationProtocol

logger = logging.getLogger(__name__)

__all__ = ["DynamicModelRouterScoringMixin"]

_IMPLICIT_BOOST_CAP = 0.10
_IMPLICIT_BOOST_MATURITY = 10


class DynamicModelRouterScoringMixin:
    """Internal scoring, implicit-feedback, and confidence behavior."""

    if TYPE_CHECKING:
        _model_tiers: Any
        _ponder_score: Any
        prefer_local: Any

    # Minimum Thompson observations before Thompson becomes the primary scorer.
    # Below this, rule-based scoring dominates with a small Thompson bonus.
    _THOMPSON_MATURITY_THRESHOLD = 20

    def _score_model(
        self,
        model: ModelInfo,
        task_type: TaskType,
        task_description: str,
        preferred_models: list[str],
        implicit_boosts: dict[str, dict[str, Any]] | None = None,
    ) -> float:
        """Score a model for a given task, blending PonderEngine and internal scoring.

        Args:
            model: The model to score.
            task_type: The GoalCategory for this task.
            task_description: Free-text task description.
            preferred_models: Ordered list of preferred model IDs.
            implicit_boosts: Per-call accumulator for implicit-feedback boosts;
                written by ``_internal_score`` and read by ``select_model``
                after all candidates are scored.  When ``None`` (direct test
                calls), a throwaway dict is used so callers need not supply it.

        Returns:
            Score in [0, 1+] (higher is better).
        """
        _boosts: dict[str, dict[str, Any]] = implicit_boosts if implicit_boosts is not None else {}
        ponder_score = self._ponder_score(model, task_description)
        if ponder_score is not None:
            return 0.50 * ponder_score + 0.50 * self._internal_score(
                model, task_type, task_description, preferred_models, _boosts
            )
        return self._internal_score(model, task_type, task_description, preferred_models, _boosts)

    def _internal_score(
        self,
        model: ModelInfo,
        task_type: TaskType,
        task_description: str,
        preferred_models: list[str] | None,
        implicit_boosts: dict[str, dict[str, Any]] | None = None,
    ) -> float:
        """Internal scoring with Thompson Sampling as primary when data is mature.

        When a model+task_type arm has >= 20 observations, Thompson Sampling
        provides 70% of the score (bandit-first). Below that threshold,
        rule-based scoring dominates with a small Thompson bonus and an
        exploration bonus for undertested models.

        Args:
            model: The model to score.
            task_type: The GoalCategory for this task.
            task_description: Free-text task description (currently unused in base scoring).
            preferred_models: Ordered list of preferred model IDs for preference boost.
            implicit_boosts: Per-call accumulator written here and read by
                ``select_model`` after all candidates are scored.  Using a
                local dict (not ``self._last_implicit_boost``) eliminates the
                TOCTOU race where concurrent ``select_model`` calls would
                clear-and-overwrite the shared instance dict mid-scoring.

        Returns:
            Score in [0, 1+].
        """
        rule_score = self._rule_based_score(model, task_type, preferred_models)
        thompson_score, thompson_observations = self._thompson_score(model, task_type)
        score = self._blend_rule_and_thompson(rule_score, thompson_score, thompson_observations)
        score += assess_warm_model_bonus(model.id)
        score += self._tier_score_adjustment(model, task_type)

        boost, source = self._compute_implicit_feedback_boost(model.id, task_type)
        if implicit_boosts is not None:
            implicit_boosts[model.id] = {"boost": boost, "source": source}
        return score + boost

    def _rule_based_score(
        self,
        model: ModelInfo,
        task_type: TaskType,
        preferred_models: list[str] | None,
    ) -> float:
        """Return deterministic capability, preference, and locality score."""
        score = 0.40 * model.capabilities.matches_task(task_type)
        if preferred_models and model.id in preferred_models:
            score += 0.20 * max(0.0, 1.0 - preferred_models.index(model.id) * 0.3)
        if model.total_uses > 0:
            perf_score = model.success_rate * (1.0 - min(model.avg_latency_ms / 60000, 1.0))
            score += 0.20 * perf_score
        else:
            score += 0.10
        if self.prefer_local:
            if model.provider == ModelProvider.LOCAL:
                score += 0.10
            elif model.provider == ModelProvider.OTHER:
                score += 0.05
        else:
            score += 0.10
        if model.context_length >= 8192:
            score += 0.10
        elif model.context_length >= 4096:
            score += 0.05
        return score

    @staticmethod
    def _thompson_arm_key(model: ModelInfo, task_type: TaskType) -> str:
        """Return the Thompson arm key for model and task type."""
        task_type_str = task_type.value if hasattr(task_type, "value") else str(task_type)
        return f"{model.id}:{task_type_str}"

    def _thompson_score(self, model: ModelInfo, task_type: TaskType) -> tuple[float, int]:
        """Return Thompson sample and observation count for a model arm."""
        try:
            from vetinari.learning.model_selector import get_thompson_selector

            ts = get_thompson_selector()
            arm = ts._arms.get(self._thompson_arm_key(model, task_type))
            if arm is not None:
                if arm.alpha + arm.beta > 2:
                    return arm.sample(), arm.total_pulls
                return 0.0, arm.total_pulls
        except Exception:
            logger.warning("Thompson Sampling failed for %s - using rule-based scoring only", model.id)
        return 0.0, 0

    def _blend_rule_and_thompson(
        self,
        rule_score: float,
        thompson_score: float,
        thompson_observations: int,
    ) -> float:
        """Blend rule and Thompson scores based on observation maturity."""
        if thompson_observations >= self._THOMPSON_MATURITY_THRESHOLD:
            return 0.70 * thompson_score + 0.30 * rule_score
        score = rule_score
        if thompson_score > 0:
            score += thompson_score * 0.10
        return score + 0.15 * (1.0 - min(1.0, thompson_observations / 20))

    def _tier_score_adjustment(self, model: ModelInfo, task_type: TaskType) -> float:
        """Return score adjustment from configured model tiers."""
        if self._model_tiers:
            best_tier = self._find_cheapest_adequate_tier(task_type.value.lower())
            if best_tier is not None:
                tier_max_gb = best_tier.get("max_params_b", 999) * 0.6
                if model.memory_gb <= tier_max_gb:
                    return 0.05
                elif model.memory_gb > tier_max_gb * 2:
                    return -0.03
        return 0.0

    @staticmethod
    def _compute_implicit_feedback_boost(model_id: str, task_type: object) -> tuple[float, dict[str, Any] | None]:
        """Return the bounded additive implicit-feedback boost for routing."""
        try:
            from vetinari.learning.implicit_feedback import get_implicit_feedback_collector

            collector = get_implicit_feedback_collector()
            summary = collector.get_summary_for_routing(model_id, task_type)
        except Exception:
            logger.warning(
                "Implicit-feedback boost lookup failed for %s; falling back to zero boost",
                model_id,
                exc_info=True,
            )
            return 0.0, None

        total = summary.accept_count + summary.edit_count + summary.regenerate_count
        if total == 0:
            return 0.0, None

        confidence_weight = min(1.0, total / _IMPLICIT_BOOST_MATURITY)
        raw = _IMPLICIT_BOOST_CAP * (summary.acceptance_rate - 0.5) * 2 * confidence_weight
        boost = max(-_IMPLICIT_BOOST_CAP, min(_IMPLICIT_BOOST_CAP, raw))
        source = {
            "accept_count": summary.accept_count,
            "edit_count": summary.edit_count,
            "regenerate_count": summary.regenerate_count,
            "acceptance_rate": summary.acceptance_rate,
        }
        return boost, source

    @staticmethod
    def _calculate_confidence(scored: list[tuple[ModelInfo, float]]) -> float:
        """Delegate to the module-level ``calculate_confidence`` pure function.

        Exists as an instance method so tests and callers can access it via the
        router object without importing the scoring module directly.

        Args:
            scored: List of ``(ModelInfo, score)`` tuples sorted by score descending.

        Returns:
            Confidence value in [0.0, 1.0].
        """
        return calculate_confidence(scored)

    @staticmethod
    def _task_type_str(task_type: TaskType) -> str:
        """Return the string value used by learning stores."""
        return task_type.value if hasattr(task_type, "value") else str(task_type)

    @staticmethod
    def _confidence_computer(model: ModelInfo, task_type: TaskType) -> Any | None:
        """Return the confidence computer, or None when unavailable."""
        try:
            from vetinari.awareness.confidence import get_confidence_computer

            return get_confidence_computer()
        except Exception:
            logger.warning(
                "Confidence computer unavailable for model %s/%s - falling back to gap confidence only",
                model.id,
                task_type,
                exc_info=True,
            )
            return None

    @staticmethod
    def _thompson_confidence_data(model: ModelInfo, task_type_str: str) -> tuple[int, float, str | None]:
        """Return Thompson observations, mean, and last-data timestamp."""
        try:
            from vetinari.learning.model_selector import get_thompson_selector

            arm = get_thompson_selector()._arms.get(f"{model.id}:{task_type_str}")
            if arm is not None:
                return arm.total_pulls, arm.mean, getattr(arm, "last_updated", None)
        except Exception:
            logger.warning(
                "Thompson data unavailable for confidence computation on %s/%s",
                model.id,
                task_type_str,
                exc_info=True,
            )
        return 0, 0.5, None

    @staticmethod
    def _quality_scores_for_confidence(model: ModelInfo, task_type_str: str) -> list[float] | None:
        """Return recent quality scores used for confidence variance."""
        try:
            from vetinari.learning.quality_scorer import get_quality_scorer

            history = getattr(get_quality_scorer(), "_score_history", {})
            key = f"{model.id}:{task_type_str}"
            if key in history:
                return list(history[key])[-20:]
        except Exception:
            logger.warning(
                "Quality score history unavailable for confidence computation on %s/%s",
                model.id,
                task_type_str,
                exc_info=True,
            )
        return None

    @staticmethod
    def _log_unknown_protocols(protocols: list[UnknownSituationProtocol]) -> None:
        """Log unknown-situation protocols emitted by the confidence layer."""
        for protocol in protocols:
            logger.info(
                "[Awareness] %s - %s: %s",
                protocol.situation.value,
                protocol.message,
                protocol.action,
            )

    def _compute_awareness_confidence(
        self,
        model: ModelInfo,
        task_type: TaskType,
        gap_confidence: float,
    ) -> tuple[ConfidenceResult | None, list[UnknownSituationProtocol]]:
        """Compute multi-signal confidence for a model selection decision.

        Extracts Thompson arm data, quality history, and capability match
        to feed the awareness-layer ConfidenceComputer. Also detects and
        logs any unknown-situation protocols ("I don't know").

        Args:
            model: The selected model.
            task_type: The task type for this selection.
            gap_confidence: Gap-based confidence from ``calculate_confidence``.

        Returns:
            Tuple of (ConfidenceResult or None, list of unknown situation protocols).
        """
        computer = self._confidence_computer(model, task_type)
        if computer is None:
            return None, []

        task_type_str = self._task_type_str(task_type)
        thompson_observations, thompson_mean, last_data_timestamp = self._thompson_confidence_data(
            model,
            task_type_str,
        )
        capability_match = model.capabilities.matches_task(task_type)
        result = computer.compute(
            model_id=model.id,
            task_type=task_type_str,
            capability_match_score=capability_match,
            thompson_observations=thompson_observations,
            thompson_mean=thompson_mean,
            success_rate=model.success_rate,
            total_uses=model.total_uses,
            quality_scores=self._quality_scores_for_confidence(model, task_type_str),
        )

        protocols = computer.detect_unknown_situations(
            model_id=model.id,
            task_type=task_type_str,
            thompson_observations=thompson_observations,
            last_data_timestamp=last_data_timestamp,
            thompson_mean=thompson_mean,
            capability_match_score=capability_match,
        )
        self._log_unknown_protocols(protocols)
        return result, protocols

    def _find_cheapest_adequate_tier(self, task_key: str) -> dict[str, Any] | None:
        """Find the lowest-tier (cheapest) model tier adequate for a task type.

        Tiers are sorted by tier number ascending (cheapest first). Returns
        the first tier whose ``preferred_for`` list includes the task key.

        Args:
            task_key: Lowercase task type string (e.g. ``"coding"``, ``"reasoning"``).

        Returns:
            Tier config dict, or None if no tier matches.
        """
        for tier in sorted(self._model_tiers, key=lambda t: t.get("tier", 999)):
            preferred = tier.get("preferred_for", [])
            if task_key in preferred or "general" in preferred:
                return tier
        return None
