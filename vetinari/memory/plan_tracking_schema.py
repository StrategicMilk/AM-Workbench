"""SQLite schema setup for the plan-tracking memory store."""

from __future__ import annotations

import logging
import sqlite3
from typing import TYPE_CHECKING, Any

from vetinari.database import get_connection

logger = logging.getLogger("vetinari.memory.plan_tracking")

_PLAN_HISTORY_SQL = """
    CREATE TABLE IF NOT EXISTS PlanHistory (
        plan_id TEXT PRIMARY KEY,
        plan_version INTEGER DEFAULT 1,
        goal TEXT NOT NULL,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        status TEXT DEFAULT 'draft',
        plan_json TEXT,
        plan_explanation_json TEXT,
        chosen_plan_id TEXT,
        plan_justification TEXT,
        risk_score REAL DEFAULT 0.0,
        dry_run BOOLEAN DEFAULT 0,
        auto_approved BOOLEAN DEFAULT 0
    )
"""
_SUBTASK_MEMORY_SQL = """
    CREATE TABLE IF NOT EXISTS SubtaskMemory (
        subtask_id TEXT PRIMARY KEY,
        plan_id TEXT NOT NULL,
        parent_subtask_id TEXT,
        description TEXT NOT NULL,
        depth INTEGER DEFAULT 0,
        status TEXT DEFAULT 'pending',
        assigned_model_id TEXT,
        outcome TEXT,
        duration_seconds REAL,
        cost_estimate REAL,
        rationale TEXT,
        subtask_explanation_json TEXT,
        domain TEXT,
        quality_score REAL DEFAULT 0.0,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (plan_id) REFERENCES PlanHistory(plan_id)
    )
"""
_MODEL_PERFORMANCE_SQL = """
    CREATE TABLE IF NOT EXISTS ModelPerformance (
        model_id TEXT NOT NULL,
        task_type TEXT NOT NULL,
        success_rate REAL DEFAULT 0.0,
        avg_latency REAL DEFAULT 0.0,
        total_uses INTEGER DEFAULT 0,
        last_used_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (model_id, task_type)
    )
"""
_PLAN_PRUNE_RECEIPTS_SQL = """
    CREATE TABLE IF NOT EXISTS PlanPruneReceipts (
        receipt_id TEXT PRIMARY KEY,
        created_at DATETIME NOT NULL,
        cutoff DATETIME NOT NULL,
        retention_days INTEGER NOT NULL,
        owner_ref TEXT NOT NULL,
        dry_run BOOLEAN NOT NULL,
        pruned_count INTEGER NOT NULL,
        payload TEXT NOT NULL
    )
"""


class PlanTrackingSchemaMixin:
    """Initialize and migrate the plan-tracking SQLite schema."""

    if TYPE_CHECKING:
        _db_path: Any
        _init_json_store: Any

    def _init_sqlite(self) -> None:
        try:
            conn = get_connection(self._db_path)
            self._create_plan_tracking_tables(conn)
            self._migrate_plan_tracking_tables(conn)
            conn.commit()
            logger.info("Memory store initialized (unified database)")
        except sqlite3.Error as e:
            logger.warning("SQLite initialization failed: %s. Falling back to JSON.", e)
            self._init_json_store()

    @staticmethod
    def _create_plan_tracking_tables(conn: sqlite3.Connection) -> None:
        conn.execute(_PLAN_HISTORY_SQL)
        conn.execute(_SUBTASK_MEMORY_SQL)
        conn.execute(_MODEL_PERFORMANCE_SQL)
        conn.execute(_PLAN_PRUNE_RECEIPTS_SQL)

    @staticmethod
    def _migrate_plan_tracking_tables(conn: sqlite3.Connection) -> None:
        plan_columns = {row[1] for row in conn.execute("PRAGMA table_info(PlanHistory)").fetchall()}
        if "plan_explanation_json" not in plan_columns:
            conn.execute("ALTER TABLE PlanHistory ADD COLUMN plan_explanation_json TEXT")

        subtask_columns = {row[1] for row in conn.execute("PRAGMA table_info(SubtaskMemory)").fetchall()}
        if "subtask_explanation_json" not in subtask_columns:
            conn.execute("ALTER TABLE SubtaskMemory ADD COLUMN subtask_explanation_json TEXT")
        if "quality_score" not in subtask_columns:
            conn.execute("ALTER TABLE SubtaskMemory ADD COLUMN quality_score REAL DEFAULT 0.0")
