"""Defect Tracker — records and queries defect occurrences.

Standalone functions that operate on a sqlite3 connection.  Called by
ImprovementLog to keep the defect-tracking concern separate from the
improvement lifecycle concern.

The ``defect_occurrences`` table is defined in
``vetinari.kaizen.improvement_types._SCHEMA_SQL``.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from vetinari.security.redaction import redact_text

logger = logging.getLogger(__name__)


# Number of days per ISO week — used for empty-week filling arithmetic.
_DAYS_PER_WEEK = 7


def record_defect(
    conn: object,
    category: str,
    agent_type: str = "",
    mode: str = "",
    task_id: str = "",
    confidence: float = 0.0,
) -> None:
    """Record a defect occurrence for trend analysis.

    Called when root cause analysis classifies a quality rejection.
    These records feed the DefectTrendAnalyzer.

    Args:
        conn: An open sqlite3.Connection with Row factory set.
        category: The DefectCategory value (e.g. 'hallucination', 'bad_spec').
        agent_type: The agent type that produced the defective output.
        mode: The agent mode (e.g. 'code_review', 'build').
        task_id: The ID of the failed task.
        confidence: RCA confidence score in [0.0, 1.0].
    """
    now = datetime.now(timezone.utc).isoformat()
    task_ref = _task_ref(task_id)
    conn.execute(
        "INSERT INTO defect_occurrences "
        "(occurred_at, category, agent_type, mode, task_id, confidence) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (now, category, agent_type, mode, task_ref, confidence),
    )
    logger.info(
        "Defect recorded: category=%s, agent=%s, mode=%s, task=%s",
        category,
        agent_type,
        mode,
        task_ref,
    )


def _task_ref(task_id: str) -> str:
    """Store only a redacted task identifier reference."""
    if not task_id:
        return ""
    return redact_text(task_id)


def get_weekly_defect_counts(conn: object, weeks: int = 4) -> list[dict[str, int]]:
    """Return per-category defect counts for the last N weeks.

    Args:
        conn: An open sqlite3.Connection with Row factory set.
        weeks: Number of weeks to retrieve (default 4).

    Returns:
        List of dicts mapping category string to count, ordered oldest-first.
        Each entry represents one ISO week.
    """
    from collections import defaultdict

    rows = conn.execute(
        "SELECT occurred_at, category FROM defect_occurrences ORDER BY occurred_at ASC",
    ).fetchall()

    if not rows:
        # Return ``weeks`` empty dicts so callers always get a uniform-length list.
        return [{} for _ in range(weeks)]

    week_buckets: dict[tuple[int, int], dict[str, int]] = defaultdict(
        lambda: defaultdict(int),
    )
    for row in rows:
        dt = datetime.fromisoformat(row["occurred_at"])
        iso_year, iso_week, _ = dt.isocalendar()
        week_buckets[iso_year, iso_week][row["category"]] += 1

    # Select the N most recent populated weeks, then fill any calendar gaps
    # between the earliest and latest so that empty weeks appear as {} rather
    # than being silently skipped.  Downstream trend analysis needs a
    # contiguous sequence to compute week-over-week change correctly.
    populated = sorted(week_buckets.keys())[-weeks:]
    if not populated:
        return []

    first_year, first_week = populated[0]
    last_year, last_week = populated[-1]

    # Walk every ISO week from first to last, inserting zero-count dicts
    all_weeks: list[dict[str, int]] = []
    cur_date = datetime(first_year, 1, 4, tzinfo=timezone.utc)  # ISO week 1 anchor
    # Advance to the Monday of first_week in first_year
    cur_date += timedelta(weeks=first_week - cur_date.isocalendar()[1])
    # Correct year if the anchor landed in a different year
    if cur_date.isocalendar()[0] < first_year:
        cur_date += timedelta(weeks=52)

    while True:
        iso_y, iso_w, _ = cur_date.isocalendar()
        key = (iso_y, iso_w)
        all_weeks.append(dict(week_buckets.get(key, {})))
        if key == (last_year, last_week):
            break
        cur_date += timedelta(weeks=1)
        if len(all_weeks) > 52 * 2:  # safety valve — never loop more than 2 years
            break

    return all_weeks


def get_top_defect_pattern(conn: object) -> dict[str, Any] | None:
    """Return the most frequently occurring defect category.

    Used by the curriculum to prioritize defect-targeted training.

    Args:
        conn: An open sqlite3.Connection with Row factory set.

    Returns:
        Dict with ``pattern`` (category name) and ``count``, or None if
        no defect occurrences have been recorded.
    """
    row = conn.execute(
        "SELECT category, COUNT(*) AS cnt FROM defect_occurrences GROUP BY category ORDER BY cnt DESC LIMIT 1",
    ).fetchone()
    if row is None:
        return None
    return {"pattern": row["category"], "count": row["cnt"]}


def get_defect_hotspots(conn: object, days: int = 28) -> list[dict[str, Any]]:
    """Return agent+mode combinations with the highest defect rates.

    Defect rate is computed as (defects for this agent+mode+category) /
    (total defects for this agent+mode across all categories) within the
    lookback window.  This normalises raw counts so a busy agent that
    produces many tasks does not dominate the list over a smaller agent
    with a proportionally worse defect rate.

    Args:
        conn: An open sqlite3.Connection with Row factory set.
        days: Lookback window in days (default 28).

    Returns:
        List of hotspot dicts with agent_type, mode, category, count, and
        defect_rate, sorted by count descending, capped at 10 entries.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    rows = conn.execute(
        "SELECT agent_type, mode, category, COUNT(*) as cnt "
        "FROM defect_occurrences "
        "WHERE occurred_at >= ? AND agent_type != '' "
        "GROUP BY agent_type, mode, category "
        "ORDER BY cnt DESC "
        "LIMIT 10",
        (cutoff,),
    ).fetchall()

    # Compute per-agent-mode totals for rate normalisation.
    totals_rows = conn.execute(
        "SELECT agent_type, mode, COUNT(*) as total "
        "FROM defect_occurrences "
        "WHERE occurred_at >= ? AND agent_type != '' "
        "GROUP BY agent_type, mode",
        (cutoff,),
    ).fetchall()
    totals: dict[tuple[str, str], int] = {(r["agent_type"], r["mode"]): r["total"] for r in totals_rows}

    return [
        {
            "agent_type": r["agent_type"],
            "mode": r["mode"],
            "category": r["category"],
            "count": r["cnt"],
            "defect_rate": r["cnt"] / totals.get((r["agent_type"], r["mode"]), r["cnt"]),
        }
        for r in rows
    ]
