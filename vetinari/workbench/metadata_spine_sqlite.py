"""SQLite index helpers for the Workbench metadata spine."""

from __future__ import annotations

import json
import sqlite3
from typing import TYPE_CHECKING, Any

from vetinari.workbench.metadata_spine_records import (
    WorkbenchSpineCorrupt,
    _payload_to_record,
)
from vetinari.workbench.metadata_spine_validation import _SeenRecords
from vetinari.workbench.migration import run_all_migrations


class _WorkbenchSpineSqliteMixin:
    """SQLite integrity, schema, rebuild, insert, and query behavior."""

    if TYPE_CHECKING:
        _conn: Any
        _sqlite_path: Any
        _validate_dependencies_against_seen: Any
        _write_lock: Any

    def _check_sqlite_integrity(self) -> None:
        try:
            conn = sqlite3.connect(self._sqlite_path)
            try:
                rows = conn.execute("PRAGMA integrity_check").fetchall()
            finally:
                conn.close()
        except sqlite3.DatabaseError as exc:
            raise WorkbenchSpineCorrupt("SQLite integrity_check failed", path=self._sqlite_path) from exc
        if rows != [("ok",)]:
            raise WorkbenchSpineCorrupt("SQLite integrity_check failed", path=self._sqlite_path)

    def _create_schema(self) -> None:
        self._require_conn().execute(
            """
            CREATE TABLE IF NOT EXISTS records (
                kind TEXT NOT NULL,
                record_id TEXT NOT NULL,
                payload TEXT NOT NULL,
                created_order INTEGER PRIMARY KEY AUTOINCREMENT,
                UNIQUE(kind, record_id)
            )
            """
        )
        run_all_migrations(self._require_conn())

    def _configure_sqlite(self) -> None:
        conn = self._require_conn()
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=5000")
        except sqlite3.DatabaseError as exc:
            raise WorkbenchSpineCorrupt("SQLite concurrency configuration failed", path=self._sqlite_path) from exc

    def _rebuild_sqlite_from_records(self, records: list[dict[str, Any]]) -> None:
        conn = self._require_conn()
        seen = _SeenRecords()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM records")
            for row in records:
                kind = str(row["kind"])
                record_id = str(row["record_id"])
                payload = row["payload"]
                self._validate_dependencies_against_seen(kind, payload, seen)
                if kind == "delete":
                    target_kind = str(payload["target_kind"])
                    target_record_id = str(payload["target_record_id"])
                    conn.execute(
                        "DELETE FROM records WHERE kind = ? AND record_id = ?",
                        (target_kind, target_record_id),
                    )
                    seen.remove(target_kind, target_record_id)
                conn.execute(
                    "INSERT INTO records(kind, record_id, payload) VALUES (?, ?, ?)",
                    (kind, record_id, json.dumps(payload, separators=(",", ":"), sort_keys=True)),
                )
                seen.add(kind, record_id, payload)
            conn.execute("COMMIT")
        except WorkbenchSpineCorrupt:
            conn.execute("ROLLBACK")
            raise
        except sqlite3.DatabaseError as exc:
            conn.execute("ROLLBACK")
            raise WorkbenchSpineCorrupt("SQLite rebuild failed", path=self._sqlite_path) from exc

    def _insert_record(self, kind: str, record_id: str, payload: dict[str, Any]) -> None:
        try:
            if kind == "delete":
                self._require_conn().execute(
                    "DELETE FROM records WHERE kind = ? AND record_id = ?",
                    (str(payload["target_kind"]), str(payload["target_record_id"])),
                )
            self._require_conn().execute(
                "INSERT INTO records(kind, record_id, payload) VALUES (?, ?, ?)",
                (kind, record_id, json.dumps(payload, separators=(",", ":"), sort_keys=True)),
            )
        except sqlite3.IntegrityError as exc:
            raise WorkbenchSpineCorrupt(f"duplicate {kind} record_id {record_id!r}") from exc

    def _select(self, kind: str, *, limit: int | None = None) -> list[tuple[str, str, str]]:
        sql = "SELECT kind, record_id, payload FROM records WHERE kind = ? ORDER BY created_order"
        params: list[Any] = [kind]
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        with self._write_lock:
            return list(self._require_conn().execute(sql, params))

    @staticmethod
    def _record_from_row(row: tuple[str, str, str]) -> Any:
        kind, _record_id, payload_json = row
        payload = json.loads(payload_json)
        return _payload_to_record(kind, payload)

    def _require_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise WorkbenchSpineCorrupt("spine connection is closed", path=self._sqlite_path)
        return self._conn
