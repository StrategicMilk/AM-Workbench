"""Extracted implementation helpers for pdca.py."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from vetinari.kaizen.defect_trends import DefectHotspot, build_hypothesis, is_valid_category
from vetinari.kaizen.improvement_log import ImprovementStatus
from vetinari.validation import DefectCategory

logger = logging.getLogger(__name__)


class PDCACycleMixin:
    """Shared method implementations for the compatibility wrapper."""

    if TYPE_CHECKING:
        _applicators: Any
        _log: Any
        _trend_analyzer: Any
        confirm_and_persist: Any

    def check_trends_and_propose(
        self,
        weekly_counts: list[dict[str, int]] | None = None,
        hotspot_data: list[dict[str, Any]] | None = None,
    ) -> list[str]:
        """Analyze defect trends and propose worsening-metric improvements.

        Args:
            weekly_counts: Weekly counts value consumed by check_trends_and_propose().
            hotspot_data: Structured data consumed by the operation.

        Returns:
            Value produced for the caller.
        """
        if weekly_counts is None:
            weekly_counts = self._log.get_weekly_defect_counts(weeks=4)
        if not weekly_counts or len(weekly_counts) < 2:
            logger.info("Insufficient defect data for trend analysis (need >= 2 weeks)")
            return []
        typed_counts: list[dict[DefectCategory, int]] = []
        for week in weekly_counts:
            typed_week: dict[DefectCategory, int] = {}
            for cat_str, count in week.items():
                try:
                    typed_week[DefectCategory(cat_str)] = count
                except ValueError:
                    logger.warning("Skipping unknown defect category: %s", cat_str)
            typed_counts.append(typed_week)
        hotspots: list[DefectHotspot] | None = None
        if hotspot_data:
            hotspots = [
                DefectHotspot(
                    agent_type=h["agent_type"],
                    mode=h["mode"],
                    defect_category=DefectCategory(h["category"]),
                    defect_count=h["count"],
                    defect_rate=h.get("defect_rate", 0.0),
                )
                for h in hotspot_data
                if is_valid_category(h.get("category", ""))
            ]
        report = self._trend_analyzer.analyze_trends(typed_counts, hotspots)
        proposed_ids: list[str] = []
        for trend in report.trends.values():
            if not trend.is_concerning:
                continue
            hypothesis = build_hypothesis(trend.category, trend.change_pct)
            imp_id = self._log.propose(
                hypothesis=hypothesis,
                metric="defect_count",
                baseline=float(trend.current_count),
                target=float(max(trend.previous_count - 1, 0)),
                applied_by="pdca_trend_monitor",
                rollback_plan="Revert to previous configuration for this defect category",
            )
            proposed_ids.append(imp_id)
            logger.info(
                "Auto-proposed improvement %s for worsening %s trend (+%.0f%%)",
                imp_id,
                trend.category.value,
                trend.change_pct * 100,
            )
        logger.info(
            "Trend analysis complete: %d improvement(s) proposed",
            len(proposed_ids),
        )
        return proposed_ids

    def run_check_phase(self) -> list[str]:
        """Run the Check phase: evaluate active improvements and handle results.

        Evaluates all active improvements.  Confirmed ones are persisted;
        failed ones are logged.  This is the automated Check-Act bridge.

        Returns:
            List of improvement IDs that were confirmed and persisted.
        """
        active = self._log.get_active_improvements()
        confirmed_ids: list[str] = []

        now_utc = datetime.now(timezone.utc)
        for improvement in active:
            observations = self._log.get_observations(improvement.id)
            if not observations:
                # Skip improvements that are still within their observation window.
                # If the window has expired with no observations, revert to PROPOSED
                # so the improvement does not remain stuck in ACTIVE indefinitely.
                if improvement.applied_at is not None:
                    window_expires = improvement.applied_at + improvement.observation_window
                    if now_utc > window_expires:
                        logger.warning(
                            "Improvement %s stuck in ACTIVE: observation window expired "
                            "with no observations — reverting to PROPOSED for retry",
                            improvement.id,
                        )
                        try:
                            self._log.revert_to_proposed(improvement.id)
                        except Exception:
                            logger.error(
                                "Failed to revert stuck improvement %s to PROPOSED",
                                improvement.id,
                                exc_info=True,
                            )
                continue
            result = self._log.evaluate(improvement.id)
            if result == ImprovementStatus.CONFIRMED:
                self.confirm_and_persist(improvement.id)
                confirmed_ids.append(improvement.id)
            elif result == ImprovementStatus.FAILED:
                # Revert the applicator's changes
                applicator = self._applicators.get(improvement.metric)
                from vetinari.kaizen.pdca import ThresholdApplicator

                if isinstance(applicator, ThresholdApplicator):
                    applicator.revert_override(improvement.id)
                logger.info(
                    "Improvement %s failed evaluation — changes reverted",
                    improvement.id,
                )

        from vetinari.kaizen.knowledge_compactor import run_compaction_step

        run_compaction_step()

        return confirmed_ids
