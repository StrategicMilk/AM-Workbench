"""Decision Journal — SQLite-backed log of every pipeline decision with enriched confidence data.

Records decisions with numeric confidence scores, contributing factors, and
outcome tracking. Unlike the approval_queue's decision_log (which only tracks
autonomy decisions), this journal captures ALL decision types across the pipeline.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from vetinari.constants import get_user_dir
from vetinari.security.fail_closed import sanitize_untrusted_text
from vetinari.security.redaction import redact_text, redact_value
from vetinari.types import ConfidenceLevel, DecisionType

logger = logging.getLogger(__name__)


def _default_db_path() -> Path:
    """Resolve the default journal database path from the current user dir."""
    return get_user_dir() / "decisions.db"


@dataclass(frozen=True, slots=True)
class DecisionRecord:
    """A single decision recorded in the journal with enriched confidence data.

    Stores the numeric confidence score AND the factors that produced it,
    not just the enum level. This is what makes decisions auditable and
    analyzable after the fact.

    Args:
        decision_id: Unique identifier for this decision.
        decision_type: What kind of decision (routing, approval, autonomy, etc.).
        description: Human-readable description of the decision.
        confidence_score: Numeric confidence score (e.g. mean logprob or similarity).
        confidence_level: Classified confidence level.
        confidence_factors: The individual factors that produced the score.
        action_taken: What action was taken as a result.
        context: Additional context about the decision.
        outcome: Result of the decision (filled in later via update_outcome).
        timestamp: When the decision was made (ISO 8601 UTC).
    """

    decision_id: str
    decision_type: DecisionType
    description: str
    confidence_score: float
    confidence_level: ConfidenceLevel
    confidence_factors: dict[str, float] = field(default_factory=dict)
    action_taken: str = ""
    context: dict[str, Any] = field(default_factory=dict)
    outcome: str = ""
    timestamp: str = ""

    def __repr__(self) -> str:
        return (
            f"DecisionRecord(id={self.decision_id!r}, type={self.decision_type.value!r}, "
            f"score={self.confidence_score:.3f}, level={self.confidence_level.value!r})"
        )


class DecisionJournal:
    """SQLite-backed decision journal with enriched confidence data.

    All pipeline decisions flow through here for audit, analysis, and
    outcome tracking. Thread-safe via lock.

    Side effects in __init__:
      - Creates SQLite database file at db_path if it doesn't exist
      - Creates the decisions table with indexes
    """

    def __init__(self, db_path: Path | None = None) -> None:
        self._db_path = db_path or _default_db_path()
        self._lock = threading.Lock()
        self._ensure_tables()

    def _get_connection(self) -> sqlite3.Connection:
        """Create a new SQLite connection with WAL mode and busy timeout.

        Returns:
            Configured SQLite connection with Row factory.
        """
        conn = sqlite3.connect(str(self._db_path), timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_tables(self) -> None:
        """Create tables if they don't exist."""
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            conn = self._get_connection()
            try:
                conn.executescript("""
                    CREATE TABLE IF NOT EXISTS decisions (
                        decision_id TEXT PRIMARY KEY,
                        decision_type TEXT NOT NULL,
                        description TEXT NOT NULL,
                        confidence_score REAL NOT NULL,
                        confidence_level TEXT NOT NULL,
                        confidence_factors_json TEXT NOT NULL DEFAULT '{}',
                        action_taken TEXT NOT NULL DEFAULT '',
                        context_json TEXT NOT NULL DEFAULT '{}',
                        outcome TEXT NOT NULL DEFAULT '',
                        timestamp TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_dec_type ON decisions(decision_type);
                    CREATE INDEX IF NOT EXISTS idx_dec_timestamp ON decisions(timestamp);
                    CREATE INDEX IF NOT EXISTS idx_dec_level ON decisions(confidence_level);
                """)
                conn.commit()
            finally:
                conn.close()

    def log_decision(
        self,
        decision_type: DecisionType,
        description: str,
        confidence_score: float,
        confidence_level: ConfidenceLevel,
        confidence_factors: dict[str, float] | None = None,
        action_taken: str = "",
        context: dict[str, Any] | None = None,
    ) -> str:
        """Record a decision in the journal.

        Args:
            decision_type: Classification of the decision.
            description: Human-readable description.
            confidence_score: Numeric confidence score.
            confidence_level: Classified confidence level.
            confidence_factors: Individual factors that produced the score.
            action_taken: What action was taken as a result.
            context: Additional context metadata.

        Returns:
            The unique decision_id for this entry.
        """
        description = sanitize_untrusted_text(description, max_length=4_000)
        action_taken = sanitize_untrusted_text(action_taken or "none", max_length=1_000)
        confidence_score = float(confidence_score)
        decision_id = f"dec_{uuid.uuid4().hex[:12]}"
        now = datetime.now(timezone.utc).isoformat()

        with self._lock:
            conn = self._get_connection()
            try:
                conn.execute(
                    """INSERT INTO decisions
                       (decision_id, decision_type, description, confidence_score,
                        confidence_level, confidence_factors_json, action_taken,
                        context_json, outcome, timestamp)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        decision_id,
                        decision_type.value,
                        redact_text(description),
                        confidence_score,
                        confidence_level.value,
                        json.dumps(redact_value(confidence_factors or {})),
                        redact_text(action_taken),
                        json.dumps(redact_value(context or {})),
                        "",
                        now,
                    ),
                )
                conn.commit()
            finally:
                conn.close()

        logger.info(
            "Decision logged: id=%s type=%s score=%.3f level=%s action=%s",
            decision_id,
            decision_type.value,
            confidence_score,
            confidence_level.value,
            action_taken,
        )
        return decision_id

    def update_outcome(self, decision_id: str, outcome: str) -> bool:
        """Record the outcome of a previously logged decision.

        Args:
            decision_id: The decision to update.
            outcome: The result of executing the decision.

        Returns:
            True if updated, False if decision_id not found.
        """
        decision_id = sanitize_untrusted_text(decision_id, max_length=64)
        outcome = sanitize_untrusted_text(outcome, max_length=4_000)
        with self._lock:
            conn = self._get_connection()
            try:
                cursor = conn.execute(
                    "UPDATE decisions SET outcome = ? WHERE decision_id = ?",
                    (redact_text(outcome), decision_id),
                )
                conn.commit()
                updated = cursor.rowcount > 0
            finally:
                conn.close()

        if not updated:
            logger.warning("Cannot update outcome — decision %s not found", decision_id)
        return updated

    def get_decisions(
        self,
        decision_type: DecisionType | None = None,
        confidence_level: ConfidenceLevel | None = None,
        limit: int = 100,
    ) -> list[DecisionRecord]:
        """Query the decision journal with optional filters.

        Args:
            decision_type: Optional filter by decision type.
            confidence_level: Optional filter by confidence level.
            limit: Maximum entries to return.

        Returns:
            List of DecisionRecord, most recent first.
        """
        # Build query from fixed set of known filters (no user-supplied column names)
        _BASE = "SELECT * FROM decisions"
        _ORDER = "ORDER BY timestamp DESC LIMIT ?"

        has_type = decision_type is not None
        has_level = confidence_level is not None

        if has_type and has_level:
            query = f"{_BASE} WHERE decision_type = ? AND confidence_level = ? {_ORDER}"
            params: list[Any] = [decision_type.value, confidence_level.value, limit]
        elif has_type:
            query = f"{_BASE} WHERE decision_type = ? {_ORDER}"
            params = [decision_type.value, limit]
        elif has_level:
            query = f"{_BASE} WHERE confidence_level = ? {_ORDER}"
            params = [confidence_level.value, limit]
        else:
            query = f"{_BASE} {_ORDER}"
            params = [limit]

        with self._lock:
            conn = self._get_connection()
            try:
                cursor = conn.execute(query, params)
                rows = cursor.fetchall()
            finally:
                conn.close()

        return [
            DecisionRecord(
                decision_id=row["decision_id"],
                decision_type=DecisionType(row["decision_type"]),
                description=row["description"],
                confidence_score=row["confidence_score"],
                confidence_level=ConfidenceLevel(row["confidence_level"]),
                confidence_factors=json.loads(row["confidence_factors_json"]),
                action_taken=row["action_taken"],
                context=json.loads(row["context_json"]),
                outcome=row["outcome"],
                timestamp=row["timestamp"],
            )
            for row in rows
        ]


# -- Singleton ----------------------------------------------------------------

_journal: DecisionJournal | None = None
_journal_lock = threading.Lock()


def get_decision_journal(db_path: Path | None = None) -> DecisionJournal:
    """Get or create the singleton DecisionJournal.

    Args:
        db_path: Optional override for database path (used in tests).

    Returns:
        The singleton DecisionJournal instance.
    """
    global _journal
    if _journal is None:
        with _journal_lock:
            if _journal is None:
                _journal = DecisionJournal(db_path=db_path)
    return _journal


def reset_decision_journal() -> None:
    """Reset the singleton DecisionJournal for test isolation."""
    global _journal
    with _journal_lock:
        _journal = None
