"""Persistent self-improvement feedback store.

Step 0 for self-improvement learning is keeping feedback signals durable across
restart. This module writes an append-only JSONL source of truth and maintains a
SQLite index for query speed. Importing this module performs no filesystem I/O;
the first FeedbackStore construction creates runtime files.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import sqlite3
import threading
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from vetinari.boundary_guards import account_evidence_drop
from vetinari.constants import OUTPUTS_DIR
from vetinari.lifecycle.policies import ArchivePolicy

logger = logging.getLogger(__name__)


_BASE_DEFAULT_STORE_DIR = OUTPUTS_DIR / "learning" / "feedback_store"
_JSONL_FILENAME = "signals.jsonl"
_SQLITE_FILENAME = "signals.sqlite"
_SCHEMA_VERSION = 1


def _default_store_dir() -> Path:
    worker_id = os.environ.get("PYTEST_XDIST_WORKER")
    if worker_id:
        return _BASE_DEFAULT_STORE_DIR / "pytest-xdist" / worker_id
    return _BASE_DEFAULT_STORE_DIR


class FeedbackStoreCorrupt(Exception):
    """Raised when durable feedback state is unreadable or internally unsafe."""

    def __init__(self, reason: str, *, path: Path | None = None) -> None:
        self.reason = reason
        self.path = path
        suffix = f" ({path})" if path is not None else ""
        super().__init__(f"{reason}{suffix}")


class FeedbackStore:
    """Durable JSONL plus SQLite feedback signal store."""

    def __init__(self, store_dir: Path | None = None, *, auto_compact_days: int | None = None) -> None:
        self.store_dir = Path(store_dir) if store_dir is not None else _default_store_dir()
        self.jsonl_path = self.store_dir / _JSONL_FILENAME
        self.sqlite_path = self.store_dir / _SQLITE_FILENAME
        self._write_lock = threading.Lock()
        self._policy = ArchivePolicy()
        # auto_compact_days reserved for future scheduler integration; 0 / None
        # disables automatic compaction so explicit rotate_cold_signals calls
        # are the only mover of records out of the live store.
        self._auto_compact_days = auto_compact_days

        try:
            self.store_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise FeedbackStoreCorrupt("Feedback store directory unavailable", path=self.store_dir) from exc

        jsonl_exists = self.jsonl_path.exists()
        sqlite_exists = self.sqlite_path.exists()
        if sqlite_exists and not jsonl_exists:
            raise FeedbackStoreCorrupt(
                "JSONL missing but SQLite present - append-log is the source of truth",
                path=self.jsonl_path,
            )

        should_log_rebuild = jsonl_exists and not sqlite_exists
        if not jsonl_exists:
            self.jsonl_path.touch()

        records = self._read_jsonl_records()
        if sqlite_exists:
            self._validate_sqlite()
        else:
            if should_log_rebuild:
                logger.warning("Rebuilding SQLite index from %s", self.jsonl_path)
            self._rebuild_sqlite(records)
            return

        self._ensure_schema()

    def append_signal(self, signal: dict[str, Any]) -> None:
        """Append one feedback signal and index it in SQLite.

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        record = self._normalize_signal(signal)
        self._validate_signal_record(record)
        payload = json.dumps(record, sort_keys=True, separators=(",", ":"))
        with self._write_lock:
            try:
                conn = self._connect()
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    with self.jsonl_path.open("a", encoding="utf-8", newline="\n") as fh:
                        fh.write(payload + "\n")
                        fh.flush()
                        os.fsync(fh.fileno())
                    self._insert_signal(conn, record)
                    conn.commit()
                except Exception:
                    conn.rollback()
                    account_evidence_drop(record, "feedback_store", logger=logger)
                    raise
                finally:
                    conn.close()
            except Exception as exc:
                if isinstance(exc, FeedbackStoreCorrupt):
                    raise
                raise FeedbackStoreCorrupt("Feedback signal append failed", path=self.jsonl_path) from exc

    def list_signals(
        self,
        *,
        model_id: str | None = None,
        task_type: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Return indexed feedback signals, newest first.

        Returns:
            Collection of signals values.

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        clauses: list[str] = []
        params: list[Any] = []
        if model_id is not None:
            clauses.append("model_id = ?")
            params.append(model_id)
        if task_type is not None:
            clauses.append("task_type = ?")
            params.append(task_type)

        query = "SELECT payload_json FROM signals"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY timestamp DESC, rowid DESC"
        if limit is not None:
            query += " LIMIT ?"
            params.append(limit)

        try:
            conn = self._connect()
            try:
                rows = conn.execute(query, params).fetchall()
            finally:
                conn.close()
        except sqlite3.Error as exc:
            raise FeedbackStoreCorrupt("Feedback signal query failed", path=self.sqlite_path) from exc
        return [json.loads(row[0]) for row in rows]

    def purge_expired_signals(self, *, cutoff_days: int = 30, now: datetime | None = None) -> int:
        """Physically remove feedback signals older than the retention cutoff.

        Args:
            cutoff_days: Maximum age in days to retain. Must be non-negative.
            now: Optional reference time for deterministic tests.

        Returns:
            Number of signal rows removed from the append log and SQLite index.

        Raises:
            ValueError: If ``cutoff_days`` is negative.
            FeedbackStoreCorrupt: If persisted feedback state is unreadable or
                cannot be rewritten safely.
        """
        if cutoff_days < 0:
            raise ValueError("cutoff_days must be non-negative")
        reference = now if now is not None else datetime.now(timezone.utc)
        records = self._read_jsonl_records()
        kept: list[dict[str, Any]] = []
        removed = 0
        for record in records:
            timestamp = self._parse_record_timestamp(record)
            age = reference - timestamp
            if age.total_seconds() > cutoff_days * 24 * 60 * 60:
                removed += 1
            else:
                kept.append(record)
        if removed == 0:
            return 0

        with self._write_lock:
            payload = "".join(
                json.dumps(self._normalize_signal(record), sort_keys=True, separators=(",", ":")) + "\n"
                for record in kept
            )
            tmp_path = self.jsonl_path.with_name(f".{self.jsonl_path.name}.{os.getpid()}.tmp")
            try:
                with tmp_path.open("w", encoding="utf-8", newline="\n") as fh:
                    fh.write(payload)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp_path, self.jsonl_path)
                self._rebuild_sqlite(kept)
            except Exception as exc:
                with contextlib.suppress(FileNotFoundError):
                    tmp_path.unlink()
                if isinstance(exc, FeedbackStoreCorrupt):
                    raise
                raise FeedbackStoreCorrupt("Feedback retention purge failed", path=self.jsonl_path) from exc
        return removed

    def delete_signals_for_subject(self, subject: str) -> int:
        """Physically remove feedback signals containing a subject marker.

        Args:
        subject: Exact subject marker to erase from persisted feedback.

        Returns:
        Number of feedback signals removed from JSONL and SQLite.

        Raises:
            FeedbackStoreCorrupt: Propagated when validation, persistence, or execution fails.
        """
        marker = subject.strip()
        if not marker:
            return 0
        records = self._read_jsonl_records()
        kept = [record for record in records if not _record_has_exact_subject(record, marker)]
        removed = len(records) - len(kept)
        if removed == 0:
            return 0

        with self._write_lock:
            payload = "".join(
                json.dumps(self._normalize_signal(record), sort_keys=True, separators=(",", ":")) + "\n"
                for record in kept
            )
            tmp_path = self.jsonl_path.with_name(f".{self.jsonl_path.name}.{os.getpid()}.tmp")
            try:
                with tmp_path.open("w", encoding="utf-8", newline="\n") as fh:
                    fh.write(payload)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp_path, self.jsonl_path)
                self._rebuild_sqlite(kept)
            except Exception as exc:
                with contextlib.suppress(FileNotFoundError):
                    tmp_path.unlink()
                if isinstance(exc, FeedbackStoreCorrupt):
                    raise
                raise FeedbackStoreCorrupt("Feedback subject deletion failed", path=self.jsonl_path) from exc
        return removed

    def count_signals_for_subject(self, subject: str) -> int:
        """Return the number of feedback signals with an exact subject value.

        Returns:
            Number of exact subject matches in the durable JSONL store.
        """
        marker = subject.strip()
        if not marker:
            return 0
        return sum(1 for record in self._read_jsonl_records() if _record_has_exact_subject(record, marker))

    def signals_for_subject(self, subject: str) -> list[dict[str, Any]]:
        """Return feedback signals with an exact subject value.

        Returns:
            Feedback signal records whose values exactly match the subject marker.
        """
        marker = subject.strip()
        if not marker:
            return []
        return [record for record in self._read_jsonl_records() if _record_has_exact_subject(record, marker)]

    def set_drift_count(self, task_type: str, count: int) -> None:
        """Persist the current consecutive drift count for a task type.

        Args:
            task_type: Task type value consumed by set_drift_count().
            count: Count value consumed by set_drift_count().

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        with self._write_lock:
            try:
                conn = self._connect()
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    conn.execute(
                        """
                        INSERT INTO drift_counts(task_type, count)
                        VALUES (?, ?)
                        ON CONFLICT(task_type) DO UPDATE SET count = excluded.count
                        """,
                        (task_type, int(count)),
                    )
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
                finally:
                    conn.close()
            except Exception as exc:
                if isinstance(exc, FeedbackStoreCorrupt):
                    raise
                raise FeedbackStoreCorrupt("Drift count write failed", path=self.sqlite_path) from exc

    def get_drift_count(self, task_type: str) -> int:
        """Return one persisted drift count, defaulting to zero.

        Returns:
            Resolved drift count value.

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        try:
            conn = self._connect()
            try:
                row = conn.execute("SELECT count FROM drift_counts WHERE task_type = ?", (task_type,)).fetchone()
            finally:
                conn.close()
        except sqlite3.Error as exc:
            raise FeedbackStoreCorrupt("Drift count read failed", path=self.sqlite_path) from exc
        return int(row[0]) if row is not None else 0

    def get_all_drift_counts(self) -> dict[str, int]:
        """Return all persisted drift counts.

        Returns:
            Resolved all drift counts value.

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        try:
            conn = self._connect()
            try:
                rows = conn.execute("SELECT task_type, count FROM drift_counts").fetchall()
            finally:
                conn.close()
        except sqlite3.Error as exc:
            raise FeedbackStoreCorrupt("Drift count snapshot failed", path=self.sqlite_path) from exc
        return {str(task_type): int(count) for task_type, count in rows}

    def rotate_cold_signals(self, *, now: datetime | None = None) -> int:
        """Physically remove signals whose age has crossed into the cold tier.

        ADR-0119 cold-tier classification: any signal older than the configured
        ``cooling_days`` threshold (default 30) is "cold" and rotated out of the
        live store.  The append log is rewritten and the SQLite index is
        rebuilt; restart-safe.

        Args:
            now: Reference timestamp for deterministic tests.

        Returns:
            Number of signal rows removed from the live store.

        Raises:
            FeedbackStoreCorrupt: If persisted feedback state is unreadable or
                cannot be rewritten safely.
        """
        return self.purge_expired_signals(cutoff_days=self._policy.cooling_days, now=now)

    def tier_for_signal(self, signal: dict[str, Any], *, now: datetime | None = None) -> str:
        """Classify a signal into ArchivePolicy's recent/cooling/cold tiers.

        Returns:
            str value produced by tier_for_signal().
        """
        timestamp = str(signal["timestamp"])
        record = SimpleNamespace(retired_at_utc=timestamp)
        reference = now if now is not None else datetime.now(timezone.utc)
        return self._policy.surface_buckets(record, reference)

    def _connect(self) -> sqlite3.Connection:
        try:
            conn = sqlite3.connect(self.sqlite_path, timeout=30)
            # RD-014 fix: enable WAL journaling for crash-recovery safety,
            # matching RD-003/RD-013 pattern across other Vetinari SQLite stores.
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            return conn
        except sqlite3.Error as exc:
            raise FeedbackStoreCorrupt("SQLite open failed", path=self.sqlite_path) from exc

    def _ensure_schema(self) -> None:
        try:
            conn = self._connect()
            try:
                self._create_schema(conn)
                conn.commit()
            finally:
                conn.close()
        except sqlite3.Error as exc:
            raise FeedbackStoreCorrupt("SQLite schema setup failed", path=self.sqlite_path) from exc

    def _create_schema(self, conn: sqlite3.Connection) -> None:
        conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS signals (
                signal_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                model_id TEXT NOT NULL,
                task_type TEXT NOT NULL,
                action TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                tier TEXT NOT NULL,
                payload_json TEXT NOT NULL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_signals_model_task ON signals(model_id, task_type)")
        # list_signals(limit=N) without filters orders by timestamp DESC,
        # rowid DESC. Without this index it does a full-table sort once
        # the signals table grows — clearly an O(n log n) hot-path on read.
        conn.execute("CREATE INDEX IF NOT EXISTS idx_signals_timestamp ON signals(timestamp DESC)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS drift_counts (
                task_type TEXT PRIMARY KEY,
                count INTEGER NOT NULL
            )
            """
        )
        conn.execute("INSERT OR IGNORE INTO schema_version(version) VALUES (?)", (_SCHEMA_VERSION,))

    def _read_jsonl_records(self) -> list[dict[str, Any]]:
        try:
            data = self.jsonl_path.read_bytes()
        except OSError as exc:
            raise FeedbackStoreCorrupt("JSONL read failed", path=self.jsonl_path) from exc
        if data and not data.endswith(b"\n"):
            raise FeedbackStoreCorrupt("JSONL truncated; last line incomplete", path=self.jsonl_path)

        records: list[dict[str, Any]] = []
        try:
            with self.jsonl_path.open(encoding="utf-8") as fh:
                for line_no, line in enumerate(fh, 1):
                    if not line.strip():
                        continue
                    try:
                        records.append(json.loads(line))
                    except json.JSONDecodeError as exc:
                        raise FeedbackStoreCorrupt(
                            f"JSONL parse failed at line {line_no}",
                            path=self.jsonl_path,
                        ) from exc
        except OSError as exc:
            raise FeedbackStoreCorrupt("JSONL read failed", path=self.jsonl_path) from exc
        return records

    def _validate_sqlite(self) -> None:
        try:
            conn = sqlite3.connect(self.sqlite_path, timeout=30)
            try:
                row = conn.execute("PRAGMA integrity_check").fetchone()
            finally:
                conn.close()
        except sqlite3.Error as exc:
            raise FeedbackStoreCorrupt("SQLite integrity_check failed", path=self.sqlite_path) from exc
        if row is None or row[0] != "ok":
            raise FeedbackStoreCorrupt("SQLite integrity_check failed", path=self.sqlite_path)

    def _rebuild_sqlite(self, records: list[dict[str, Any]]) -> None:
        try:
            conn = self._connect()
            try:
                self._create_schema(conn)
                conn.execute("DELETE FROM signals")
                for record in records:
                    self._insert_signal(conn, self._normalize_signal(record))
                conn.commit()
            finally:
                conn.close()
        except sqlite3.Error as exc:
            raise FeedbackStoreCorrupt("SQLite rebuild failed", path=self.sqlite_path) from exc

    def _insert_signal(self, conn: sqlite3.Connection, record: dict[str, Any]) -> None:
        conn.execute(
            """
            INSERT OR REPLACE INTO signals(
                signal_id, task_id, model_id, task_type, action, timestamp, tier, payload_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record["signal_id"],
                record["task_id"],
                record["model_id"],
                record["task_type"],
                record["action"],
                record["timestamp"],
                self.tier_for_signal(record),
                json.dumps(record, sort_keys=True, separators=(",", ":")),
            ),
        )

    @staticmethod
    def _normalize_signal(signal: dict[str, Any]) -> dict[str, Any]:
        record = dict(signal)
        action = record.get("action")
        if hasattr(action, "value"):
            record["action"] = action.value
        record.setdefault("signal_id", "")
        record.setdefault("task_id", "")
        record.setdefault("model_id", "")
        record.setdefault("task_type", "")
        record.setdefault("edit_diff", None)
        record.setdefault("inspector_score", None)
        record.setdefault("timestamp", datetime.now(timezone.utc).isoformat())
        record.setdefault("metadata", {})
        return record

    def _validate_signal_record(self, record: dict[str, Any]) -> None:
        required = ("signal_id", "task_id", "model_id", "task_type", "action")
        missing = [key for key in required if not str(record.get(key) or "").strip()]
        if missing:
            raise FeedbackStoreCorrupt(
                f"Feedback signal missing required field(s): {', '.join(missing)}",
                path=self.jsonl_path,
            )

    def _parse_record_timestamp(self, record: dict[str, Any]) -> datetime:
        value = record.get("timestamp")
        if not isinstance(value, str) or not value.strip():
            raise FeedbackStoreCorrupt("Feedback signal timestamp missing", path=self.jsonl_path)
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise FeedbackStoreCorrupt("Feedback signal timestamp malformed", path=self.jsonl_path) from exc
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)


def _record_has_exact_subject(value: Any, marker: str) -> bool:
    if isinstance(value, str):
        return value.strip() == marker
    if isinstance(value, Mapping):
        return any(_record_has_exact_subject(item, marker) for item in value.values())
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return any(_record_has_exact_subject(item, marker) for item in value)
    return False


_INSTANCE: FeedbackStore | None = None
_INSTANCE_LOCK = threading.Lock()


def get_feedback_store() -> FeedbackStore:
    """Return the process-wide FeedbackStore singleton.

        Uses double-checked locking so the first construction is serialized
        without paying the lock cost on subsequent reads.

    Returns:
        Resolved feedback store value.
    """
    global _INSTANCE
    if _INSTANCE is None:
        with _INSTANCE_LOCK:
            if _INSTANCE is None:
                _INSTANCE = FeedbackStore()
    return _INSTANCE


def reset_feedback_store_for_test(*, clear_default_store: bool = False) -> None:
    """Reset the singleton for tests."""
    global _INSTANCE
    with _INSTANCE_LOCK:
        _INSTANCE = None
    if clear_default_store:
        default_store_dir = _default_store_dir()
        for path in (default_store_dir / _JSONL_FILENAME, default_store_dir / _SQLITE_FILENAME):
            try:
                path.unlink()
            except FileNotFoundError:
                logger.warning("Handled recoverable failure before fallback.", exc_info=True)
                continue


def purge_feedback_store_retention(*, cutoff_days: int = 30, now: datetime | None = None) -> int:
    """Purge expired records from the process-wide feedback store.

    Args:
        cutoff_days: Maximum age in days to retain.
        now: Optional reference time for deterministic callers.

    Returns:
        Number of feedback signals removed.

    Raises:
        ValueError: If ``cutoff_days`` is negative.
        FeedbackStoreCorrupt: If persisted feedback state is unreadable or
            cannot be rewritten safely.
    """
    return get_feedback_store().purge_expired_signals(cutoff_days=cutoff_days, now=now)


def rotate_feedback_store_cold_tier(*, now: datetime | None = None) -> int:
    """Rotate cold-tier signals out of the process-wide feedback store.

    Args:
        now: Optional reference time for deterministic callers.

    Returns:
        Number of feedback signals removed from the live store.

    Raises:
        FeedbackStoreCorrupt: If persisted feedback state is unreadable or
            cannot be rewritten safely.
    """
    return get_feedback_store().rotate_cold_signals(now=now)


def delete_feedback_signals_for_subject(subject: str) -> int:
    """Delete subject-matching feedback signals from the process-wide store."""
    return get_feedback_store().delete_signals_for_subject(subject)


__all__ = [
    "FeedbackStore",
    "FeedbackStoreCorrupt",
    "delete_feedback_signals_for_subject",
    "get_feedback_store",
    "purge_feedback_store_retention",
    "reset_feedback_store_for_test",
    "rotate_feedback_store_cold_tier",
]
