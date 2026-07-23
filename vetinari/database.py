"""Unified SQLite database module — single connection pool for all stores.

Consolidates 10+ separate SQLite databases into one unified database with
thread-local connections, WAL mode, and centralized schema initialization.
Credential vault remains encrypted and separate (~/.vetinari/vault/).

Usage:
    from vetinari.database import get_connection, init_schema

    conn = get_connection()  # Thread-local, WAL mode, auto-initialized
    cursor = conn.execute("SELECT * FROM quality_scores WHERE task_id = ?", (task_id,))

Environment:
    VETINARI_DB_PATH: Path to the unified database file.
        Defaults to ``<PROJECT_ROOT>/.vetinari/vetinari.db``.

Decision: Consolidate 10 SQLite stores into 1 (ADR-0072).
"""

from __future__ import annotations

import contextlib
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, cast

from vetinari.constants import VETINARI_STATE_DIR, get_user_dir
from vetinari.database_schema import _UNIFIED_SCHEMA

logger = logging.getLogger(__name__)

_CONNECTION_PRAGMA_RETRIES = 5
_CONNECTION_PRAGMA_RETRY_DELAY_S = 0.1


_safe_log_lock = threading.Lock()

# ── Default database location ────────────────────────────────────────────────
_DEFAULT_DB_DIR = VETINARI_STATE_DIR
_DEFAULT_DB_PATH = _DEFAULT_DB_DIR / "vetinari.db"

# Thread-local storage for connections
_thread_local = threading.local()


def _reachable_handlers(log: logging.Logger) -> list[logging.Handler]:
    """Return handlers that may receive records from *log* via propagation."""
    handlers: list[logging.Handler] = []
    current: logging.Logger | None = log
    while current is not None:
        for handler in current.handlers:
            if handler not in handlers:
                handlers.append(handler)
        if not current.propagate:
            break
        current = current.parent
    if logging.lastResort is not None and logging.lastResort not in handlers:
        handlers.append(logging.lastResort)
    return handlers


def _safe_log(level: int, message: str, *args: object) -> None:
    """Log non-critical database lifecycle messages without teardown noise."""
    if level < logging.WARNING:
        return
    with _safe_log_lock:
        handlers = _reachable_handlers(logger)
        original_handle_errors = [
            (handler, "handleError" in handler.__dict__, handler.__dict__.get("handleError")) for handler in handlers
        ]
        try:
            for handler, _had_instance_override, _original in original_handle_errors:
                mutable_handler: Any = handler
                mutable_handler.handleError = lambda _record: None
            with contextlib.suppress(OSError, ValueError):
                logger.log(level, message, *args)
        finally:
            for handler, had_instance_override, original in original_handle_errors:
                restorable_handler: Any = handler
                if had_instance_override:
                    restorable_handler.handleError = original
                else:
                    with contextlib.suppress(AttributeError):
                        del restorable_handler.handleError


# Lock for schema initialization (one-time operation)
_schema_init_lock = threading.Lock()
_schema_initialized = False
_schema_initialized_paths: set[Path] = set()


def _get_db_path(db_path: str | os.PathLike[str] | None = None) -> Path:
    """Resolve the database file path from env var or default.

    Returns:
        Absolute path to the unified SQLite database file.
    """
    if db_path is not None:
        return Path(db_path)
    env_path = os.environ.get("VETINARI_DB_PATH")
    if env_path:
        return Path(env_path)
    return cast(Path, get_user_dir()) / "vetinari.db"


def _schema_path_key(db_path: str | os.PathLike[str] | None = None) -> Path:
    """Return a stable key for the currently configured physical DB path."""
    return _get_db_path(db_path).expanduser().resolve(strict=False)


def get_connection(db_path: str | os.PathLike[str] | None = None) -> sqlite3.Connection:
    """Return a thread-local SQLite connection to the unified database.

    Creates the connection on first call per thread, sets WAL mode,
    and ensures the schema is initialized. Subsequent calls on the
    same thread return the cached connection. If VETINARI_DB_PATH changes,
    closes and recreates the connection to the new path.

    Returns:
        A ``sqlite3.Connection`` with WAL mode and row factory enabled.

    Raises:
        sqlite3.Error: If SQLite cannot open or initialize the database.
    """
    conn: sqlite3.Connection | None = getattr(_thread_local, "connection", None)
    cached_db_path = getattr(_thread_local, "db_path", None)
    resolved_db_path = _get_db_path(db_path)

    # If we have a cached connection but the DB path changed, close and reconnect
    if conn is not None and cached_db_path is not None and cached_db_path != resolved_db_path:
        _safe_log(
            logging.WARNING,
            "Database path changed from %s to %s — closing and reconnecting",
            cached_db_path,
            resolved_db_path,
        )
        conn.close()
        conn = None
        _thread_local.connection = None
        _thread_local.db_path = None

    if conn is not None:
        return conn

    resolved_db_path.parent.mkdir(parents=True, exist_ok=True)

    last_lock_error: sqlite3.OperationalError | None = None
    for attempt in range(_CONNECTION_PRAGMA_RETRIES):
        conn = sqlite3.connect(str(resolved_db_path), check_same_thread=False, timeout=30.0)
        try:
            conn.execute("PRAGMA busy_timeout=5000")  # 5 second wait on lock contention
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA wal_autocheckpoint=1000")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA cache_size=-32768")  # 32 MB page cache
            conn.execute("PRAGMA mmap_size=268435456")  # 256 MB memory-mapped I/O
            break
        except sqlite3.OperationalError as exc:
            conn.close()
            if "locked" not in str(exc).lower() or attempt == _CONNECTION_PRAGMA_RETRIES - 1:
                raise
            last_lock_error = exc
            time.sleep(_CONNECTION_PRAGMA_RETRY_DELAY_S * (attempt + 1))
    else:
        raise sqlite3.OperationalError("database remained locked during connection setup") from last_lock_error
    conn.row_factory = sqlite3.Row

    _thread_local.connection = conn
    _thread_local.db_path = resolved_db_path

    # Ensure schema is created (thread-safe, one-time)
    init_schema(conn)

    _safe_log(
        logging.DEBUG,
        "Database connection established for thread %s at %s",
        threading.current_thread().name,
        resolved_db_path,
    )
    return conn


def close_connection() -> None:
    """Close the thread-local connection if it exists.

    Call this during thread shutdown or test cleanup. The next call
    to ``get_connection()`` on this thread will create a fresh connection.
    """
    conn: sqlite3.Connection | None = getattr(_thread_local, "connection", None)
    if conn is not None:
        conn.close()
        _thread_local.connection = None
        _safe_log(logging.DEBUG, "Database connection closed for thread %s", threading.current_thread().name)


# ── Unified schema ───────────────────────────────────────────────────────────
def _schema_migration_statements(conn: sqlite3.Connection) -> list[str]:
    """Return startup schema repair statements for the current database.

    ``CREATE TABLE IF NOT EXISTS`` silently skips creation when a table
    already exists with the old column set, then subsequent ``CREATE INDEX``
    statements fail because expected columns are missing. Startup migrations
    preserve kaizen rows and only drop legacy benchmark/defect tables whose
    old shapes have no supported row-preserving upgrade path here.
    """
    statements: list[str] = []
    # Map: table_name -> column that MUST exist in the current schema.
    # Kaizen tables use additive migrations below because they contain user
    # improvement history that must not be discarded on startup.
    _required_columns: dict[str, str] = {
        "benchmark_results": "run_id",
        "benchmark_runs": "suite_name",
        # defect_occurrences was added in session-2B — drop and recreate if stale
        "defect_occurrences": "occurred_at",
    }
    for table, required_col in _required_columns.items():
        try:
            cursor = conn.execute(f"PRAGMA table_info({table})")
            columns = {row[1] for row in cursor.fetchall()}
        except sqlite3.OperationalError:
            logger.warning("Table %s not yet created; skipping column migration check", table)
            continue  # table doesn't exist yet — nothing to migrate
        if columns and required_col not in columns:
            _safe_log(
                logging.INFO,
                "Migrating stale table %s (missing column %s) — dropping and recreating",
                table,
                required_col,
            )
            statements.append(f"DROP TABLE IF EXISTS {table};")

    return statements


# Per-table additive columns applied individually (outside executescript) so
# that each ALTER TABLE can be wrapped in try/except for concurrent-process safety.
_ADDITIVE_COLUMNS: dict[str, list[tuple[str, str]]] = {
    "improvements": [
        ("hypothesis", "TEXT NOT NULL DEFAULT ''"),
        ("target_value", "REAL NOT NULL DEFAULT 0.0"),
        ("applied_by", "TEXT NOT NULL DEFAULT 'system'"),
        ("created_at", "TEXT"),
        ("observation_window_hours", "INTEGER NOT NULL DEFAULT 168"),
        ("regression_detected", "INTEGER NOT NULL DEFAULT 0"),
        ("rollback_plan", "TEXT NOT NULL DEFAULT ''"),
        ("confirmed_at", "TEXT"),
        ("reverted_at", "TEXT"),
        ("notes", "TEXT DEFAULT ''"),
    ],
    "improvement_observations": [
        ("observation_id", "INTEGER"),
        ("sample_size", "INTEGER NOT NULL DEFAULT 1"),
    ],
    "memories": [
        ("scope", "TEXT NOT NULL DEFAULT 'global'"),
        ("recall_count", "INTEGER DEFAULT 0"),
        ("supersedes_id", "TEXT"),
        ("relationship_type", "TEXT"),
        ("last_accessed", "INTEGER DEFAULT 0"),
    ],
    "embeddings": [("dimensions", "INTEGER NOT NULL DEFAULT 0")],
    "episode_embeddings": [
        ("model", "TEXT NOT NULL DEFAULT ''"),
        ("dimensions", "INTEGER NOT NULL DEFAULT 0"),
    ],
    "memory_episodes": [("scope", "TEXT NOT NULL DEFAULT 'global'")],
    "execution_state": [("terminal_status", "TEXT")],
    "PlanHistory": [("plan_explanation_json", "TEXT")],
    "SubtaskMemory": [
        ("subtask_explanation_json", "TEXT"),
        ("quality_score", "REAL DEFAULT 0.0"),
    ],
}


def _apply_additive_column_migrations(conn: sqlite3.Connection) -> None:
    """Apply per-table additive column migrations individually with race safety.

    Each ALTER TABLE is executed outside executescript() so duplicate-column
    errors from concurrent processes are tolerated via try/except.  Also
    stamps every ordinary table with ``schema_version`` and derives
    ``recall_count`` from ``access_count`` in the ``memories`` table.

    Args:
        conn: Open SQLite connection.
    """
    # 1. Named per-table column additions (pre-existing list)
    for table, cols in _ADDITIVE_COLUMNS.items():
        try:
            rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        except sqlite3.OperationalError:
            logger.warning("Table %s not yet created; skipping additive column migration", table)
            continue
        existing = {row[1] for row in rows}
        if not existing:
            continue  # table missing — CREATE TABLE will add the column
        for col_name, col_def in cols:
            if col_name not in existing:
                _safe_log(logging.INFO, "Adding column %s to %s", col_name, table)
                try:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {col_name} {col_def}")
                    conn.commit()
                except sqlite3.OperationalError as exc:
                    if "duplicate column" in str(exc).lower():
                        logger.info("Race: column %s.%s already added by peer", table, col_name)
                    else:
                        raise

    # 2. Stamp schema_version on every ordinary table that lacks it.
    #    Skip sqlite_* internal tables and memory_fts* virtual tables.
    try:
        all_tables = [
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            if not row[0].startswith(("sqlite_", "memory_fts"))
        ]
    except sqlite3.OperationalError as exc:
        logger.warning("Could not enumerate SQLite tables for schema_version stamping: %s", exc)
        all_tables = []

    for table in all_tables:
        try:
            rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        except sqlite3.OperationalError as exc:
            logger.warning("Could not inspect table %s for schema_version stamping: %s", table, exc)
            continue
        existing = {row[1] for row in rows}
        if "schema_version" not in existing:
            _safe_log(logging.INFO, "Stamping schema_version on table %s", table)
            try:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN schema_version INTEGER NOT NULL DEFAULT 1")
                conn.commit()
            except sqlite3.OperationalError as exc:
                if "duplicate column" in str(exc).lower():
                    logger.info("Race: schema_version already added to %s by peer", table)
                else:
                    raise

    # 3. Derive recall_count from access_count for legacy memories rows.
    try:
        mem_rows = conn.execute("PRAGMA table_info(memories)").fetchall()
    except sqlite3.OperationalError:
        mem_rows = []
    mem_cols = {row[1] for row in mem_rows}
    if "recall_count" in mem_cols and "access_count" in mem_cols:
        conn.execute("UPDATE memories SET recall_count = access_count WHERE recall_count = 0 AND access_count > 0")
        conn.commit()

    try:
        obs_rows = conn.execute("PRAGMA table_info(improvement_observations)").fetchall()
    except sqlite3.OperationalError:
        obs_rows = []
    obs_cols = {row[1] for row in obs_rows}
    if "observation_id" in obs_cols:
        conn.execute(
            "UPDATE improvement_observations "
            "SET observation_id = rowid "
            "WHERE observation_id IS NULL OR observation_id = 0"
        )
        conn.commit()


def init_schema(conn: sqlite3.Connection | None = None) -> None:
    """Create all tables in the unified schema if they don't exist.

    Thread-safe — only executes once per process. Subsequent calls are no-ops.

    Args:
        conn: Connection to use. Required on first call (provided by
            ``get_connection()`` automatically). If None and schema is
            already initialized, this is a no-op.

    Raises:
        sqlite3.Error: If schema migration or creation fails.
    """
    global _schema_initialized
    schema_key = _schema_path_key()
    if schema_key in _schema_initialized_paths:
        return

    with _schema_init_lock:
        if schema_key in _schema_initialized_paths:
            return

        if conn is None:
            # Schema not initialized and no connection provided — caller
            # must use get_connection() which will pass conn to us.
            return

        # Additive column migrations run BEFORE executescript so that
        # legacy tables gain columns (e.g. scope, recall_count) before the
        # unified schema tries to CREATE INDEX on those columns.  Each ALTER
        # TABLE is individually wrapped in try/except for concurrent-process
        # race safety (duplicate column name from peer process).
        _apply_additive_column_migrations(conn)
        migration_sql = "\n".join(_schema_migration_statements(conn))
        script = f"BEGIN IMMEDIATE;\n{migration_sql}\n{_UNIFIED_SCHEMA}\nCOMMIT;"
        try:
            conn.executescript(script)
        except sqlite3.Error:
            with contextlib.suppress(sqlite3.Error):
                conn.execute("ROLLBACK")
            raise
        # Run again after executescript so that tables created by the schema
        # script (PlanHistory, SubtaskMemory, etc.) also receive schema_version.
        _apply_additive_column_migrations(conn)
        _schema_initialized_paths.add(schema_key)
        _schema_initialized = True
        _safe_log(logging.INFO, "Unified database schema initialized at %s", schema_key)


def reset_for_testing() -> None:
    """Reset module state for test isolation.

    Closes any thread-local connection and resets the schema-initialized
    flag so the next ``get_connection()`` call creates a fresh database.
    """
    global _schema_initialized
    close_connection()
    with _schema_init_lock:
        _schema_initialized = False
        _schema_initialized_paths.clear()


def execute_query(sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
    """Execute a read query and return all rows.

    Convenience wrapper that acquires the thread-local connection,
    executes the query, and returns results.

    Args:
        sql: SQL SELECT statement.
        params: Query parameters.

    Returns:
        List of sqlite3.Row objects (dict-like access by column name).
    """
    conn = get_connection()
    cursor = conn.execute(sql, params)
    return cursor.fetchall()


def execute_write(sql: str, params: tuple[Any, ...] = ()) -> int:
    """Execute a write statement (INSERT/UPDATE/DELETE) and return rowcount.

    Commits the transaction after execution.

    Args:
        sql: SQL write statement.
        params: Statement parameters.

    Returns:
        Number of rows affected.
    """
    conn = get_connection()
    cursor = conn.execute(sql, params)
    conn.commit()
    return cursor.rowcount
