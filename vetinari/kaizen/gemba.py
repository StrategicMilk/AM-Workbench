"""Auto-Gemba Walk — Systematic Execution Review.

Automated review of actual execution history to surface improvement
opportunities. Implements the "go see the actual work" principle from
lean manufacturing — the system reviews its own traces to find patterns
that humans might miss.

Runs on-demand or on a weekly schedule. Findings are proposed as
improvements in the ImprovementLog.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from vetinari.kaizen.improvement_log import ImprovementLog

logger = logging.getLogger(__name__)


REWORK_RATE_THRESHOLD = 0.20  # >20% rework rate is concerning
VALUE_ADD_RATIO_THRESHOLD = 0.50  # <50% value-adding time is wasteful
DOMINANT_CAUSE_THRESHOLD = 0.40  # >40% defects from one cause is systemic
RECURRING_FAILURE_MIN = 3  # Minimum occurrences to count as recurring


@dataclass(frozen=True, slots=True)
class GembaFinding:
    """A single finding from the gemba walk.

    Attributes:
        type: Category of finding (recurring_failure, high_rework, low_value_add, etc.).
        detail: Human-readable description of what was observed.
        proposed_improvement: Suggested improvement hypothesis.
        metric: Which metric this finding relates to.
        baseline: Current metric value.
        target: Desired metric value after improvement.
    """

    type: str
    detail: str
    proposed_improvement: str
    metric: str
    baseline: float
    target: float

    def __repr__(self) -> str:
        return (
            f"GembaFinding(type={self.type!r}, metric={self.metric!r}, "
            f"baseline={self.baseline!r}, target={self.target!r})"
        )


@dataclass
class GembaReport:
    """Report produced by a gemba walk.

    Attributes:
        findings: List of findings from this walk.
        timestamp: When this walk was performed.
        improvements_proposed: Number of improvements proposed to the log.
    """

    findings: list[GembaFinding] = field(default_factory=list)
    timestamp: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc),
    )
    improvements_proposed: int = 0

    def __repr__(self) -> str:
        return f"GembaReport(findings={len(self.findings)}, improvements_proposed={self.improvements_proposed!r})"


class AutoGembaWalk:
    """Automated execution review — 'go see the actual work' principle.

    Reviews execution history and surfaces improvement opportunities
    by looking for recurring failures, high rework rates, low value-add
    ratios, and dominant defect causes. Each finding is proposed as an
    improvement in the ImprovementLog.

    Args:
        improvement_log: The ImprovementLog to propose improvements to.
    """

    def __init__(self, improvement_log: ImprovementLog) -> None:
        self._improvement_log = improvement_log
        self.latest_report: GembaReport | None = None

    def run(
        self,
        failure_patterns: list[dict] | None = None,
        rework_stats: dict | None = None,
        value_stream_stats: dict | None = None,
        defect_distribution: dict | None = None,
    ) -> GembaReport:
        """Perform a gemba walk using provided execution statistics.

        Each parameter is optional — the walk inspects whatever data is
        available. In production, callers gather this data from episode
        memory, value stream analyzer, and root cause analyzer before
        invoking the walk.

        Args:
            failure_patterns: List of dicts with keys: model, task_type,
                frequency, avg_score. Recurring failure patterns.
            rework_stats: Dict with keys: rework_count, total_tasks.
                Rework rate statistics.
            value_stream_stats: Dict with keys: value_add_ratio,
                avg_lead_time, bottleneck_station. Value stream metrics.
            defect_distribution: Dict mapping defect category strings to
                their proportions (0.0-1.0). Root cause distribution.

        Returns:
            A GembaReport with all findings and the count of proposed improvements.
        """
        findings: list[GembaFinding] = []

        # 1. Recurring failure patterns
        if failure_patterns:
            findings.extend(self._check_failure_patterns(failure_patterns))

        # 2. Rework identification
        if rework_stats:
            finding = self._check_rework_rate(rework_stats)
            if finding:
                findings.append(finding)

        # 3. Value stream analysis
        if value_stream_stats:
            finding = self._check_value_add_ratio(value_stream_stats)
            if finding:
                findings.append(finding)

        # 4. Dominant defect cause
        if defect_distribution:
            finding = self._check_dominant_cause(defect_distribution)
            if finding:
                findings.append(finding)

        # Propose improvements to ImprovementLog
        proposed_count = 0
        for finding in findings:
            self._improvement_log.propose(
                hypothesis=finding.proposed_improvement,
                metric=finding.metric,
                baseline=finding.baseline,
                target=finding.target,
                applied_by="gemba_walk",
                rollback_plan="Revert to previous configuration",
            )
            proposed_count += 1

        report = GembaReport(
            findings=findings,
            improvements_proposed=proposed_count,
        )
        self.latest_report = report

        logger.info(
            "Gemba walk completed: %d findings, %d improvements proposed",
            len(findings),
            proposed_count,
        )
        return report

    # ── Private analysis methods ───────────────────────────────────────

    @staticmethod
    def _check_failure_patterns(
        patterns: list[dict],
    ) -> list[GembaFinding]:
        """Identify recurring failure patterns.

        Args:
            patterns: List of failure pattern dicts with model, task_type,
                frequency, avg_score keys.

        Returns:
            List of GembaFinding for patterns exceeding the threshold.
        """
        findings = []
        for pattern in patterns:
            frequency = pattern.get("frequency", 0)
            if frequency >= RECURRING_FAILURE_MIN:
                model = pattern.get("model", "unknown")
                task_type = pattern.get("task_type", "unknown")
                avg_score = pattern.get("avg_score", 0.0)
                findings.append(
                    GembaFinding(
                        type="recurring_failure",
                        detail=f"Model {model} fails {frequency}x on {task_type}",
                        proposed_improvement=f"Try different model for {task_type}",
                        metric="quality",
                        baseline=avg_score,
                        target=avg_score + 0.15,
                    )
                )
        return findings

    @staticmethod
    def _check_rework_rate(stats: dict) -> GembaFinding | None:
        """Check if rework rate exceeds threshold.

        Args:
            stats: Dict with rework_count and total_tasks.

        Returns:
            A GembaFinding if rework rate exceeds 20%, else None.
        """
        total = stats.get("total_tasks", 0)
        if total == 0:
            return None
        rework_rate = stats.get("rework_count", 0) / total
        if rework_rate > REWORK_RATE_THRESHOLD:
            return GembaFinding(
                type="high_rework",
                detail=f"Rework rate is {rework_rate:.0%} — target is <20%",
                proposed_improvement="Strengthen prevention gate or improve task specifications",
                metric="rework_rate",
                baseline=rework_rate,
                target=0.15,
            )
        return None

    @staticmethod
    def _check_value_add_ratio(stats: dict) -> GembaFinding | None:
        """Check if value-add ratio is too low.

        Args:
            stats: Dict with value_add_ratio, avg_lead_time, bottleneck_station.

        Returns:
            A GembaFinding if value-add ratio is below 50%, else None.
        """
        ratio = stats.get("value_add_ratio", 1.0)
        if ratio < VALUE_ADD_RATIO_THRESHOLD:
            bottleneck = stats.get("bottleneck_station", "unknown")
            lead_time = stats.get("avg_lead_time", 0.0)
            return GembaFinding(
                type="low_value_add",
                detail=f"Only {ratio:.0%} of time is value-adding",
                proposed_improvement=f"Reduce queue time at {bottleneck}",
                metric="latency",
                baseline=lead_time,
                target=lead_time * 0.7,
            )
        return None

    @staticmethod
    def _check_dominant_cause(distribution: dict) -> GembaFinding | None:
        """Check if one defect category dominates.

        Args:
            distribution: Dict mapping defect category strings to proportions.

        Returns:
            A GembaFinding if any category exceeds 40%, else None.
        """
        if not distribution:
            return None
        dominant = max(distribution, key=distribution.get)
        proportion = distribution[dominant]
        if proportion > DOMINANT_CAUSE_THRESHOLD:
            return GembaFinding(
                type="dominant_defect_cause",
                detail=f"{dominant} causes {proportion:.0%} of defects",
                proposed_improvement=f"Address systemic {dominant} issue",
                metric="quality",
                baseline=proportion,
                target=proportion * 0.5,
            )
        return None
