"""Kaizen wiring — connects PDCA, regression detection, and defect trends to the scheduler.

This module is the call site for all scheduled kaizen work. The background
scheduler (daily/weekly) calls the three ``scheduled_*`` functions here, which
in turn drive the PDCA feedback loop, regression monitoring, and defect trend
analysis. Together they make Vetinari self-correcting over time.

Pipeline role: sits between the background scheduler and the kaizen subsystems.
Scheduler → **wiring** → (PDCAController | RegressionDetector | DefectTrendAnalyzer)
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import cast

from vetinari.boundary_guards import assert_dependency_success, clamp_score
from vetinari.constants import get_user_dir
from vetinari.kaizen.defect_trends import DefectHotspot, DefectTrendAnalyzer, DefectTrendReport
from vetinari.kaizen.improvement_log import ImprovementLog
from vetinari.kaizen.pdca import PDCAController
from vetinari.kaizen.regression import RegressionAlert, RegressionDetector
from vetinari.validation import DefectCategory

logger = logging.getLogger(__name__)

PDCA_APPLY_RECEIPTS_FILENAME = "kaizen_apply_receipts.jsonl"


@dataclass(frozen=True, slots=True)
class KaizenScheduledTask:
    """Scheduler-facing registration for a Kaizen recurring task."""

    name: str
    cadence: str
    callback: Callable[[str | None], object]


def _pdca_receipt_path(db_path: str | None) -> str:
    """Return the receipt path next to the scheduled PDCA database."""
    return str(get_user_dir() / PDCA_APPLY_RECEIPTS_FILENAME) if db_path is None else str(db_path) + ".receipts.jsonl"


def _int_value(value: object, default: int = 0) -> int:
    """Convert persisted numeric values without accepting bool as an integer."""
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str | bytes):
        try:
            return int(value)
        except ValueError:
            logger.warning("Invalid persisted integer value %r; using default %d", value, default)
            return default
    return default


# ── Private helpers ───────────────────────────────────────────────────────────


def _build_weekly_counts(
    raw_weeks: list[dict[str, int]],
) -> list[dict[DefectCategory, int]]:
    """Convert raw string-keyed weekly counts to DefectCategory-keyed dicts.

    ``ImprovementLog.get_weekly_defect_counts()`` stores category strings;
    ``DefectTrendAnalyzer.analyze_trends()`` expects ``DefectCategory`` enum
    keys. Unknown category strings are silently dropped to stay forward-
    compatible with new categories not yet in the enum.

    Args:
        raw_weeks: List of dicts mapping category string to count, oldest first.

    Returns:
        Same structure with string keys replaced by ``DefectCategory`` enum values.
    """
    typed: list[dict[DefectCategory, int]] = []
    for week in raw_weeks:
        converted: dict[DefectCategory, int] = {}
        for category_str, count in week.items():
            try:
                converted[DefectCategory(category_str)] = count
            except ValueError:
                logger.warning(
                    "Unknown defect category %r in weekly counts — skipping entry",
                    category_str,
                )
        typed.append(converted)
    return typed


def _build_hotspots(raw_hotspots: list[dict[str, object]]) -> list[DefectHotspot]:
    """Convert raw hotspot dicts from ImprovementLog to DefectHotspot objects.

    Args:
        raw_hotspots: List of dicts with keys agent_type, mode, category, count.

    Returns:
        List of DefectHotspot dataclass instances.
    """
    hotspots: list[DefectHotspot] = []
    for h in raw_hotspots:
        try:
            category = DefectCategory(str(h["category"]))
        except ValueError:
            logger.warning(
                "Unknown defect category %r in hotspot — skipping entry",
                h.get("category"),
            )
            continue
        count = _int_value(h.get("count", 0))
        total = max(count, 1)
        raw_rate = h.get("defect_rate", count / total)
        hotspots.append(
            DefectHotspot(
                agent_type=str(h["agent_type"]),
                mode=str(h["mode"]),
                defect_category=category,
                defect_count=count,
                defect_rate=clamp_score(raw_rate, field_name="defect_rate"),
            )
        )
    return hotspots


# ── Scheduled entry points ────────────────────────────────────────────────────


def scheduled_pdca_check(db_path: str | None = None) -> list[str]:
    """Run the PDCA check — propose improvements from worsening defect trends.

    Intended to be called daily by the background scheduler. Creates a
    ``PDCAController`` backed by the given database and calls
    ``check_trends_and_propose()``, which inspects defect hotspots and
    generates improvement hypotheses for each actionable hotspot. Only
    hotspots with valid ``DefectCategory`` values produce proposals.

    Args:
        db_path: Path to the kaizen SQLite database. Defaults to
            ``get_user_dir() / "kaizen.db"`` when ``None``.

    Returns:
        List of improvement IDs that were proposed during this check.
    """
    resolved_path = db_path if db_path is not None else str(get_user_dir() / "kaizen.db")
    improvement_log = ImprovementLog(resolved_path)
    controller = PDCAController(improvement_log, receipt_path=_pdca_receipt_path(db_path))
    controller.run_check_phase()
    proposed_ids = controller.check_trends_and_propose()
    logger.info(
        "Scheduled PDCA check complete — proposed %d improvement(s)",
        len(proposed_ids),
    )
    return [str(proposed_id) for proposed_id in proposed_ids]


def scheduled_regression_check(db_path: str | None = None) -> list[RegressionAlert]:
    """Check confirmed improvements for metric regression.

    Intended to be called daily by the background scheduler. Compares recent
    observations against the post-improvement baseline for every confirmed
    improvement. Logs a WARNING for each alert so operators can investigate
    without querying the database directly.

    Args:
        db_path: Path to the kaizen SQLite database. Defaults to
            ``get_user_dir() / "kaizen.db"`` when ``None``.

    Returns:
        List of ``RegressionAlert`` instances for improvements showing regression.
    """
    resolved_path = db_path if db_path is not None else str(get_user_dir() / "kaizen.db")
    improvement_log = ImprovementLog(resolved_path)
    detector = RegressionDetector(improvement_log)
    alerts = detector.check_regressions()
    for alert in alerts:
        logger.warning(
            "Regression detected for improvement %s — metric=%s severity=%s "
            "(expected=%.4f, actual=%.4f, degradation=%.1f%%)",
            alert.improvement_id,
            alert.metric,
            alert.severity,
            alert.expected,
            alert.actual,
            alert.degradation_pct * 100,
        )
    logger.info(
        "Scheduled regression check complete — %d alert(s) raised",
        len(alerts),
    )
    return cast(list[RegressionAlert], alerts)


def scheduled_trend_analysis(db_path: str | None = None) -> DefectTrendReport:
    """Analyze defect category trends and surface actionable recommendations.

    Intended to be called weekly by the background scheduler. Reads the last
    four weeks of defect counts from the improvement log, converts them to
    typed ``DefectCategory`` keys, and runs the full trend analysis. Concerning
    trends (>15% week-over-week increase) are logged at WARNING level.

    Args:
        db_path: Path to the kaizen SQLite database. Defaults to
            ``get_user_dir() / "kaizen.db"`` when ``None``.

    Returns:
        A ``DefectTrendReport`` with per-category trends, hotspots, and
        actionable recommendations.
    """
    resolved_path = db_path if db_path is not None else str(get_user_dir() / "kaizen.db")
    improvement_log = ImprovementLog(resolved_path)
    analyzer = DefectTrendAnalyzer()

    raw_weekly = improvement_log.get_weekly_defect_counts(weeks=4)
    weekly_counts = _build_weekly_counts(raw_weekly)

    raw_hotspots = improvement_log.get_defect_hotspots(days=28)
    hotspots = _build_hotspots(raw_hotspots)

    report = analyzer.analyze_trends(weekly_counts, hotspots=hotspots)

    concerning = [t for t in report.trends.values() if t.is_concerning]
    for trend in concerning:
        logger.warning(
            "Concerning defect trend: category=%s change=+%.1f%% current=%d previous=%d",
            trend.category.value,
            trend.change_pct * 100,
            trend.current_count,
            trend.previous_count,
        )

    logger.info(
        "Scheduled trend analysis complete — %d concerning trend(s), %d recommendation(s)",
        len(concerning),
        len(report.recommendations),
    )
    return report


# ── Master wiring entry point ─────────────────────────────────────────────────


def kaizen_scheduled_tasks() -> tuple[KaizenScheduledTask, ...]:
    """Return every Kaizen task the background scheduler must register."""
    return (
        KaizenScheduledTask("pdca_check", "daily", scheduled_pdca_check),
        KaizenScheduledTask("regression_check", "daily", scheduled_regression_check),
        KaizenScheduledTask("trend_analysis", "weekly", scheduled_trend_analysis),
    )


def register_kaizen_jobs(scheduler: object, db_path: str | None = None) -> tuple[KaizenScheduledTask, ...]:
    """Register Kaizen recurring jobs on an APScheduler-compatible scheduler.

    Args:
        scheduler: Object exposing an ``add_job`` method.
        db_path: Optional Kaizen database path passed to each scheduled task.

    Returns:
        Registered Kaizen task descriptors.
    """
    assert_dependency_success(scheduler is not None, dependency_id="kaizen_scheduler")
    tasks = kaizen_scheduled_tasks()
    add_job = getattr(scheduler, "add_job", None)
    assert_dependency_success(callable(add_job), dependency_id="kaizen_scheduler.add_job")
    for task in tasks:
        add_job(task.callback, task.cadence, id=task.name, args=[db_path])
    return tasks


def wire_kaizen_subsystem(
    scheduler: object | None = None, db_path: str | None = None
) -> tuple[KaizenScheduledTask, ...]:
    """Register the kaizen subsystem as ready for scheduled invocation.

    Called once at startup to confirm that all kaizen scheduled functions
    (PDCA check, regression check, trend analysis) are importable and
    correctly wired. Logs a summary so the startup log confirms kaizen is
    active.

    Args:
        scheduler: Optional APScheduler-compatible scheduler. When provided,
            recurring jobs are registered immediately.
        db_path: Optional Kaizen database path passed to registered jobs.

    Returns:
        Scheduler task registrations that callers must register with their
        recurring task runner.
    """
    tasks = register_kaizen_jobs(scheduler, db_path) if scheduler is not None else kaizen_scheduled_tasks()
    logger.info("Kaizen subsystem ready - %s", ", ".join(f"{task.name}:{task.cadence}" for task in tasks))
    return tasks
