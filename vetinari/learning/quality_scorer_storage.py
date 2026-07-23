"""Persistence and history helpers for QualityScorer."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from vetinari.database import get_connection

if TYPE_CHECKING:
    from vetinari.learning.quality_scorer import QualityScore

logger = logging.getLogger(__name__)


class QualityScorerStorageMixin:
    """Mixin for QualityScorer persistence and history queries."""

    def _persist(self, score: QualityScore) -> None:
        """Persist a quality score to the unified SQLite database."""
        try:
            conn = get_connection()
            conn.execute(
                """INSERT INTO quality_scores
                   (task_id, model_id, task_type, overall_score, completeness_score,
                    correctness_score, style_score, llm_calibrated)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    score.task_id,
                    score.model_id,
                    score.task_type,
                    score.overall_score,
                    score.completeness,
                    score.correctness,
                    score.style,
                    1 if score.method == "llm" else 0,
                ),
            )
            conn.commit()
        except Exception as e:
            raise RuntimeError(f"[QualityScorer] persist failed: {e}") from e

    def get_history(self, model_id: str | None = None, task_type: str | None = None) -> list[QualityScore]:
        """Get scoring history from SQLite + in-memory cache, optionally filtered.

        Args:
            model_id: The model id.
            task_type: The task type.

        Returns:
            List of results.

        Raises:
            Exception: Propagates unexpected database construction failures.
        """
        try:
            from vetinari.learning.quality_scorer import QualityScore

            query = (
                "SELECT task_id, model_id, task_type, overall_score, completeness_score,"
                " correctness_score, style_score, llm_calibrated FROM quality_scores WHERE 1=1"
            )
            params: list = []
            if model_id:
                query += " AND model_id = ?"
                params.append(model_id)
            if task_type:
                query += " AND task_type = ?"
                params.append(task_type)
            query += " ORDER BY created_at DESC LIMIT 1000"

            conn = get_connection()
            rows = conn.execute(query, params).fetchall()

            scores = [
                QualityScore(
                    task_id=row[0],
                    model_id=row[1],
                    task_type=row[2],
                    overall_score=row[3],
                    completeness=row[4] or row[3],
                    correctness=row[5] or row[3],
                    style=row[6] or row[3],
                    method="llm" if row[7] else "heuristic",
                )
                for row in rows
            ]
            return scores
        except Exception:
            raise
            # Fall back to in-memory
            result = []
            if model_id:
                result = [s for s in result if s.model_id == model_id]
            if task_type:
                result = [s for s in result if s.task_type == task_type]
            logger.warning(
                "Quality score DB query failed for model_id=%r task_type=%r — falling back to in-memory scores (%d records)",
                model_id,
                task_type,
                len(result),
            )
            return result

    def get_model_average(self, model_id: str, task_type: str | None = None) -> float:
        """Get average quality score for a model (optionally filtered by task type).

        Args:
            model_id: The model id.
            task_type: The task type.

        Returns:
            Resolved model average value.
        """
        scores = self.get_history(model_id=model_id, task_type=task_type)
        if not scores:
            return 0.0  # No data — unmeasured, not "good"
        return sum(s.overall_score for s in scores) / len(scores)
