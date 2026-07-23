"""Kaizen defect occurrence log — records and queries quality defects.

Maintains the ``defect_occurrences`` table that feeds the DefectTrendAnalyzer.
Every quality rejection from the Inspector agent flows through here so that
curriculum training and PDCA planning can prioritize the most common failure
categories.

This module owns the write side of the defect feedback loop; the read side
lives in ``kaizen.defect_trends`` (trend aggregation) and
``training.curriculum`` (training prioritization).
"""

from __future__ import annotations

import contextlib
import logging
import sqlite3
import threading
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from vetinari.database import get_connection

logger = logging.getLogger(__name__)


_DEFECT_SCHEMA_SQL = """\
CREATE TABLE IF NOT EXISTS defect_occurrences (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at TEXT NOT NULL,
    category TEXT NOT NULL,
    agent_type TEXT NOT NULL DEFAULT '',
    mode TEXT NOT NULL DEFAULT '',
    task_id TEXT NOT NULL DEFAULT '',
    confidence REAL NOT NULL DEFAULT 0.0
);
"""


class DefectLog:
    """Thread-safe SQLite-backed store for defect occurrence records.

    Writes defect events to ``defect_occurrences`` and provides query methods
    for trend analysis and training curriculum prioritization.

    In production (``db_path=None``) uses the unified database via
    ``vetinari.database.get_connection()``. When ``db_path`` is provided
    (test isolation), opens a dedicated per-instance connection to that file.

    Args:
        db_path: Path to a dedicated SQLite database file for test isolation.
            Pass ``None`` (default) to use the unified production database.
    """

    def __init__(self, db_path: Path | str | None = None) -> None:
        self._db_path: Path | None = Path(db_path) if db_path is not None else None
        if self._db_path is not None:
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_db()

    def _init_db(self) -> None:
        """Create the defect_occurrences table if it does not exist."""
        with self._connect() as conn:
            conn.executescript(_DEFECT_SCHEMA_SQL)

    @contextlib.contextmanager
    def _connect(self):
        """Yield a database connection, committing on success.

        Test mode opens a dedicated file connection; production uses the
        unified thread-local connection from ``get_connection()``.

        Yields:
            A sqlite3 Connection with Row factory.
        """
        if self._db_path is not None:
            conn = sqlite3.connect(str(self._db_path))
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()
        else:
            conn = get_connection()
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    # -- Write side --------------------------------------------------------

    def record_defect(
        self,
        category: str,
        agent_type: str = "",
        mode: str = "",
        task_id: str = "",
        confidence: float = 0.0,
    ) -> None:
        """Record a defect occurrence for trend analysis.

        Called when root cause analysis classifies a quality rejection.
        These records feed the DefectTrendAnalyzer and training curriculum.

        Args:
            category: The DefectCategory value (e.g. 'hallucination', 'bad_spec').
            agent_type: The agent type that produced the defective output.
            mode: The agent mode (e.g. 'code_review', 'build').
            task_id: The ID of the failed task.
            confidence: RCA confidence score in [0.0, 1.0].
        """
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO defect_occurrences "
                "(occurred_at, category, agent_type, mode, task_id, confidence) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (now, category, agent_type, mode, task_id, confidence),
            )
        logger.info(
            "Defect recorded: category=%s, agent=%s, mode=%s, task=%s",
            category,
            agent_type,
            mode,
            task_id,
        )

    # -- Read side ---------------------------------------------------------

    def get_weekly_defect_counts(self, weeks: int = 4) -> list[dict[str, int]]:
        """Return per-category defect counts for the last N weeks.

        Groups defect occurrences by ISO week and returns a list ordered
        oldest-first, one dict per week mapping category to count.

        Args:
            weeks: Number of weeks to retrieve (default 4).

        Returns:
            List of dicts mapping category string to count, oldest-first.
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT occurred_at, category FROM defect_occurrences ORDER BY occurred_at ASC",
            ).fetchall()

        if not rows:
            return []

        # Group by ISO week — each (year, week) key holds per-category counts.
        week_buckets: dict[tuple[int, int], dict[str, int]] = defaultdict(
            lambda: defaultdict(int),
        )
        for row in rows:
            dt = datetime.fromisoformat(row["occurred_at"])
            iso_year, iso_week, _ = dt.isocalendar()
            week_buckets[iso_year, iso_week][row["category"]] += 1

        sorted_weeks = sorted(week_buckets.keys())[-weeks:]
        return [dict(week_buckets[w]) for w in sorted_weeks]

    def get_top_defect_pattern(self) -> dict[str, Any] | None:
        """Return the most frequently occurring defect category.

        Queries the ``defect_occurrences`` table for the category with the
        highest count, used by the curriculum to prioritize defect-targeted
        training.

        Returns:
            Dict with ``pattern`` (category name) and ``count``, or None if
            no defect occurrences have been recorded.
        """
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT category, COUNT(*) AS cnt FROM defect_occurrences GROUP BY category ORDER BY cnt DESC LIMIT 1",
            ).fetchone()
        if row is None:
            return None
        return {"pattern": row["category"], "count": row["cnt"]}

    def get_defect_hotspots(self, days: int = 28) -> list[dict[str, Any]]:
        """Return agent+mode combinations with highest defect rates.

        Looks back ``days`` days and returns up to 10 hotspots ranked by
        occurrence count.

        Args:
            days: Lookback window in days (default 28).

        Returns:
            List of hotspot dicts with agent_type, mode, category, count.
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT agent_type, mode, category, COUNT(*) as cnt "
                "FROM defect_occurrences "
                "WHERE occurred_at >= ? AND agent_type != '' "
                "GROUP BY agent_type, mode, category "
                "ORDER BY cnt DESC "
                "LIMIT 10",
                (cutoff,),
            ).fetchall()
        return [
            {
                "agent_type": r["agent_type"],
                "mode": r["mode"],
                "category": r["category"],
                "count": r["cnt"],
            }
            for r in rows
        ]
