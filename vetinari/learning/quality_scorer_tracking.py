"""Score tracking and feedback helpers for QualityScorer."""

from __future__ import annotations

import logging
from collections import deque
from typing import TYPE_CHECKING, Any

logger = logging.getLogger(__name__)


class QualityScorerTrackingMixin:
    """Mixin for score history, distribution checks, and bandit feedback."""

    if TYPE_CHECKING:
        _FLAT_SCORE_THRESHOLD: Any
        _FLAT_SCORE_WINDOW: Any
        _VARIANCE_WARN_MIN_SCORES: Any
        _VARIANCE_WARN_THRESHOLD: Any
        _score_history: Any

    @staticmethod
    def _update_thompson_temperature(
        task_type: str, quality_score: float, temperature_used: float | None = None
    ) -> None:
        """Update Thompson strategy arms with quality feedback for temperature learning.

        Called after quality scoring to teach the bandit which temperature
        values produce better outputs per task type. Skipped when the actual
        temperature used during inference is not known — recording the wrong
        arm would corrupt the bandit's temperature-quality mapping.

        Args:
            task_type: The task type that was scored.
            quality_score: The overall quality score (0.0-1.0).
            temperature_used: The actual temperature used during inference.
                If None, the update is skipped to avoid recording wrong data.
        """
        if temperature_used is None:
            return  # Cannot record without knowing which temperature was used
        try:
            from vetinari.learning.model_selector import get_thompson_selector

            ts = get_thompson_selector()
            ts.update_strategy(
                "WORKER",
                task_type.lower() if task_type else "default",
                "temperature",
                temperature_used,
                quality_score,
            )
        except Exception:
            logger.warning(
                "Thompson temperature feedback skipped — selector unavailable",
                exc_info=True,
            )

    def _record_score_history(self, model_id: str, task_type: str, score: float) -> None:
        """Record a score in per-model+task history for trend analysis."""
        key = (model_id, task_type.lower())
        if key not in self._score_history:
            self._score_history[key] = deque(maxlen=50)
        self._score_history[key].append(score)

    def _is_score_distribution_flat(self, model_id: str, task_type: str) -> bool:
        """Check if last N scores are suspiciously flat (within threshold range).

        Returns True if the last _FLAT_SCORE_WINDOW scores for this model+task
        are all within _FLAT_SCORE_THRESHOLD of each other, indicating the
        heuristic scorer is not producing meaningful variance.
        """
        key = (model_id, task_type.lower())
        history = self._score_history.get(key)
        if not history or len(history) < self._FLAT_SCORE_WINDOW:
            return False
        recent = list(history)[-self._FLAT_SCORE_WINDOW :]
        score_range = max(recent) - min(recent)
        return score_range < self._FLAT_SCORE_THRESHOLD

    def _check_score_distribution(self, model_id: str, task_type: str) -> str:
        """Log WARNING if score variance is suspiciously low over many scores.

        Monitors per-model+task score distributions to catch broken scorers
        that produce identical scores regardless of output quality.
        """
        key = (model_id, task_type.lower())
        history = self._score_history.get(key)
        if not history or len(history) < self._VARIANCE_WARN_MIN_SCORES:
            return ""
        scores_list = list(history)
        mean = sum(scores_list) / len(scores_list)
        variance = sum((s - mean) ** 2 for s in scores_list) / len(scores_list)
        if variance < self._VARIANCE_WARN_THRESHOLD:
            issue = (
                f"Rejected: score variance too low for {model_id}/{task_type} "
                f"(variance={variance:.4f} over {len(scores_list)} scores)"
            )
            logger.warning(
                "[QualityScorer] Score variance too low for %s/%s: variance=%.4f over %d scores "
                "(mean=%.3f) — scores may not reflect actual quality differences",
                model_id,
                task_type,
                variance,
                len(scores_list),
                mean,
            )
            return issue
        return ""
