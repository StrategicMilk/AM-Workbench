"""Retraining recommendation helpers for TrainingManager."""

from __future__ import annotations

import fnmatch
from typing import TYPE_CHECKING, Any

from vetinari.boundary_guards import require_score_in_range

if TYPE_CHECKING:
    from .training_manager import RetrainingRecommendation

_MAX_RETRAINING_RECORDS = 5000
_MIN_RETRAINING_RECORDS = 3


class TrainingManagerRetrainingMixin:
    """Evaluate quality degradation and retraining recommendations."""

    if TYPE_CHECKING:
        _get_collector: Any

    def should_retrain(self, model_id: str, task_type: str) -> RetrainingRecommendation:
        """Evaluate whether a model warrants retraining for a task type.

        Compares the rolling average quality of recent records against
        ``_BASELINE_QUALITY``. Recommends retraining when degradation exceeds
        ``_RETRAIN_DEGRADATION_THRESHOLD``.

        Args:
            model_id: Model to evaluate. Wildcards are supported.
            task_type: Task category to filter records by. Wildcards are
                supported.

        Returns:
            RetrainingRecommendation for the model/task pair.

        Raises:
            RuntimeError: If training records cannot be loaded from the
                collector, because a recommendation would otherwise be based on
                untrusted empty state.
        """
        from .training_manager import (
            _BASELINE_QUALITY,
            _RETRAIN_DEGRADATION_THRESHOLD,
            RetrainingRecommendation,
        )

        collector = self._get_collector()
        try:
            all_records = collector._load_all()
        except Exception as exc:
            raise RuntimeError(
                "Cannot evaluate retraining recommendation because training records are unavailable"
            ) from exc

        # fnmatch treats non-wildcard strings as exact matches, while allowing
        # callers to evaluate model or task families with patterns such as "*".
        recent_records = all_records[-_MAX_RETRAINING_RECORDS:]
        relevant = []
        scores: list[float] = []
        for record in recent_records:
            if fnmatch.fnmatch(record.model_id, model_id) and fnmatch.fnmatch(record.task_type, task_type):
                score = require_score_in_range(
                    record.score,
                    "training_manager.retraining_record",
                    field_name="score",
                )
                relevant.append(record)
                scores.append(score)

        if not relevant:
            return RetrainingRecommendation(
                model_id=model_id,
                task_type=task_type,
                current_avg_quality=0.0,
                baseline_quality=_BASELINE_QUALITY,
                degradation=0.0,
                recommended=False,
                reason="No records found for this model/task combination.",
            )

        if len(relevant) < _MIN_RETRAINING_RECORDS:
            return RetrainingRecommendation(
                model_id=model_id,
                task_type=task_type,
                current_avg_quality=round(sum(scores) / len(scores), 4),
                baseline_quality=_BASELINE_QUALITY,
                degradation=0.0,
                recommended=False,
                reason=f"Insufficient bounded records for this model/task combination ({len(relevant)} of {_MIN_RETRAINING_RECORDS}).",
            )

        current_avg = round(sum(scores) / len(scores), 4)
        degradation = round(max(0.0, (_BASELINE_QUALITY - current_avg) / _BASELINE_QUALITY), 4)
        recommended = degradation >= _RETRAIN_DEGRADATION_THRESHOLD

        if recommended:
            reason = (
                f"Quality degraded {degradation * 100:.1f}% below baseline "
                f"({current_avg:.3f} vs {_BASELINE_QUALITY:.3f} baseline). "
                f"Retraining on {len(relevant)} records recommended."
            )
        else:
            reason = (
                f"Quality acceptable: {current_avg:.3f} "
                f"(baseline {_BASELINE_QUALITY:.3f}, "
                f"degradation {degradation * 100:.1f}%)."
            )

        return RetrainingRecommendation(
            model_id=model_id,
            task_type=task_type,
            current_avg_quality=current_avg,
            baseline_quality=_BASELINE_QUALITY,
            degradation=degradation,
            recommended=recommended,
            reason=reason,
        )
