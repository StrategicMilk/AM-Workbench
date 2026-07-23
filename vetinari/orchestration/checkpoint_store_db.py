"""SQLite primitives for durable execution checkpoint storage."""

from __future__ import annotations

import logging
import sqlite3
import threading
from pathlib import Path

logger = logging.getLogger(__name__)


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS execution_state (
    execution_id TEXT PRIMARY KEY,
    goal TEXT NOT NULL,
    tier TEXT NOT NULL DEFAULT 'standard',
    request_spec_json TEXT,
    pipeline_state TEXT NOT NULL DEFAULT 'executing',
    current_agent TEXT,
    task_dag_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT,
    terminal_status TEXT,
    error TEXT
);

CREATE TABLE IF NOT EXISTS task_checkpoints (
    task_id TEXT NOT NULL,
    execution_id TEXT NOT NULL,
    node_id TEXT NOT NULL DEFAULT '',
    superstep_index INTEGER NOT NULL DEFAULT 0,
    substep_index INTEGER NOT NULL DEFAULT 0,
    agent_type TEXT NOT NULL DEFAULT '',
    mode TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    input_json TEXT,
    output_json TEXT,
    failure_json TEXT,
    manifest_hash TEXT,
    started_at TEXT,
    completed_at TEXT,
    retry_count INTEGER DEFAULT 0,
    PRIMARY KEY (execution_id, task_id),
    FOREIGN KEY (execution_id) REFERENCES execution_state(execution_id)
);

CREATE TABLE IF NOT EXISTS paused_questions (
    question_id TEXT PRIMARY KEY,
    execution_id TEXT NOT NULL,
    task_id TEXT,
    questions_json TEXT NOT NULL,
    answers_json TEXT,
    asked_at TEXT NOT NULL,
    answered_at TEXT,
    FOREIGN KEY (execution_id) REFERENCES execution_state(execution_id)
);

CREATE TABLE IF NOT EXISTS execution_events (
    event_id TEXT PRIMARY KEY,
    execution_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    task_id TEXT,
    timestamp TEXT NOT NULL,
    data_json TEXT,
    FOREIGN KEY (execution_id) REFERENCES execution_state(execution_id)
);

CREATE INDEX IF NOT EXISTS idx_task_checkpoints_execution_id
    ON task_checkpoints(execution_id);
CREATE INDEX IF NOT EXISTS idx_task_checkpoints_execution_superstep
    ON task_checkpoints(execution_id, superstep_index);
CREATE INDEX IF NOT EXISTS idx_task_checkpoints_node_id
    ON task_checkpoints(node_id);
CREATE INDEX IF NOT EXISTS idx_task_checkpoints_status
    ON task_checkpoints(status);
CREATE INDEX IF NOT EXISTS idx_paused_questions_execution_id
    ON paused_questions(execution_id);
CREATE INDEX IF NOT EXISTS idx_paused_questions_task_id
    ON paused_questions(task_id);
CREATE INDEX IF NOT EXISTS idx_execution_events_execution_timestamp
    ON execution_events(execution_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_execution_events_task_id
    ON execution_events(task_id);
"""


class CheckpointDatabaseManager:
    """Thread-safe SQLite wrapper for durable execution checkpoints.

    When an explicit ``db_path`` is provided (e.g. in tests), uses a
    direct per-thread connection to that path.  When ``db_path`` is
    ``None``, delegates to the unified ``vetinari.database`` module so
    production data lands in the consolidated database (ADR-0072).
    """

    def __init__(self, db_path: Path | None = None) -> None:
        self._db_path = db_path
        self._shared_conn: sqlite3.Connection | None = None  # For standalone path mode
        # Write lock serializes INSERT/UPDATE across threads for this engine
        self._write_lock = threading.Lock()
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        """Return a single shared SQLite connection for this engine.

        Uses one connection with ``check_same_thread=False``, protected
        by the engine's ``_write_lock``. This avoids ``database is locked``
        errors from multiple thread-local connections competing for
        SQLite's single-writer lock.

        When ``_db_path is None`` (production), connects to the unified
        database at ``VETINARI_DB_PATH``. When ``_db_path`` is set (tests),
        connects to that specific file.

        Returns:
            A ``sqlite3.Connection`` with WAL mode enabled.
        """
        if self._shared_conn is not None:
            return self._shared_conn

        if self._db_path is None:
            from vetinari.database import _get_db_path

            db_path = _get_db_path()
        else:
            db_path = self._db_path

        db_path.parent.mkdir(parents=True, exist_ok=True)
        # isolation_level=None enables autocommit mode so Python's sqlite3 module
        # does NOT issue implicit BEGIN before DML statements.  Without this,
        # execute_in_transaction's explicit BEGIN raises "cannot start a transaction
        # within a transaction" because Python's deferred transaction is already open.
        self._shared_conn = sqlite3.connect(
            str(db_path),
            check_same_thread=False,
            timeout=30.0,
            isolation_level=None,
        )
        self._shared_conn.execute("PRAGMA journal_mode=WAL")
        self._shared_conn.execute("PRAGMA synchronous=NORMAL")
        self._shared_conn.execute("PRAGMA foreign_keys=ON")
        self._shared_conn.execute("PRAGMA busy_timeout=5000")
        return self._shared_conn

    def _init_db(self) -> None:
        """Initialise the schema in the target database.

        For the unified database path, ensures the shared connection is
        created and the full unified schema (including ``execution_events``)
        is applied. For standalone paths, applies only the durable execution
        schema from ``_SCHEMA_SQL``.
        """
        conn = self._get_conn()
        self._prepare_task_checkpoints_for_schema(conn)
        if self._db_path is None:
            # Production: apply the full unified schema which includes
            # execution_events, quality_scores, etc.
            from vetinari.database import _UNIFIED_SCHEMA

            conn.executescript(_UNIFIED_SCHEMA)
            conn.commit()
        else:
            # Tests: apply only the durable execution schema
            conn.executescript(_SCHEMA_SQL)
            conn.commit()

        # Migrate pre-existing databases that lack terminal_status.
        # ALTER TABLE ADD COLUMN is a no-op if the column already exists in
        # SQLite >=3.37. For older SQLite, we catch OperationalError and ignore
        # it — the column not existing means we're on a fresh db that already
        # has it from the CREATE TABLE above.
        try:
            conn.execute("ALTER TABLE execution_state ADD COLUMN terminal_status TEXT")
            conn.commit()
        except sqlite3.OperationalError:
            # Column already exists — migration already applied, nothing to do.
            logger.info("terminal_status column already present in execution_state; skipping migration")

        checkpoint_columns = {
            "node_id": "TEXT NOT NULL DEFAULT ''",
            "superstep_index": "INTEGER NOT NULL DEFAULT 0",
            "substep_index": "INTEGER NOT NULL DEFAULT 0",
            "failure_json": "TEXT",
        }
        for column_name, column_definition in checkpoint_columns.items():
            try:
                conn.execute(f"ALTER TABLE task_checkpoints ADD COLUMN {column_name} {column_definition}")
                conn.commit()
            except sqlite3.OperationalError:
                logger.info(
                    "%s column already present in task_checkpoints; skipping migration",
                    column_name,
                )
        self._ensure_task_checkpoint_composite_identity(conn)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_task_checkpoints_execution_superstep "
            "ON task_checkpoints(execution_id, superstep_index)"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_task_checkpoints_node_id ON task_checkpoints(node_id)")
        conn.commit()

    def _prepare_task_checkpoints_for_schema(self, conn: sqlite3.Connection) -> None:
        """Repair legacy task_checkpoints before schema scripts create indexes."""
        table_info = conn.execute("PRAGMA table_info(task_checkpoints)").fetchall()
        if not table_info:
            return
        existing_columns = {row[1] for row in table_info}
        checkpoint_columns = {
            "node_id": "TEXT NOT NULL DEFAULT ''",
            "superstep_index": "INTEGER NOT NULL DEFAULT 0",
            "substep_index": "INTEGER NOT NULL DEFAULT 0",
            "failure_json": "TEXT",
        }
        for column_name, column_definition in checkpoint_columns.items():
            if column_name in existing_columns:
                continue
            conn.execute(f"ALTER TABLE task_checkpoints ADD COLUMN {column_name} {column_definition}")
        self._ensure_task_checkpoint_composite_identity(conn)
        conn.commit()

    def _ensure_task_checkpoint_composite_identity(self, conn: sqlite3.Connection) -> None:
        """Migrate old task-id-only checkpoint tables to execution-scoped identity."""
        table_info = conn.execute("PRAGMA table_info(task_checkpoints)").fetchall()
        pk_columns = [row[1] for row in sorted(table_info, key=lambda row: row[5]) if row[5]]
        if pk_columns == ["execution_id", "task_id"]:
            return
        if pk_columns != ["task_id"]:
            logger.warning("Unexpected task_checkpoints primary key %s; leaving schema unchanged", pk_columns)
            return
        logger.warning("Migrating task_checkpoints primary key from task_id to (execution_id, task_id)")
        conn.execute("ALTER TABLE task_checkpoints RENAME TO task_checkpoints_legacy")
        conn.execute(
            """
            CREATE TABLE task_checkpoints (
                task_id TEXT NOT NULL,
                execution_id TEXT NOT NULL,
                node_id TEXT NOT NULL DEFAULT '',
                superstep_index INTEGER NOT NULL DEFAULT 0,
                substep_index INTEGER NOT NULL DEFAULT 0,
                agent_type TEXT NOT NULL DEFAULT '',
                mode TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL,
                input_json TEXT,
                output_json TEXT,
                failure_json TEXT,
                manifest_hash TEXT,
                started_at TEXT,
                completed_at TEXT,
                retry_count INTEGER DEFAULT 0,
                PRIMARY KEY (execution_id, task_id),
                FOREIGN KEY (execution_id) REFERENCES execution_state(execution_id)
            )
            """
        )
        legacy_columns = {row[1] for row in conn.execute("PRAGMA table_info(task_checkpoints_legacy)").fetchall()}
        columns = [
            "task_id",
            "execution_id",
            "node_id",
            "superstep_index",
            "substep_index",
            "agent_type",
            "mode",
            "status",
            "input_json",
            "output_json",
            "failure_json",
            "manifest_hash",
            "started_at",
            "completed_at",
            "retry_count",
        ]
        select_exprs = []
        for column in columns:
            if column in legacy_columns:
                select_exprs.append(column)
            elif column in {"node_id", "agent_type", "mode"}:
                select_exprs.append("''")
            elif column in {"superstep_index", "substep_index", "retry_count"}:
                select_exprs.append("0")
            else:
                select_exprs.append("NULL")
        conn.execute(
            f"INSERT OR REPLACE INTO task_checkpoints ({', '.join(columns)}) "  # noqa: S608 - fixed schema lists.
            f"SELECT {', '.join(select_exprs)} FROM task_checkpoints_legacy"
        )
        conn.execute("DROP TABLE task_checkpoints_legacy")
        conn.commit()

    def execute(self, sql: str, params: tuple = ()) -> list[tuple]:
        """Execute a SQL statement with thread-safe write serialization.

        All operations go through the write lock to prevent concurrent
        thread-local connections from hitting ``database is locked``
        errors. This serializes writes across all threads that share
        this ``CheckpointDatabaseManager`` instance.

        Args:
            sql: SQL statement to execute.
            params: Parameters for the SQL statement.

        Returns:
            List of result rows.
        """
        with self._write_lock:
            conn = self._get_conn()
            cursor = conn.execute(sql, params)
            rows = cursor.fetchall()
            if sql.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE", "CREATE", "DROP")):
                conn.commit()
            return rows

    def executemany(self, sql: str, params_list: list[tuple]) -> None:
        """Execute a SQL statement for many parameter sets.

        Args:
            sql: SQL statement to execute.
            params_list: List of parameter tuples.
        """
        with self._write_lock:
            conn = self._get_conn()
            conn.executemany(sql, params_list)
            conn.commit()

    def execute_in_transaction(
        self,
        statements: list[tuple[str, tuple]],
        many_statements: list[tuple[str, list[tuple]]] | None = None,
    ) -> None:
        """Execute multiple SQL statements atomically in a single transaction.

        All statements are committed together or rolled back together if any
        fails.  This is the correct primitive for multi-table writes that must
        be crash-safe: either all rows land or none do.

        Args:
            statements: List of ``(sql, params)`` pairs executed with
                ``conn.execute()``.
            many_statements: Optional list of ``(sql, params_list)`` pairs
                executed with ``conn.executemany()``.

        Raises:
            sqlite3.Error: Re-raised after rolling back if any statement fails.
        """
        with self._write_lock:
            conn = self._get_conn()
            try:
                conn.execute("BEGIN")
                for sql, params in statements:
                    conn.execute(sql, params)
                for sql, params_list in many_statements or []:
                    conn.executemany(sql, params_list)
                conn.execute("COMMIT")
            except sqlite3.Error:
                conn.execute("ROLLBACK")
                raise

    def close(self) -> None:
        """Close the database connection.

        When using the unified database (``_db_path is None``), delegates
        to ``vetinari.database.close_connection()``. When using a standalone
        path (tests), closes the shared connection directly.
        """
        if self._shared_conn is not None:
            self._shared_conn.close()
            self._shared_conn = None
        if self._db_path is None:
            from vetinari.database import close_connection

            close_connection()


def _normalize_task_checkpoint_row(row: tuple) -> tuple:
    if len(row) == 15:
        return row
    if len(row) == 14:
        # Insert substep_index=0 at position 4 (after superstep_index, before agent_type)
        return (*row[:4], 0, *row[4:])
    if len(row) == 11:
        (
            task_id,
            execution_id,
            agent_type,
            mode,
            status,
            input_json,
            output_json,
            manifest_hash,
            started_at,
            completed_at,
            retry_count,
        ) = row
        return (
            task_id,
            execution_id,
            task_id,
            0,
            0,
            agent_type,
            mode,
            status,
            input_json,
            output_json,
            None,
            manifest_hash,
            started_at,
            completed_at,
            retry_count,
        )
    msg = f"Task checkpoint rows must contain 11 legacy or 14 current fields; got {len(row)}"
    raise ValueError(msg)
