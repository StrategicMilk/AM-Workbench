"""SQLite schema creation for the private UnifiedMemoryStore connection.

Used only when UnifiedMemoryStore is opened with an explicit ``db_path``
(typically for test isolation).  The unified production database has its
schema created by ``vetinari.database.init_schema``.
"""

from __future__ import annotations

import logging
import sqlite3

logger = logging.getLogger(__name__)


# The full DDL for a private memory database. Keep in sync with
# vetinari/database.py init_schema whenever the schema changes.
_SCHEMA_SQL = """
    CREATE TABLE IF NOT EXISTS memories (
        id TEXT PRIMARY KEY,
        agent TEXT NOT NULL DEFAULT '',
        entry_type TEXT NOT NULL,
        content TEXT NOT NULL,
        summary TEXT NOT NULL DEFAULT '',
        timestamp INTEGER NOT NULL,
        provenance TEXT NOT NULL DEFAULT '',
        content_hash TEXT NOT NULL,
        previous_content_hash TEXT NOT NULL DEFAULT '',
        chain_hash TEXT NOT NULL DEFAULT '',
        forgotten INTEGER DEFAULT 0,
        access_count INTEGER DEFAULT 0,
        quality_score REAL DEFAULT 0.0,
        importance REAL DEFAULT 0.5,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        metadata_json TEXT,
        scope TEXT NOT NULL DEFAULT 'global',
        recall_count INTEGER DEFAULT 0,
        supersedes_id TEXT,
        relationship_type TEXT,
        last_accessed INTEGER DEFAULT 0
    );
    CREATE INDEX IF NOT EXISTS idx_mem_agent ON memories(agent);
    CREATE INDEX IF NOT EXISTS idx_mem_type ON memories(entry_type);
    CREATE INDEX IF NOT EXISTS idx_mem_hash ON memories(content_hash);
    CREATE INDEX IF NOT EXISTS idx_mem_ts ON memories(timestamp);
    CREATE INDEX IF NOT EXISTS idx_mem_forgotten ON memories(forgotten);

    CREATE TABLE IF NOT EXISTS embeddings (
        memory_id TEXT PRIMARY KEY,
        embedding_blob BLOB NOT NULL,
        model TEXT NOT NULL,
        dimensions INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        FOREIGN KEY (memory_id) REFERENCES memories(id) ON DELETE CASCADE
    );

    CREATE TABLE IF NOT EXISTS memory_episodes (
        episode_id TEXT PRIMARY KEY,
        timestamp TEXT NOT NULL,
        task_summary TEXT NOT NULL,
        agent_type TEXT NOT NULL,
        task_type TEXT NOT NULL,
        output_summary TEXT NOT NULL,
        quality_score REAL DEFAULT 0.0,
        success INTEGER DEFAULT 0,
        model_id TEXT DEFAULT '',
        importance REAL DEFAULT 0.5,
        metadata_json TEXT,
        created_at REAL DEFAULT (unixepoch())
    );
    CREATE INDEX IF NOT EXISTS idx_ep_type ON memory_episodes(task_type);
    CREATE INDEX IF NOT EXISTS idx_ep_agent ON memory_episodes(agent_type);
    CREATE INDEX IF NOT EXISTS idx_ep_score ON memory_episodes(quality_score);

    CREATE TABLE IF NOT EXISTS episode_embeddings (
        episode_id TEXT PRIMARY KEY,
        embedding_blob BLOB NOT NULL,
        FOREIGN KEY (episode_id) REFERENCES memory_episodes(episode_id) ON DELETE CASCADE
    );

    CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
        id,
        content,
        summary,
        agent,
        content=memories,
        content_rowid=rowid
    );
    CREATE TRIGGER IF NOT EXISTS memory_fts_ai AFTER INSERT ON memories BEGIN
        INSERT INTO memory_fts(rowid, id, content, summary, agent)
        VALUES (NEW.rowid, NEW.id, NEW.content, NEW.summary, NEW.agent);
    END;
    CREATE TRIGGER IF NOT EXISTS memory_fts_ad AFTER DELETE ON memories BEGIN
        INSERT INTO memory_fts(memory_fts, rowid, id, content, summary, agent)
        VALUES ('delete', OLD.rowid, OLD.id, OLD.content, OLD.summary, OLD.agent);
    END;
    CREATE TRIGGER IF NOT EXISTS memory_fts_au AFTER UPDATE ON memories BEGIN
        INSERT INTO memory_fts(memory_fts, rowid, id, content, summary, agent)
        VALUES ('delete', OLD.rowid, OLD.id, OLD.content, OLD.summary, OLD.agent);
        INSERT INTO memory_fts(rowid, id, content, summary, agent)
        VALUES (NEW.rowid, NEW.id, NEW.content, NEW.summary, NEW.agent);
    END;
"""


def create_vec_tables(conn: sqlite3.Connection, dimensions: int) -> bool:
    """Create sqlite-vec virtual tables for KNN memory and episode search.

    Creates ``memory_vec`` and ``episode_vec`` vec0 tables with the given
    embedding dimensionality.  Returns False and logs a warning if table
    creation fails (e.g. because the vec0 module is unavailable).

    Args:
        conn: SQLite connection with sqlite-vec already loaded.
        dimensions: Embedding vector dimensionality.

    Returns:
        True on success, False if table creation fails.
    """
    try:
        conn.execute(f"""
            CREATE VIRTUAL TABLE IF NOT EXISTS memory_vec USING vec0(
                memory_id TEXT PRIMARY KEY,
                embedding float[{dimensions}]
            )
        """)
        conn.execute(f"""
            CREATE VIRTUAL TABLE IF NOT EXISTS episode_vec USING vec0(
                episode_id TEXT PRIMARY KEY,
                embedding float[{dimensions}]
            )
        """)
        conn.commit()
        return True
    except sqlite3.OperationalError as exc:
        logger.warning("sqlite-vec table creation failed: %s", exc)
        return False


def _migrate_private_schema(conn: sqlite3.Connection) -> None:
    """Add new columns to an existing private memory database.

    Runs additive ALTER TABLE migrations so that databases created before
    the recall_count / supersedes_id / relationship_type columns were added
    continue to work without data loss.
    """
    _additive_columns: list[tuple[str, str]] = [
        ("recall_count", "INTEGER DEFAULT 0"),
        ("supersedes_id", "TEXT"),
        ("relationship_type", "TEXT"),
        ("last_accessed", "INTEGER DEFAULT 0"),
        ("scope", "TEXT NOT NULL DEFAULT 'global'"),
        ("previous_content_hash", "TEXT NOT NULL DEFAULT ''"),
        ("chain_hash", "TEXT NOT NULL DEFAULT ''"),
    ]
    try:
        cursor = conn.execute("PRAGMA table_info(memories)")
        existing = {row[1] for row in cursor.fetchall()}
    except sqlite3.OperationalError:
        logger.warning(
            "Could not inspect memories table columns — table may not exist yet, skipping additive migration"
        )
        return  # table doesn't exist yet — CREATE TABLE will add all columns
    for col_name, col_def in _additive_columns:
        if existing and col_name not in existing:
            logger.info("Adding column %s to memories (private store)", col_name)
            conn.execute(f"ALTER TABLE memories ADD COLUMN {col_name} {col_def}")
    try:
        cursor = conn.execute("PRAGMA table_info(embeddings)")
        embedding_cols = {row[1] for row in cursor.fetchall()}
    except sqlite3.OperationalError:
        embedding_cols = set()
    if embedding_cols and "dimensions" not in embedding_cols:
        logger.info("Adding column dimensions to embeddings (private store)")
        conn.execute("ALTER TABLE embeddings ADD COLUMN dimensions INTEGER NOT NULL DEFAULT 0")
    conn.commit()


def create_schema(conn: sqlite3.Connection) -> None:
    """Create the unified memory schema on the given connection.

    Executes the full DDL script (tables, indexes, FTS5 virtual table,
    and triggers) idempotently using ``IF NOT EXISTS`` guards.  Also runs
    additive column migrations for databases created before Session 19.

    Args:
        conn: SQLite connection to initialise.
    """
    _migrate_private_schema(conn)
    conn.cursor().executescript(_SCHEMA_SQL)
    conn.commit()
    logger.debug("Memory schema initialised")
