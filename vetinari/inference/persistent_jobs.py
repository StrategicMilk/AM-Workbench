"""SQLite-backed durable inference job queue."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from vetinari.constants import OUTPUTS_DIR
from vetinari.security.redaction import redact_text, redact_value

DEVELOPER_WORKFLOW_CONTRACT_ID = "wave3-rcg0014p06-scope-followup-P09"
PERSISTENT_JOBS_WORKFLOW_GUARDS: tuple[str, ...] = (
    "SQLite integrity failure raises before queue use",
    "enqueue stores only redacted payload JSON durably",
    "dequeue marks one pending row running under the DB lock",
    "stale running jobs are reclaimed only through explicit recovery",
)


def developer_workflow_contract() -> dict[str, object]:
    """Return persistent-job workflow guarantees verified by this follow-up pack."""
    return {
        "pack": DEVELOPER_WORKFLOW_CONTRACT_ID,
        "surface": "vetinari/inference/persistent_jobs.py",
        "guards": PERSISTENT_JOBS_WORKFLOW_GUARDS,
    }


class PersistentJobsCorruptError(RuntimeError):
    """Raised when SQLite integrity checks fail."""

    def __init__(self, db_path: str | Path, integrity_result: str) -> None:
        super().__init__(f"persistent jobs DB is corrupt: {db_path} ({integrity_result})")
        self.db_path = Path(db_path)
        self.integrity_result = integrity_result


class PersistentJobsPermissionError(RuntimeError):
    """Raised when the configured DB parent is not writable."""

    def __init__(self, db_path: str | Path) -> None:
        super().__init__(f"persistent jobs DB parent is not writable: {Path(db_path).parent}")
        self.db_path = Path(db_path)


# Side effects:
#   - _queue_instance: PersistentJobQueue | None is a module-level singleton.
#   - _queue_lock: threading.Lock guards singleton init.
_queue_instance: PersistentJobQueue | None = None
_queue_lock = threading.Lock()


class PersistentJobQueue:
    """Durable SQLite queue for event-driven inference jobs."""

    def __init__(self, db_path: str) -> None:
        self.db_path = Path(db_path)
        parent = self.db_path.parent
        parent.mkdir(parents=True, exist_ok=True)
        if not os.access(parent, os.W_OK):
            raise PersistentJobsPermissionError(self.db_path)
        self._db_lock = threading.Lock()
        try:
            self._conn = sqlite3.connect(self.db_path, check_same_thread=False, isolation_level=None)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            result = str(self._conn.execute("PRAGMA integrity_check").fetchone()[0])
        except sqlite3.DatabaseError as exc:
            raise PersistentJobsCorruptError(self.db_path, str(exc)) from exc
        if result != "ok":
            raise PersistentJobsCorruptError(self.db_path, result)
        self._volatile_payloads: dict[str, dict[str, Any]] = {}
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                job_id TEXT PRIMARY KEY,
                capability TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                result_json TEXT
            )
            """
        )
        # One-shot recovery: rescue jobs left in 'running' from a prior process
        # crash. This MUST run only at __init__ time, not on every dequeue() —
        # mid-life recovery races concurrent workers and double-assigns jobs.
        self._conn.execute(
            "UPDATE jobs SET status='pending', updated_at=? WHERE status='running'",
            (time.time(),),
        )

    def enqueue(self, job_id: str, capability: str, payload: dict[str, Any]) -> None:
        """Insert a pending job.

        Args:
            job_id: Job id value consumed by enqueue().
            capability: Capability value consumed by enqueue().
            payload: Payload data validated or transformed by the operation.

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        now = time.time()
        with self._db_lock, self._transaction():
            try:
                self._volatile_payloads[job_id] = dict(payload)
                self._conn.execute(
                    """
                    INSERT INTO jobs (job_id, capability, payload_json, status, created_at, updated_at, result_json)
                    VALUES (?, ?, ?, 'pending', ?, ?, NULL)
                    """,
                    (job_id, capability, json.dumps(redact_value(payload), sort_keys=True), now, now),
                )
            except sqlite3.IntegrityError as exc:
                self._volatile_payloads.pop(job_id, None)
                raise ValueError(f"job_id already exists: {job_id}") from exc

    def dequeue(self) -> dict[str, Any] | None:
        """Return the oldest pending job and mark it running.

                Stuck-job recovery (running -> pending) is intentionally NOT performed
                here. It is a one-shot operation in __init__; running it on every
                dequeue races concurrent workers and double-assigns jobs.

        Returns:
            dict[str, Any] | None value produced by dequeue().
        """
        with self._db_lock, self._transaction():
            now = time.time()
            row = self._conn.execute("SELECT * FROM jobs WHERE status='pending' ORDER BY created_at LIMIT 1").fetchone()
            if row is None:
                return None
            self._conn.execute(
                "UPDATE jobs SET status='running', updated_at=? WHERE job_id=?",
                (now, row["job_id"]),
            )
            updated = dict(row)
            updated["status"] = "running"
            updated["payload"] = self._volatile_payloads.get(updated["job_id"], json.loads(updated["payload_json"]))
            return updated

    def complete(self, job_id: str, result: dict[str, Any]) -> None:
        """Mark a running job done and store its result.

        Args:
            job_id: Job id value consumed by complete().
            result: Result value consumed by complete().
        """
        with self._db_lock, self._transaction():
            self._conn.execute(
                "UPDATE jobs SET status='done', result_json=?, updated_at=? WHERE job_id=? AND status='running'",
                (json.dumps(redact_value(result), sort_keys=True), time.time(), job_id),
            )
            self._volatile_payloads.pop(job_id, None)

    def fail(self, job_id: str, reason: str) -> None:
        """Mark a running job failed and store the reason.

        Args:
            job_id: Job id value consumed by fail().
            reason: Reason value consumed by fail().
        """
        with self._db_lock, self._transaction():
            self._conn.execute(
                "UPDATE jobs SET status='failed', result_json=?, updated_at=? WHERE job_id=? AND status='running'",
                (json.dumps({"reason": redact_text(reason)}, sort_keys=True), time.time(), job_id),
            )
            self._volatile_payloads.pop(job_id, None)

    def heartbeat(self, job_id: str) -> bool:
        """Refresh updated_at for a running job so the watchdog does not reclaim it.

                Workers MUST call this periodically while a job is in flight. The
                recommended cadence is below half the configured ``claim_timeout_s``.
                Returns True if the row was updated (still running and owned), False
                otherwise — callers can use the False return as a signal that the job
                was reclaimed by the watchdog.

        Returns:
            bool value produced by heartbeat().
        """
        with self._db_lock, self._transaction():
            cursor = self._conn.execute(
                "UPDATE jobs SET updated_at=? WHERE job_id=? AND status='running'",
                (time.time(), job_id),
            )
            return cursor.rowcount > 0

    def recover_stuck_jobs(self, claim_timeout_s: float) -> int:
        """Re-flag jobs whose heartbeat is older than ``claim_timeout_s`` as pending.

        This is the mid-life counterpart to the one-shot recovery in __init__:
        a worker that crashed mid-job leaves the row in ``running`` forever.
        A scheduler thread (or a cron-style call from the consumer) MUST call
        this periodically to make stale ``running`` rows available again.

        Args:
            claim_timeout_s: A row whose ``updated_at`` is more than this many
                seconds in the past becomes a recovery candidate. Pick a value
                comfortably larger than the maximum legitimate per-job runtime
                + heartbeat cadence so live workers are never reclaimed.

        Returns:
            Number of rows reclaimed. Useful for telemetry and logging.
        """
        threshold = time.time() - max(0.0, claim_timeout_s)
        with self._db_lock, self._transaction():
            cursor = self._conn.execute(
                "UPDATE jobs SET status='pending', updated_at=? WHERE status='running' AND updated_at < ?",
                (time.time(), threshold),
            )
            return cursor.rowcount

    def _commit(self) -> None:
        self._conn.commit()

    def _transaction(self) -> _Transaction:
        return _Transaction(self._conn, self._commit)


class _Transaction:
    def __init__(self, conn: sqlite3.Connection, commit) -> None:
        self._conn = conn
        self._commit = commit

    def __enter__(self) -> None:
        self._conn.execute("BEGIN")

    def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
        if exc_type is not None:
            self._conn.rollback()
            return False
        try:
            self._commit()
        except Exception:
            self._conn.rollback()
            raise
        return False


def get_persistent_job_queue(config: dict) -> PersistentJobQueue:
    """Return the process singleton job queue.

    Returns:
        Resolved persistent job queue value.
    """
    global _queue_instance
    if _queue_instance is not None:
        return _queue_instance
    with _queue_lock:
        if _queue_instance is None:
            db_path = config.get("persistent_jobs_db") or str(OUTPUTS_DIR / "inference_jobs.db")
            _queue_instance = PersistentJobQueue(str(db_path))
    return _queue_instance


def reset_persistent_job_queue() -> None:
    """Reset the process singleton so tests can isolate durable queue state."""
    global _queue_instance
    with _queue_lock:
        if _queue_instance is not None:
            _queue_instance._conn.close()
        _queue_instance = None


__all__ = [
    "PersistentJobQueue",
    "PersistentJobsCorruptError",
    "PersistentJobsPermissionError",
    "developer_workflow_contract",
    "get_persistent_job_queue",
    "reset_persistent_job_queue",
]
