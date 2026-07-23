"""Migration runner for Vetinari storage schemas."""

from __future__ import annotations

import contextlib
import hashlib
import logging
import re
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)

_MIGRATIONS_DIR = Path(__file__).resolve().parent
_SCHEMA_FILE = _MIGRATIONS_DIR / "schema.sql"
_MIGRATION_FILENAME_RE = re.compile(r"^(?P<number>\d{3})_.*\.sql$")


def _run_script_transaction(conn: sqlite3.Connection, sql: str, trailer_sql: str = "") -> None:
    """Run a SQL script inside one SQLite transaction."""
    if trailer_sql and not trailer_sql.rstrip().endswith(";"):
        trailer_sql = f"{trailer_sql};"
    script = f"BEGIN IMMEDIATE;\n{sql}\n{trailer_sql}\nCOMMIT;"
    try:
        conn.executescript(script)
    except sqlite3.Error:
        with contextlib.suppress(sqlite3.Error):
            conn.execute("ROLLBACK")
        raise


def _ensure_migration_history_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS _migration_history ("
        "  filename TEXT PRIMARY KEY,"
        "  migration_id TEXT,"
        "  sha256 TEXT,"
        "  path TEXT,"
        "  applied_at TEXT NOT NULL DEFAULT (datetime('now'))"
        ")"
    )
    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(_migration_history)").fetchall()}
    for column, definition in {"migration_id": "TEXT", "sha256": "TEXT", "path": "TEXT"}.items():
        if column not in existing_cols:
            conn.execute(f"ALTER TABLE _migration_history ADD COLUMN {column} {definition}")


def _apply_base_schema(conn: sqlite3.Connection) -> None:
    if not _SCHEMA_FILE.exists():
        return
    sql = _SCHEMA_FILE.read_text(encoding="utf-8")
    _execute_add_column_directives(conn, sql)
    _run_script_transaction(conn, sql)
    logger.info("Base schema applied from %s", _SCHEMA_FILE.name)


def _already_applied_migrations(conn: sqlite3.Connection) -> dict[str, str]:
    return {row[0]: row[1] for row in conn.execute("SELECT filename, sha256 FROM _migration_history").fetchall()}


def _validate_migration_sequence(db_path: Path, mfile: Path, previous_number: int) -> int | None:
    match = _MIGRATION_FILENAME_RE.match(mfile.name)
    if match is None:
        logger.warning("Skipping migration with unexpected filename: %s", mfile.name)
        return None
    migration_number = int(match.group("number"))
    if migration_number != previous_number + 1:
        raise RuntimeError(
            f"Migration sequence gap at {db_path}: expected migration "
            f"{previous_number + 1:03d} but found {migration_number:03d}. "
            "Migrations halted - resolve the gap before re-running."
        )
    return migration_number


def _verify_applied_digest(mfile: Path, digest: str, already_applied: dict[str, str]) -> bool:
    if mfile.name not in already_applied:
        return False
    recorded_digest = already_applied[mfile.name]
    if recorded_digest and recorded_digest != digest:
        raise RuntimeError(
            f"Migration {mfile.name} has changed since it was applied; "
            "refusing to treat filename-only history as clean."
        )
    return True


def _register_migration_functions(conn: sqlite3.Connection, mfile: Path, digest: str) -> None:
    migration_id = f"{mfile.name}:{digest[:16]}"
    conn.create_function("_vetinari_migration_filename", 0, lambda name=mfile.name: name)
    conn.create_function("_vetinari_migration_id", 0, lambda value=migration_id: value)
    conn.create_function("_vetinari_migration_sha256", 0, lambda value=digest: value)
    conn.create_function(
        "_vetinari_migration_path",
        0,
        lambda value=str(mfile.resolve(strict=False)): value,
    )


def _migration_history_trailer() -> str:
    return (
        "INSERT INTO _migration_history (filename, migration_id, sha256, path) "
        "VALUES (_vetinari_migration_filename(), _vetinari_migration_id(), "
        "_vetinari_migration_sha256(), _vetinari_migration_path())"
    )


_ADD_COLUMN_DIRECTIVE_RE = re.compile(r"^--\s*vetinari:add-column-if-missing\s+(\w+)\s+(\w+)\s+(.+)$")


def _execute_add_column_directives(conn: sqlite3.Connection, sql: str) -> None:
    """Parse and execute ``-- vetinari:add-column-if-missing`` directives.

    Each directive has the form::

        -- vetinari:add-column-if-missing <table> <column> <definition>

    If the table does not exist (``PRAGMA table_info`` returns empty), the
    directive is silently skipped.  If the column already exists the ALTER
    TABLE is skipped (idempotent).  Duplicate-column races are tolerated via
    ``sqlite3.OperationalError`` suppression.

    Args:
        conn: Open SQLite connection.
        sql: Raw SQL text of the migration file.
    """
    for line in sql.splitlines():
        match = _ADD_COLUMN_DIRECTIVE_RE.match(line.strip())
        if match is None:
            continue
        table, column, definition = match.group(1), match.group(2), match.group(3).strip()
        try:
            rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        except sqlite3.OperationalError:
            logger.warning("Table %s not accessible; skipping add-column directive", table)
            continue
        if not rows:
            # Table does not exist yet — skip safely
            logger.debug("Table %s missing; skipping add-column-if-missing for %s", table, column)
            continue
        existing_cols = {row[1] for row in rows}
        if column in existing_cols:
            logger.debug("Column %s.%s already exists; skipping directive", table, column)
            continue
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
            conn.commit()
            logger.info("Added column %s to %s via migration directive", column, table)
        except sqlite3.OperationalError as exc:
            # Tolerate duplicate-column races from concurrent processes
            if "duplicate column" in str(exc).lower():
                logger.info("Race: column %s.%s already added by peer process", table, column)
            else:
                raise


def _apply_one_migration(
    conn: sqlite3.Connection,
    mfile: Path,
    already_applied: dict[str, str],
) -> bool:
    """Apply a single numbered migration file if not already applied.

    Args:
        conn: Open SQLite connection.
        mfile: Path to the ``.sql`` migration file.
        already_applied: Map of filename to recorded sha256.

    Returns:
        True if the migration was applied, False if already recorded.
    """
    sql = mfile.read_text(encoding="utf-8")
    digest = hashlib.sha256(sql.encode("utf-8")).hexdigest()
    if _verify_applied_digest(mfile, digest, already_applied):
        return False
    _register_migration_functions(conn, mfile, digest)
    # Execute directive-based operations before the script transaction so that
    # ADD COLUMN IF NOT EXISTS semantics work (pure SQLite lacks this syntax).
    _execute_add_column_directives(conn, sql)
    # Strip directives from the SQL so executescript() only sees valid SQL.
    stripped_sql = "\n".join(line for line in sql.splitlines() if not _ADD_COLUMN_DIRECTIVE_RE.match(line.strip()))
    _run_script_transaction(conn, stripped_sql, _migration_history_trailer())
    logger.info("Applied migration: %s", mfile.name)
    return True


def _apply_numbered_migrations(conn: sqlite3.Connection, db_path: Path) -> int:
    migration_files = sorted(_MIGRATIONS_DIR.glob("[0-9][0-9][0-9]_*.sql"))
    already_applied = _already_applied_migrations(conn)
    previous_number = 0
    applied = 0
    for mfile in migration_files:
        next_number = _validate_migration_sequence(db_path, mfile, previous_number)
        if next_number is None:
            continue
        previous_number = next_number
        if _apply_one_migration(conn, mfile, already_applied):
            applied += 1
    return applied


def run_migrations(db_path: str | Path) -> int:
    """Initialise or upgrade the SQLite database at *db_path*.

    Returns:
        Value produced for the caller.
    """
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(db_path), isolation_level=None)
    try:
        _ensure_migration_history_table(conn)
        _apply_base_schema(conn)
        applied = _apply_numbered_migrations(conn, db_path)
        if applied == 0:
            logger.info("Database at %s is up-to-date", db_path)
        else:
            logger.info("Applied %d migration(s) to %s", applied, db_path)
        return applied
    finally:
        conn.close()
