"""JSONL storage, retention, deletion, and stats helpers for training records."""

from __future__ import annotations

import contextlib
import json
import logging
import shutil
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from vetinari.constants import THREAD_JOIN_TIMEOUT
from vetinari.utils.bounded_collections import BoundedList, bounded_rglob

from . import atomic_writers
from .training_record import TrainingRecord

logger = logging.getLogger(__name__)
_MAX_LOADED_RECORDS = 10000
_MAX_TRACE_SCAN_FILES = 1000
_MAX_TRACE_SCAN_BYTES = 256 * 1024
_MIN_SUBJECT_MARKER_CHARS = 8


class _TrainingCollectorStorageSupport:
    """Storage helper methods for ``TrainingDataCollector``.

    Inheritors provide ``_output_path``, ``_lock``, ``_record_count``,
    ``_queue``, and ``flush()``.
    """

    def _load_all(self) -> list[TrainingRecord]:
        """Load all records from the JSONL file.

        Returns:
            List of TrainingRecord objects; malformed lines are skipped with
            a warning.
        """
        if not self._output_path.exists():
            return []
        records: BoundedList[TrainingRecord] = BoundedList(_MAX_LOADED_RECORDS)
        with self._output_path.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    data = json.loads(line.strip())
                    records.append(TrainingRecord(**data))
                except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                    logger.warning(
                        "Skipping malformed training record in JSONL: %s",
                        line[:100] if isinstance(line, str) else repr(line)[:100],
                    )
                    continue
        return list(records)

    def purge_expired_records(self, cutoff_days: int = 30, *, now: datetime | None = None) -> int:
        """Remove records older than the training-record retention cutoff.

        Returns:
            Number of records removed from the training JSONL store.
        """
        with self._lock:
            return self._purge_expired_records_locked(cutoff_days=cutoff_days, now=now)

    def _purge_expired_records_locked(self, cutoff_days: int = 30, *, now: datetime | None = None) -> int:
        """Remove expired records while the caller already owns ``_lock``."""
        reference = now if now is not None else datetime.now(timezone.utc)
        if reference.tzinfo is None:
            reference = reference.replace(tzinfo=timezone.utc)
        reference = reference.astimezone(timezone.utc)
        cutoff = reference - timedelta(days=max(cutoff_days, 0))

        # Records with absent or unparseable timestamps would otherwise
        # accumulate forever and silently violate the retention contract.
        absent_grace = timedelta(days=max(cutoff_days, 0) * 2)
        absent_cutoff_floor = reference - absent_grace
        store_mtime: datetime | None = None
        if self._output_path.exists():
            try:
                store_mtime = datetime.fromtimestamp(self._output_path.stat().st_mtime, tz=timezone.utc)
            except OSError:
                store_mtime = None

        def keep_record(raw: dict[str, Any]) -> bool:
            """Return True when a raw training record should be retained.

            Returns:
                True when the record should remain in the JSONL store.
            """
            timestamp = str(raw.get("timestamp", ""))
            if not timestamp:
                if store_mtime is None or store_mtime >= absent_cutoff_floor:
                    return True
                logger.warning(
                    "Dropping training record with no timestamp; store mtime %s older than retention grace %s",
                    store_mtime.isoformat(),
                    absent_cutoff_floor.isoformat(),
                )
                return False
            try:
                parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            except ValueError:
                logger.warning(
                    "Dropping training record with malformed timestamp %r during purge "
                    "(cannot compute age; falls outside retention contract)",
                    timestamp,
                )
                return False
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed >= cutoff

        return self._rewrite_records_locked(keep_record)

    def delete_records_for_subject(self, subject: str) -> dict[str, int]:
        """Delete training records and trace artifacts containing a subject marker.

        Returns:
            Counts for deleted training records and trace artifact directories.

        Raises:
            ValueError: If the subject marker is too broad for safe deletion.
        """
        marker = str(subject).strip()
        if not marker:
            return {"records_deleted": 0, "traces_deleted": 0}
        if len(marker) < _MIN_SUBJECT_MARKER_CHARS:
            raise ValueError("subject marker is too broad for training-record deletion")

        records_deleted = self._rewrite_records(lambda raw: marker not in json.dumps(raw, ensure_ascii=False))
        traces_deleted = self._delete_trace_artifacts(marker)
        return {"records_deleted": records_deleted, "traces_deleted": traces_deleted}

    def _rewrite_records(self, keep_record: Any) -> int:
        """Rewrite the JSONL store, keeping only records accepted by a predicate."""
        with self._lock:
            return self._rewrite_records_locked(keep_record)

    def _rewrite_records_locked(self, keep_record: Any) -> int:
        """Rewrite the JSONL store while the caller already owns ``_lock``."""
        tmp_path = self._output_path.with_suffix(self._output_path.suffix + ".tmp")
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError as exc:
                logger.warning("Failed to remove stale rewrite tmp file %s: %s", tmp_path, exc)
        if not self._output_path.exists():
            return 0
        removed = 0
        with self._output_path.open(encoding="utf-8") as handle, tmp_path.open("w", encoding="utf-8") as out:
            for line in handle:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    raw = json.loads(stripped)
                except json.JSONDecodeError:
                    logger.warning("Preserving malformed training record during rewrite: %s", stripped[:100])
                    out.write(line)
                    continue
                if keep_record(raw):
                    out.write(json.dumps(raw, ensure_ascii=False) + "\n")
                else:
                    removed += 1
        tmp_path.replace(self._output_path)
        self._record_count = max(0, self._record_count - removed)
        return removed

    def _trim_to_recent_records_locked(self, max_records: int) -> int:
        """Keep the JSONL sink bounded by dropping oldest rows over max_records."""
        if max_records <= 0 or not self._output_path.exists():
            return 0
        kept = BoundedList[str](max_records)
        total = 0
        with self._output_path.open(encoding="utf-8") as handle:
            for line in handle:
                total += 1
                kept.append(line.rstrip("\n"))
        if total <= max_records:
            return 0
        kept_lines = list(kept)
        atomic_writers._write_text_atomic(self._output_path, "\n".join(kept_lines) + "\n")
        removed = total - len(kept_lines)
        self._record_count = max(0, min(self._record_count, max_records))
        return removed

    def _delete_trace_artifacts(self, marker: str) -> int:
        """Remove trace directories whose serialized files contain a marker."""
        traces_dir = self._output_path.parent / "traces"
        if not traces_dir.is_dir():
            return 0
        removed = 0
        for trace_dir in traces_dir.iterdir():
            if not trace_dir.is_dir():
                continue
            contains_marker = False
            for artifact in bounded_rglob(trace_dir, "*", max_depth=6, max_files=_MAX_TRACE_SCAN_FILES):
                if not artifact.is_file():
                    continue
                with contextlib.suppress(UnicodeDecodeError, OSError):
                    with artifact.open(encoding="utf-8") as handle:
                        content = handle.read(_MAX_TRACE_SCAN_BYTES)
                    if marker in content:
                        contains_marker = True
                        break
            if contains_marker:
                shutil.rmtree(trace_dir)
                removed += 1
        return removed

    def get_stats(self, *, flush: bool = True, flush_timeout: float = THREAD_JOIN_TIMEOUT) -> dict[str, Any]:
        """Return a summary of collected training data.

        Returns:
            Dict with record totals, queue depth, SFT eligibility, average
            score, task-type breakdown, and output path.
        """
        if flush:
            self.flush(timeout=flush_timeout)
        all_records = self._load_all()
        if not all_records:
            return {"total": 0, "total_records": 0, "queued": self._queue.qsize()}

        # all_records is maxlen-bounded by _load_all().
        by_type: dict[str, list[float]] = defaultdict(list)
        for record in all_records:
            by_type[record.task_type].append(record.score)

        return {
            "total": len(all_records),
            "total_records": len(all_records),
            "queued": self._queue.qsize(),
            "sft_eligible": sum(1 for record in all_records if record.score >= 0.8),
            "avg_score": round(sum(record.score for record in all_records) / len(all_records), 3),
            "by_task_type": {
                task_type: {
                    "count": len(scores),
                    "avg_score": round(sum(scores) / len(scores), 3),
                }
                for task_type, scores in by_type.items()
            },
            "output_path": str(self._output_path),
        }

    def count_records(self) -> int:
        """Return the total number of recorded training examples.

        Returns:
            Count of records written to the backing JSONL file.
        """
        stats = self.get_stats(flush=True)
        return int(stats.get("total_records", stats.get("total", 0)) or 0)
