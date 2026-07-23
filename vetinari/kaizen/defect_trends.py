"""Defect Trend Analyzer — Track defect categories over time.

Surfaces patterns like "prompt_weakness defects increasing 15%
week-over-week for Builder on code tasks." Provides actionable
recommendations based on trend analysis.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from vetinari.validation import DefectCategory

logger = logging.getLogger(__name__)


CONCERNING_CHANGE_PCT = 0.15  # >15% week-over-week increase is concerning


@dataclass(frozen=True, slots=True)
class DefectTrend:
    """Trend data for a single defect category.

    Attributes:
        category: The defect category being tracked.
        current_count: Defect count in the current period.
        previous_count: Defect count in the previous period.
        change_pct: Percentage change from previous to current period.
        trend: Direction of trend ('increasing', 'decreasing', 'stable').
        is_concerning: Whether the trend exceeds the concerning threshold.
    """

    category: DefectCategory
    current_count: int
    previous_count: int
    change_pct: float
    trend: str  # "increasing" | "decreasing" | "stable"
    is_concerning: bool

    def __repr__(self) -> str:
        return f"DefectTrend(category={self.category!r}, trend={self.trend!r}, is_concerning={self.is_concerning!r})"


@dataclass(frozen=True, slots=True)
class DefectHotspot:
    """An agent+mode combination with a high defect rate.

    Attributes:
        agent_type: The agent type with high defect rate.
        mode: The agent mode with high defect rate.
        defect_category: The dominant defect category for this hotspot.
        defect_count: Number of defects in the analysis period.
        defect_rate: Defect rate as proportion of total tasks for this combo.
    """

    agent_type: str
    mode: str
    defect_category: DefectCategory
    defect_count: int
    defect_rate: float

    def __repr__(self) -> str:
        return (
            f"DefectHotspot(agent_type={self.agent_type!r}, mode={self.mode!r},"
            f" defect_category={self.defect_category!r})"
        )


@dataclass
class DefectTrendReport:
    """Report of defect trends across all categories.

    Attributes:
        trends: Per-category trend data.
        hotspots: Agent+mode combinations with highest defect rates.
        top_defect_category: Category with the highest current count.
        recommendations: Actionable suggestions based on trend analysis.
    """

    trends: dict[DefectCategory, DefectTrend] = field(default_factory=dict)
    hotspots: list[DefectHotspot] = field(default_factory=list)
    top_defect_category: DefectCategory | None = None
    recommendations: list[str] = field(default_factory=list)

    def __repr__(self) -> str:
        return f"DefectTrendReport(top_defect_category={self.top_defect_category!r}, hotspots={len(self.hotspots)})"


class DefectTrendAnalyzer:
    """Tracks defect categories over time to surface systemic issues.

    Analyzes weekly defect counts to identify trends, hotspots, and
    generate actionable recommendations for improvement.
    """

    def analyze_trends(
        self,
        weekly_counts: list[dict[DefectCategory, int]],
        hotspots: list[DefectHotspot] | None = None,
    ) -> DefectTrendReport:
        """Analyze defect category trends from weekly count data.

        Args:
            weekly_counts: List of dicts mapping DefectCategory to count
                for each week. Index 0 is oldest, -1 is most recent.
            hotspots: Optional pre-computed hotspot data.

        Returns:
            A DefectTrendReport with trends, hotspots, and recommendations.
        """
        trends: dict[DefectCategory, DefectTrend] = {}

        for category in DefectCategory:
            counts = [week.get(category, 0) for week in weekly_counts]
            if len(counts) < 2:
                continue

            current = counts[-1]
            previous = counts[-2]

            # When previous is 0 and current is also 0, there is no change.
            # When previous is 0 and current > 0, this is a new/re-emerging
            # defect category — treat as a large positive change rather than
            # substituting 1 (which would produce 0% change and mask the signal).
            if previous == 0 and current == 0:
                change_pct = 0.0
            elif previous == 0:
                # New defect: cap at +100% to avoid infinite values while
                # still triggering the concerning threshold.
                change_pct = 1.0
            else:
                change_pct = (current - previous) / previous

            if change_pct > 0.1:
                trend_direction = "increasing"
            elif change_pct < -0.1:
                trend_direction = "decreasing"
            else:
                trend_direction = "stable"

            trends[category] = DefectTrend(
                category=category,
                current_count=current,
                previous_count=previous,
                change_pct=change_pct,
                trend=trend_direction,
                is_concerning=change_pct > CONCERNING_CHANGE_PCT,
            )

        # Top defect category by current count — only meaningful when at least
        # one category has a non-zero count.  All-zero counts mean no defects
        # this period; report None rather than picking an arbitrary category.
        top_category = None
        if trends:
            best = max(trends.values(), key=lambda t: t.current_count)
            if best.current_count > 0:
                top_category = best.category

        recommendations = self._generate_recommendations(trends)

        return DefectTrendReport(
            trends=trends,
            hotspots=hotspots or [],
            top_defect_category=top_category,
            recommendations=recommendations,
        )

    def _generate_recommendations(
        self,
        trends: dict[DefectCategory, DefectTrend],
    ) -> list[str]:
        """Generate actionable recommendations from trends.

        Args:
            trends: Per-category trend data.

        Returns:
            List of recommendation strings.
        """
        recs: list[str] = []
        for trend in trends.values():
            if not trend.is_concerning:
                continue

            match trend.category:
                case DefectCategory.HALLUCINATION:
                    recs.append(
                        f"Hallucination rate increasing {trend.change_pct:.0%} — "
                        "consider stricter verification or model change",
                    )
                case DefectCategory.BAD_SPEC:
                    recs.append(
                        f"Bad spec rate increasing {trend.change_pct:.0%} — "
                        "improve intake questions or add clarification step",
                    )
                case DefectCategory.PROMPT_WEAKNESS:
                    recs.append(
                        f"Prompt weakness increasing {trend.change_pct:.0%} — trigger PromptEvolver for affected modes",
                    )
                case DefectCategory.WRONG_MODEL:
                    recs.append(
                        f"Wrong model rate increasing {trend.change_pct:.0%} — "
                        "review model routing rules and capability profiles",
                    )
                case DefectCategory.INSUFFICIENT_CONTEXT:
                    recs.append(
                        f"Context gaps increasing {trend.change_pct:.0%} — expand context retrieval in the pipeline",
                    )
                case DefectCategory.INTEGRATION_ERROR:
                    recs.append(
                        f"Integration errors increasing {trend.change_pct:.0%} — "
                        "add integration smoke tests to quality gate",
                    )
                case DefectCategory.COMPLEXITY_UNDERESTIMATE:
                    recs.append(
                        f"Complexity underestimates increasing {trend.change_pct:.0%} — "
                        "improve task decomposition granularity in Planner",
                    )
        return recs


# ── Helpers used by PDCAController ───────────────────────────────────────────


def is_valid_category(category_str: str) -> bool:
    """Check if a string is a valid DefectCategory value.

    Args:
        category_str: The string to check.

    Returns:
        True if the string maps to a DefectCategory member.
    """
    try:
        DefectCategory(category_str)
        return True
    except ValueError:
        logger.debug("Skipping unknown defect category %r", category_str)
        return False


def build_hypothesis(category: DefectCategory, change_pct: float) -> str:
    """Build a human-readable improvement hypothesis for a worsening trend.

    Args:
        category: The defect category that is worsening.
        change_pct: The week-over-week change percentage.

    Returns:
        A hypothesis string suitable for ImprovementLog.propose().
    """
    pct_str = f"{change_pct:.0%}"

    match category:
        case DefectCategory.HALLUCINATION:
            return (
                f"Hallucination rate increased {pct_str} — "
                "add stricter output verification or switch to a more grounded model"
            )
        case DefectCategory.BAD_SPEC:
            return f"Bad spec rate increased {pct_str} — add clarification prompts to the intake pipeline"
        case DefectCategory.PROMPT_WEAKNESS:
            return f"Prompt weakness increased {pct_str} — trigger PromptEvolver A/B testing for affected agent modes"
        case DefectCategory.WRONG_MODEL:
            return f"Wrong model rate increased {pct_str} — review model routing rules and capability matching"
        case DefectCategory.INSUFFICIENT_CONTEXT:
            return f"Context gaps increased {pct_str} — expand context retrieval window or add memory pre-fetch"
        case DefectCategory.INTEGRATION_ERROR:
            return f"Integration errors increased {pct_str} — add integration smoke tests before quality gate"
        case DefectCategory.COMPLEXITY_UNDERESTIMATE:
            return f"Complexity underestimates increased {pct_str} — improve Planner task decomposition granularity"
        case _:
            return (
                f"{category.value} defects increased {pct_str} — investigate root cause and apply targeted mitigation"
            )
