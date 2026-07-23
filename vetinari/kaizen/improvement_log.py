"""Kaizen Improvement Log — tracks improvements from hypothesis to outcome.

Every improvement is recorded, measured, and tracked as a first-class entity.
This is the kaizen board: the central registry for all system improvements
following the PDCA (Plan-Do-Check-Act) cycle.

Persistence uses SQLite with two tables:
- ``improvements``: core improvement records
- ``improvement_observations``: time-series observations during the observation window
"""

from __future__ import annotations

import contextlib
import logging
import sqlite3
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path

from vetinari.database import get_connection
from vetinari.exceptions import ExecutionError
from vetinari.kaizen.improvement_log_queries_mixin import ImprovementLogQueryMixin

logger = logging.getLogger(__name__)


OBSERVATION_WINDOW_HOURS_DEFAULT = 168  # 7 days
REGRESSION_THRESHOLD = 0.95  # 5% worse than baseline → FAILED


class ImprovementStatus(Enum):
    """Lifecycle status of a kaizen improvement."""

    PROPOSED = "proposed"
    ACTIVE = "active"
    CONFIRMED = "confirmed"
    REVERTED = "reverted"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class ImprovementRecord:
    """A single kaizen improvement — tracked from hypothesis to outcome.

    Attributes:
        id: Unique improvement identifier.
        hypothesis: What the improvement is expected to achieve.
        metric: Which metric is being improved (quality, latency, cost, throughput).
        baseline_value: Metric value before improvement.
        target_value: Expected metric value after improvement.
        actual_value: Measured metric value after observation (filled post-observation).
        applied_by: Which subsystem applied this improvement.
        applied_at: When the improvement was activated.
        observation_window: How long to observe before judging.
        status: Current lifecycle status.
        regression_detected: Whether a regression was detected post-confirmation.
        rollback_plan: How to undo this improvement if it fails.
        confirmed_at: When the improvement was confirmed.
        reverted_at: When the improvement was reverted.
        notes: Free-form notes.
    """

    id: str
    hypothesis: str
    metric: str
    baseline_value: float
    target_value: float
    actual_value: float | None = None
    applied_by: str = ""
    applied_at: datetime | None = None
    observation_window: timedelta = field(
        default_factory=lambda: timedelta(hours=OBSERVATION_WINDOW_HOURS_DEFAULT),
    )
    status: ImprovementStatus = ImprovementStatus.PROPOSED
    regression_detected: bool = False
    rollback_plan: str = ""
    confirmed_at: datetime | None = None
    reverted_at: datetime | None = None
    notes: str = ""

    def __repr__(self) -> str:
        return (
            f"ImprovementRecord(id={self.id!r}, metric={self.metric!r}, "
            f"status={self.status.value!r}, baseline_value={self.baseline_value!r}, "
            f"actual_value={self.actual_value!r})"
        )


@dataclass(frozen=True, slots=True)
class Observation:
    """A single observation during an improvement's observation window.

    Attributes:
        observation_id: Auto-incremented ID.
        improvement_id: FK to the improvement being observed.
        observed_at: When this observation was recorded.
        metric_value: The measured metric value.
        sample_size: Number of samples in this observation.
    """

    observation_id: int
    improvement_id: str
    observed_at: datetime
    metric_value: float
    sample_size: int

    def __repr__(self) -> str:
        return (
            f"Observation(observation_id={self.observation_id!r}, "
            f"improvement_id={self.improvement_id!r}, metric_value={self.metric_value!r})"
        )


@dataclass(frozen=True, slots=True)
class KaizenReport:
    """Summary report of the kaizen system's current state.

    Attributes:
        total_proposed: Number of improvements in PROPOSED status.
        total_active: Number of improvements in ACTIVE status.
        total_confirmed: Number of improvements in CONFIRMED status.
        total_failed: Number of improvements in FAILED status.
        total_reverted: Number of improvements in REVERTED status.
        avg_improvement_effect: Average (actual - baseline) for CONFIRMED improvements.
        generated_at: When this report was generated.
    """

    total_proposed: int = 0
    total_active: int = 0
    total_confirmed: int = 0
    total_failed: int = 0
    total_reverted: int = 0
    avg_improvement_effect: float = 0.0
    generated_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc),
    )

    def __repr__(self) -> str:
        return (
            f"KaizenReport(total_proposed={self.total_proposed!r}, "
            f"total_active={self.total_active!r}, total_confirmed={self.total_confirmed!r}, "
            f"avg_improvement_effect={self.avg_improvement_effect!r})"
        )


_SCHEMA_SQL = """\
CREATE TABLE IF NOT EXISTS improvements (
    id TEXT PRIMARY KEY,
    hypothesis TEXT NOT NULL,
    metric TEXT NOT NULL,
    baseline_value REAL NOT NULL,
    target_value REAL NOT NULL,
    actual_value REAL,
    applied_by TEXT NOT NULL,
    created_at TEXT,
    applied_at TEXT,
    observation_window_hours INTEGER NOT NULL DEFAULT 168,
    status TEXT NOT NULL DEFAULT 'proposed',
    regression_detected INTEGER NOT NULL DEFAULT 0,
    rollback_plan TEXT NOT NULL,
    confirmed_at TEXT,
    reverted_at TEXT,
    notes TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS improvement_observations (
    observation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    improvement_id TEXT NOT NULL REFERENCES improvements(id),
    observed_at TEXT NOT NULL,
    metric_value REAL NOT NULL,
    sample_size INTEGER NOT NULL
);
"""


class ImprovementLog(ImprovementLogQueryMixin):
    """The kaizen board — tracks all improvements from hypothesis to outcome.

    Thread-safe SQLite-backed improvement tracking with full PDCA lifecycle
    management. Each improvement goes through:
    PROPOSED → ACTIVE → (CONFIRMED | FAILED | REVERTED)

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
        """Initialize the improvements table schema.

        In test isolation mode (db_path provided), creates a local kaizen schema.
        In production (db_path is None), the unified database schema from
        vetinari.database.py is already initialized and no action is needed.

        Also applies incremental column migrations for legacy local databases.
        """
        if self._db_path is None:
            # Production mode: unified database schema already initialized
            return

        # Test isolation mode: apply the local kaizen schema
        with self._connect() as conn:
            conn.executescript(_SCHEMA_SQL)
            # Migration: add created_at if not present (introduced for
            # ISO-week filtering in get_proposed_this_week).
            existing = {row[1] for row in conn.execute("PRAGMA table_info(improvements)").fetchall()}
            if "created_at" not in existing:
                conn.execute("ALTER TABLE improvements ADD COLUMN created_at TEXT")

    @contextlib.contextmanager
    def _connect(self):
        """Yield a connection with row factory enabled, committing on success.

        When ``db_path`` was provided (test mode), opens a dedicated connection
        per call and closes it on exit. In production uses the thread-local
        unified connection from ``get_connection()`` and only commits/rolls back
        without closing (lifecycle managed by ``vetinari.database``).

        Yields:
            A sqlite3 Connection with Row factory.
        """
        if self._db_path is not None:
            # Test-isolation path: open a dedicated connection to the given file.
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
            # Production path: use the unified thread-local connection.
            conn = get_connection()
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    # ── Public API ─────────────────────────────────────────────────────

    def propose(
        self,
        hypothesis: str,
        metric: str,
        baseline: float,
        target: float,
        applied_by: str,
        rollback_plan: str,
        observation_window_hours: int = OBSERVATION_WINDOW_HOURS_DEFAULT,
        notes: str = "",
        improvement_id: str | None = None,
    ) -> str:
        """Create a new proposed improvement.

        Args:
            hypothesis: What the improvement is expected to achieve.
            metric: Which metric is being improved.
            baseline: Current metric value before improvement.
            target: Expected metric value after improvement.
            applied_by: Which subsystem is proposing this improvement.
            rollback_plan: How to undo this improvement if it fails.
            observation_window_hours: Hours to observe before judging.
            notes: Optional traceability notes to store with the proposal.
            improvement_id: Optional deterministic identifier supplied by
                proposal sources that need idempotent durable records.

        Returns:
            The unique improvement ID.
        """
        improvement_id = improvement_id or f"IMP-{uuid.uuid4().hex[:8]}"
        created_at = datetime.now(timezone.utc).isoformat()
        with self._lock, self._connect() as conn:
            conn.execute(
                """INSERT INTO improvements
                   (id, hypothesis, metric, baseline_value, target_value,
                    applied_by, created_at, observation_window_hours, status, rollback_plan, notes)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'proposed', ?, ?)""",
                (
                    improvement_id,
                    hypothesis,
                    metric,
                    baseline,
                    target,
                    applied_by,
                    created_at,
                    observation_window_hours,
                    rollback_plan,
                    notes,
                ),
            )
        logger.info(
            "Improvement proposed: id=%s, hypothesis=%s, metric=%s, baseline=%.3f, target=%.3f",
            improvement_id,
            hypothesis,
            metric,
            baseline,
            target,
        )
        self._emit_proposed(improvement_id, hypothesis, metric, applied_by)
        return improvement_id

    def activate(self, improvement_id: str) -> None:
        """Mark an improvement as active — observation window begins.

        Args:
            improvement_id: The improvement to activate.

        Raises:
            ValueError: If the improvement is not in PROPOSED status.
        """
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT status, metric, applied_by FROM improvements WHERE id = ?",
                (improvement_id,),
            ).fetchone()
            if row is None:
                raise ExecutionError(f"Improvement not found: {improvement_id}")
            if row["status"] != ImprovementStatus.PROPOSED.value:
                raise ExecutionError(
                    f"Cannot activate improvement {improvement_id} — status is {row['status']}, expected 'proposed'",
                )
            conn.execute(
                "UPDATE improvements SET status = 'active', applied_at = ? WHERE id = ?",
                (now, improvement_id),
            )
        logger.info("Improvement activated: %s", improvement_id)
        self._emit_active(improvement_id, row["metric"], row["applied_by"])

    def revert_to_proposed(self, improvement_id: str) -> None:
        """Roll an ACTIVE improvement back to PROPOSED status.

        Called by PDCAController when an applicator raises an exception after
        the improvement has already been activated.  Restores the improvement
        to PROPOSED so it can be retried without leaving the system in an
        inconsistent ACTIVE state with no applied change.

        Args:
            improvement_id: The improvement to roll back.

        Raises:
            ExecutionError: If the improvement is not found or is not ACTIVE.
        """
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT status FROM improvements WHERE id = ?",
                (improvement_id,),
            ).fetchone()
            if row is None:
                raise ExecutionError(f"Improvement not found: {improvement_id}")
            if row["status"] != ImprovementStatus.ACTIVE.value:
                raise ExecutionError(
                    f"Cannot revert improvement {improvement_id} to proposed — "
                    f"status is {row['status']}, expected 'active'",
                )
            conn.execute(
                "UPDATE improvements SET status = 'proposed', applied_at = NULL WHERE id = ?",
                (improvement_id,),
            )
        logger.warning(
            "Improvement %s rolled back to PROPOSED after applicator failure",
            improvement_id,
        )

    def observe(
        self,
        improvement_id: str,
        metric_value: float,
        sample_size: int,
    ) -> None:
        """Record an observation during the observation window.

        Args:
            improvement_id: The improvement being observed.
            metric_value: The measured metric value.
            sample_size: Number of samples in this observation.

        Raises:
            ValueError: If the improvement does not exist.
        """
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT id FROM improvements WHERE id = ?",
                (improvement_id,),
            ).fetchone()
            if row is None:
                raise ExecutionError(f"Improvement not found: {improvement_id}")
            conn.execute(
                """INSERT INTO improvement_observations
                   (improvement_id, observed_at, metric_value, sample_size)
                   VALUES (?, ?, ?, ?)""",
                (improvement_id, now, metric_value, sample_size),
            )
        logger.debug(
            "Observation recorded: improvement=%s, value=%.3f, n=%d",
            improvement_id,
            metric_value,
            sample_size,
        )

    def evaluate(self, improvement_id: str) -> ImprovementStatus:
        """Evaluate an improvement after its observation window. See ``improvement_log_evaluation``.

        Returns:
            The new status of the improvement (CONFIRMED or REVERTED).
        """
        from vetinari.kaizen.improvement_log_evaluation import evaluate as _evaluate

        return _evaluate(self, improvement_id)

    def revert(self, improvement_id: str) -> None:
        """Mark an improvement as reverted. See ``improvement_log_evaluation``."""
        from vetinari.kaizen.improvement_log_evaluation import revert as _revert

        _revert(self, improvement_id)

    # ── Query methods — delegates to improvement_log_queries ──────────

    # ── Defect tracking — delegates to DefectLog ────────────────────

    # ── Private helpers ────────────────────────────────────────────────

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> ImprovementRecord:
        """Convert a sqlite3.Row to an ImprovementRecord.

        Args:
            row: A database row from the improvements table.

        Returns:
            The corresponding ImprovementRecord.
        """
        applied_at = datetime.fromisoformat(row["applied_at"]) if row["applied_at"] else None
        confirmed_at = datetime.fromisoformat(row["confirmed_at"]) if row["confirmed_at"] else None
        reverted_at = datetime.fromisoformat(row["reverted_at"]) if row["reverted_at"] else None
        return ImprovementRecord(
            id=row["id"],
            hypothesis=row["hypothesis"],
            metric=row["metric"],
            baseline_value=row["baseline_value"],
            target_value=row["target_value"],
            actual_value=row["actual_value"],
            applied_by=row["applied_by"],
            applied_at=applied_at,
            observation_window=timedelta(hours=row["observation_window_hours"]),
            status=ImprovementStatus(row["status"]),
            regression_detected=bool(row["regression_detected"]),
            rollback_plan=row["rollback_plan"],
            confirmed_at=confirmed_at,
            reverted_at=reverted_at,
            notes=row["notes"] or "",
        )

    # ── Event emission — delegates to improvement_events ──────────────

    @staticmethod
    def _emit_proposed(improvement_id: str, hypothesis: str, metric: str, applied_by: str) -> None:
        """Emit a KaizenImprovementProposed event — delegates to improvement_events."""
        from vetinari.kaizen.improvement_events import emit_proposed

        emit_proposed(improvement_id, hypothesis, metric, applied_by)

    @staticmethod
    def _emit_confirmed(
        improvement_id: str, metric: str, baseline_value: float, actual_value: float, applied_by: str
    ) -> None:
        """Emit a KaizenImprovementConfirmed event — delegates to improvement_events."""
        from vetinari.kaizen.improvement_events import emit_confirmed

        emit_confirmed(improvement_id, metric, baseline_value, actual_value, applied_by)

    @staticmethod
    def _emit_active(improvement_id: str, metric: str, applied_by: str) -> None:
        """Emit a KaizenImprovementActive event — delegates to improvement_events."""
        from vetinari.kaizen.improvement_events import emit_active

        emit_active(improvement_id, metric, applied_by)

    @staticmethod
    def _emit_reverted(improvement_id: str, metric: str, reason: str) -> None:
        """Emit a KaizenImprovementReverted event — delegates to improvement_events."""
        from vetinari.kaizen.improvement_events import emit_reverted

        emit_reverted(improvement_id, metric, reason)
