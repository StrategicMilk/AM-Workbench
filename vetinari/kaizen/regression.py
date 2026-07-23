"""Regression Detector — Monitors confirmed improvements for degradation.

Continuously monitors confirmed improvements. If a metric degrades 10%+
vs post-improvement baseline, flags as regression. Critical regressions
(worse than pre-improvement baseline) trigger auto-revert.
"""

from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass

from vetinari.kaizen.improvement_log import (
    ImprovementLog,
)
from vetinari.kaizen.improvement_log_evaluation import is_lower_is_better

logger = logging.getLogger(__name__)


REGRESSION_THRESHOLD_PCT = 0.10  # 10% degradation from post-improvement level


@dataclass(frozen=True, slots=True)
class RegressionAlert:
    """Alert raised when a confirmed improvement shows regression.

    Attributes:
        improvement_id: The improvement that regressed.
        metric: Which metric regressed.
        expected: Post-improvement baseline (actual_value at confirmation).
        actual: Current measured metric value.
        degradation_pct: Percentage degradation from expected.
        severity: 'warning' for mild regression, 'critical' if worse than
            pre-improvement baseline.
    """

    improvement_id: str
    metric: str
    expected: float
    actual: float
    degradation_pct: float
    severity: str  # "warning" or "critical"

    def __repr__(self) -> str:
        return (
            f"RegressionAlert(improvement_id={self.improvement_id!r},"
            f" metric={self.metric!r}, severity={self.severity!r})"
        )


class RegressionDetector:
    """Monitors confirmed improvements for regression.

    Runs periodically (daily). Checks all CONFIRMED improvements by
    comparing recent observations against the post-improvement actual_value.
    If degradation exceeds 10%, raises a warning. If the metric is worse
    than the pre-improvement baseline, triggers auto-revert.

    Args:
        improvement_log: The ImprovementLog to monitor.
    """

    def __init__(self, improvement_log: ImprovementLog) -> None:
        self._log = improvement_log

    def check_regressions(self) -> list[RegressionAlert]:
        """Check confirmed improvements for metric degradation.

        Returns:
            Value produced for the caller.
        """
        alerts: list[RegressionAlert] = []
        confirmed = self._log.get_confirmed_improvements()
        for improvement in confirmed:
            recent = self._log.get_observations(improvement.id, days=7)
            if not recent:
                continue
            recent_avg = statistics.mean([o.metric_value for o in recent])
            post_improvement_baseline = improvement.actual_value
            if post_improvement_baseline is None:
                continue
            lower_better = is_lower_is_better(improvement.metric)
            if lower_better:
                degraded = recent_avg > post_improvement_baseline * (1.0 + REGRESSION_THRESHOLD_PCT)
                degradation_pct = (
                    (recent_avg - post_improvement_baseline) / post_improvement_baseline
                    if post_improvement_baseline > 0
                    else 0.0
                )
                is_critical = recent_avg > improvement.baseline_value
            else:
                degraded = recent_avg < post_improvement_baseline * (1.0 - REGRESSION_THRESHOLD_PCT)
                degradation_pct = (
                    (post_improvement_baseline - recent_avg) / post_improvement_baseline
                    if post_improvement_baseline > 0
                    else 0.0
                )
                is_critical = recent_avg < improvement.baseline_value
            if degraded:
                severity = "critical" if is_critical else "warning"
                alert = RegressionAlert(
                    improvement_id=improvement.id,
                    metric=improvement.metric,
                    expected=post_improvement_baseline,
                    actual=recent_avg,
                    degradation_pct=degradation_pct,
                    severity=severity,
                )
                alerts.append(alert)
                if is_critical:
                    logger.critical(
                        "CRITICAL REGRESSION: %s worse than pre-improvement baseline "
                        "(recent=%.3f, baseline=%.3f, metric=%s)",
                        improvement.id,
                        recent_avg,
                        improvement.baseline_value,
                        improvement.metric,
                    )
                    self._auto_revert(improvement.id)
                else:
                    logger.warning(
                        "Regression warning: %s degraded %.1f%% from post-improvement level "
                        "(recent=%.3f, expected=%.3f, metric=%s)",
                        improvement.id,
                        degradation_pct * 100,
                        recent_avg,
                        post_improvement_baseline,
                        improvement.metric,
                    )
        logger.info(
            "Regression check completed: %d alerts from %d confirmed improvements",
            len(alerts),
            len(confirmed),
        )
        return alerts

    def _auto_revert(self, improvement_id: str) -> None:
        """Execute auto-revert for a critically regressed improvement.

        Args:
            improvement_id: The improvement to revert.
        """
        improvement = self._log.get_improvement(improvement_id)
        if improvement is None:
            return
        logger.warning(
            "Auto-reverting improvement %s: %s",
            improvement_id,
            improvement.rollback_plan,
        )
        self._log.revert(improvement_id)
