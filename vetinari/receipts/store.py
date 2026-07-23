"""Durable JSONL store for WorkReceipts.

Each receipt is appended to ``outputs/receipts/<project_id>/receipts.jsonl``
in O(1) append mode (open-for-append + flush + fsync). The previous
implementation read the whole file and rewrote it for every append,
which scaled as O(n) and was wasteful for projects accumulating
hundreds or thousands of receipts.

Durability story: the JSON line + trailing newline is written in a
single ``write()`` call followed by an explicit ``flush()`` and
``os.fsync()``. A process crash mid-write may leave a partially
written final line on disk; ``iter_receipts`` is corruption-resistant
and skips malformed lines with a WARNING log so a torn last line
never breaks reads of the surviving records.

On every successful append the store publishes a ``receipt.appended``
event via the global event bus so the Control Center SSE channel can
push the new receipt to attached clients without polling.
"""

from __future__ import annotations

import logging
import os
import re
import threading
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

from vetinari.events import EventBus, get_event_bus
from vetinari.receipts.events import receipt_appended
from vetinari.receipts.record import WorkReceipt
from vetinari.receipts.store_serialization import (
    WORK_RECEIPT_SCHEMA_VERSION,
    _receipt_from_jsonl,
    _receipt_to_jsonl,
)

logger = logging.getLogger(__name__)
DEFAULT_RECEIPT_MAX_BYTES = 10 * 1024 * 1024  # Rotate each project receipt JSONL before 10 MiB.
DEFAULT_RECEIPT_BACKUP_COUNT = 5  # Retain five bounded receipt backups per project.


# Allowlist for project_id components: alphanumeric, underscore, hyphen only.
# Leading/trailing whitespace, path separators, and parent-traversal markers
# are all rejected to prevent filesystem path-traversal attacks.
_PROJECT_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _default_repo_root() -> Path:
    """Resolve the repository root from this file's location."""
    return Path(__file__).resolve().parent.parent.parent


class WorkReceiptStore:
    """Per-project append-only JSONL store for WorkReceipts.

    The store writes to ``<repo_root>/outputs/receipts/<project_id>/receipts.jsonl``
    and publishes a ``receipt.appended`` event on every successful append.

    Args:
        repo_root: Repository root path. Defaults to three parents above
            this source file (i.e. ``vetinari/receipts/store.py`` -> repo
            root). Receipt files are resolved relative to this root.
        event_bus: Event bus instance for publishing append events.
            Defaults to the global singleton from ``get_event_bus()``;
            callers may inject a fresh instance for test isolation.
    """

    def __init__(
        self,
        repo_root: Path | None = None,
        event_bus: EventBus | None = None,
        max_bytes: int = DEFAULT_RECEIPT_MAX_BYTES,
        backup_count: int = DEFAULT_RECEIPT_BACKUP_COUNT,
    ) -> None:
        """Initialise the store.

        Args:
            repo_root: Repository root path. ``None`` resolves to the
                location three parents above this file.
            event_bus: Optional EventBus override; ``None`` uses the
                process-wide singleton.
            max_bytes: Maximum active per-project JSONL size before rotation.
            backup_count: Number of rotated JSONL backups retained per project.

        Raises:
            ValueError: If ``max_bytes`` or ``backup_count`` is not positive.
        """
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        if backup_count <= 0:
            raise ValueError("backup_count must be positive")
        self._repo_root: Path = repo_root if repo_root is not None else _default_repo_root()
        self._event_bus: EventBus = event_bus if event_bus is not None else get_event_bus()
        self._max_bytes = max_bytes
        self._backup_count = backup_count
        self._lock = threading.Lock()

    @property
    def receipts_root(self) -> Path:
        """Directory holding every project's receipts subdirectory.

        Returns ``<repo_root>/outputs/receipts``. Useful for cross-project
        scans (e.g. the Attention API walks every project under this
        root) without resolving a synthetic project id first.
        """
        return self._repo_root / "outputs" / "receipts"

    def receipts_path(self, project_id: str) -> Path:
        r"""Return the absolute path to a project's receipts file.

        Validates that *project_id* contains only alphanumeric characters,
        underscores, and hyphens, then resolves the candidate path and
        asserts it remains inside the receipts root tree.  This two-layer
        check (allowlist regex + canonical-path containment) defends against
        both direct path-separator injection and symlink-based escapes.

        Args:
            project_id: Project identifier; must match ``[A-Za-z0-9_-]+``.

        Returns:
            Absolute ``Path`` to ``outputs/receipts/<project_id>/receipts.jsonl``.

        Raises:
            ValueError: If project_id is empty, contains disallowed characters
                (including ``/``, ``\\``, ``..``, or whitespace), or if the
                resolved path would escape the receipts root.
        """
        if not project_id or not _PROJECT_ID_RE.fullmatch(project_id):
            raise ValueError(
                f"project_id must match [A-Za-z0-9_-]+ (got {project_id!r}); "
                "leading/trailing whitespace, path separators, and parent-traversal "
                "markers are rejected"
            )
        root = self.receipts_root.resolve()
        candidate = (self.receipts_root / project_id / "receipts.jsonl").resolve()
        if not candidate.is_relative_to(root):
            raise ValueError(f"resolved receipts path {candidate} escapes receipts root {root}")
        return candidate

    def append(self, receipt: WorkReceipt) -> None:
        """Append one receipt and publish ``receipt.appended``.

        Strategy: open the per-project JSONL in append mode, write the
        new line + newline in a single ``write()``, then ``flush()`` and
        ``os.fsync()`` so the bytes are durably on disk before the event
        fires. This is O(1) regardless of how many receipts already
        live in the file.

        A process crash mid-write may leave a torn final line; reads
        survive that case because ``iter_receipts`` skips malformed
        lines with a WARNING log.

        After the line is durably persisted, publish a
        ``receipt.appended`` event with payload
        ``{project_id, receipt_id, kind, passed, awaiting_user}``.

        Args:
            receipt: The WorkReceipt to persist.

        Raises:
            TypeError: If *receipt* is not a WorkReceipt instance.
            OSError: If the receipts directory cannot be created or
                written.
        """
        if not isinstance(receipt, WorkReceipt):
            raise TypeError(f"append() expects a WorkReceipt, got {type(receipt).__name__!r}")

        path = self.receipts_path(receipt.project_id)
        path.parent.mkdir(parents=True, exist_ok=True)

        line = _receipt_to_jsonl(receipt) + "\n"

        with self._lock:
            self._rotate_project_log_if_needed(path, len(line.encode("utf-8")))
            with path.open("a", encoding="utf-8") as fh:
                fh.write(line)
                fh.flush()
                os.fsync(fh.fileno())

        logger.debug(
            "Appended receipt %s (kind=%s, project=%s) to %s",
            receipt.receipt_id,
            receipt.kind.value,
            receipt.project_id,
            path,
        )

        self._event_bus.publish(
            receipt_appended(
                project_id=receipt.project_id,
                receipt_id=receipt.receipt_id,
                kind=receipt.kind.value,
                passed=receipt.outcome.passed,
                awaiting_user=receipt.awaiting_user,
            )
        )
        self._event_bus.drain_handlers(timeout=5.0)

    def iter_receipts(self, project_id: str) -> Iterator[WorkReceipt]:
        """Stream a project's receipts in append order, line-by-line.

        Lines that fail to deserialise are skipped with a WARNING log so
        a single corrupted record does not block the rest of the stream.

        Args:
            project_id: Project identifier whose receipts should be
                streamed.

        Yields:
            One WorkReceipt per retained line in append order, including
            rotated backups from oldest to newest.
        """
        path = self.receipts_path(project_id)
        for log_path in self._project_log_paths(path):
            yield from self._iter_receipts_from_path(log_path)

    def find_awaiting(self, project_id: str) -> list[WorkReceipt]:
        """Return only this project's receipts that block on user input.

        Args:
            project_id: Project identifier to filter on.

        Returns:
            Receipts where ``awaiting_user`` is True, in append order.
            Empty list when the project has no receipts file or no
            awaiting receipts.
        """
        return [r for r in self.iter_receipts(project_id) if r.awaiting_user]

    def purge_expired_receipts(
        self,
        project_id: str,
        *,
        cutoff_days: int = 30,
        now: datetime | None = None,
    ) -> int:
        """Physically remove receipts older than the retention cutoff.

        Args:
            project_id: Project identifier whose receipt log should be purged.
            cutoff_days: Maximum age in days to retain. Must be non-negative.
            now: Optional reference time for deterministic tests.

        Returns:
            Number of receipts removed from the project log.

        Raises:
            ValueError: If ``project_id`` is invalid, ``cutoff_days`` is
                negative, or a receipt timestamp is malformed.
            OSError: If the receipt log cannot be rewritten.
        """
        if cutoff_days < 0:
            raise ValueError("cutoff_days must be non-negative")
        path = self.receipts_path(project_id)
        if not path.exists():
            return 0

        reference = now if now is not None else datetime.now(timezone.utc)
        kept: list[WorkReceipt] = []
        removed = 0
        for receipt in self.iter_receipts(project_id):
            timestamp = _parse_receipt_retention_timestamp(receipt)
            age = reference - timestamp
            if age.total_seconds() > cutoff_days * 24 * 60 * 60:
                removed += 1
            else:
                kept.append(receipt)
        if removed == 0:
            return 0

        tmp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        with tmp_path.open("w", encoding="utf-8", newline="\n") as fh:
            for receipt in kept:
                fh.write(_receipt_to_jsonl(receipt) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
        for rotated_path in self._rotated_project_log_paths(path):
            if _path_exists_strict(rotated_path):
                rotated_path.unlink()
        return removed

    def purge_expired_receipts_all_projects(
        self,
        *,
        cutoff_days: int = 30,
        now: datetime | None = None,
    ) -> int:
        """Purge expired receipt rows for every project receipt log."""
        return sum(
            self.purge_expired_receipts(project_id, cutoff_days=cutoff_days, now=now)
            for project_id in self._project_ids()
        )

    def delete_receipts_for_subject(self, subject: str) -> int:
        """Physically remove receipts containing a subject marker.

        Args:
            subject: Exact subject marker to erase from persisted receipts.

        Returns:
            Number of receipt rows removed across all project receipt logs.
        """
        marker = subject.strip()
        if not marker:
            return 0
        return sum(
            self._delete_receipts_for_subject_in_project(project_id, marker) for project_id in self._project_ids()
        )

    def _delete_receipts_for_subject_in_project(self, project_id: str, marker: str) -> int:
        path = self.receipts_path(project_id)
        if not path.exists():
            return 0
        receipts = list(self.iter_receipts(project_id))
        kept = [receipt for receipt in receipts if marker not in _receipt_to_jsonl(receipt)]
        removed = len(receipts) - len(kept)
        if removed == 0:
            return 0
        tmp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        with tmp_path.open("w", encoding="utf-8", newline="\n") as fh:
            for receipt in kept:
                fh.write(_receipt_to_jsonl(receipt) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
        for rotated_path in self._rotated_project_log_paths(path):
            if _path_exists_strict(rotated_path):
                rotated_path.unlink()
        return removed

    def _project_ids(self) -> list[str]:
        root = self.receipts_root
        if not root.exists():
            return []
        return sorted(
            candidate.name
            for candidate in root.iterdir()
            if candidate.is_dir() and _PROJECT_ID_RE.fullmatch(candidate.name)
        )

    def _rotate_project_log_if_needed(self, path: Path, incoming_bytes: int) -> None:
        """Rotate a per-project receipt JSONL before the append exceeds its cap."""
        try:
            current_size = path.stat().st_size
        except FileNotFoundError:
            logger.warning("Exception handled by  rotate project log if needed fallback", exc_info=True)
            return
        if current_size + incoming_bytes <= self._max_bytes:
            return
        oldest = path.with_name(f"{path.name}.{self._backup_count}")
        if _path_exists_strict(oldest):
            oldest.unlink()
        for index in range(self._backup_count - 1, 0, -1):
            source = path.with_name(f"{path.name}.{index}")
            if _path_exists_strict(source):
                source.replace(path.with_name(f"{path.name}.{index + 1}"))
        path.replace(path.with_name(f"{path.name}.1"))

    def _project_log_paths(self, path: Path) -> list[Path]:
        """Return receipt ledgers from oldest retained backup to active JSONL."""
        paths = list(reversed(self._rotated_project_log_paths(path)))
        paths.append(path)
        return [candidate for candidate in paths if _path_exists_strict(candidate)]

    def _rotated_project_log_paths(self, path: Path) -> list[Path]:
        """Return retained rotated receipt paths from newest to oldest."""
        return [path.with_name(f"{path.name}.{index}") for index in range(1, self._backup_count + 1)]

    @staticmethod
    def _iter_receipts_from_path(path: Path) -> Iterator[WorkReceipt]:
        """Stream receipts from one JSONL path, skipping malformed rows explicitly."""
        with path.open("r", encoding="utf-8") as fh:
            for lineno, raw in enumerate(fh, start=1):
                stripped = raw.strip()
                if not stripped:
                    continue
                try:
                    yield _receipt_from_jsonl(stripped)
                except (ValueError, KeyError, TypeError) as exc:
                    logger.warning(
                        "Skipping malformed receipts line %d in %s — %s",
                        lineno,
                        path,
                        exc,
                    )
                    continue


def _parse_receipt_retention_timestamp(receipt: WorkReceipt) -> datetime:
    value = receipt.finished_at_utc or receipt.started_at_utc
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"receipt {receipt.receipt_id!r} has malformed retention timestamp") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _path_exists_strict(path: Path) -> bool:
    """Return whether a path exists without hiding unreadable state."""
    try:
        path.stat()
    except FileNotFoundError:
        logger.warning("Exception handled by  path exists strict fallback", exc_info=True)
        return False
    return True


def purge_project_receipts_retention(
    project_id: str,
    *,
    cutoff_days: int = 30,
    now: datetime | None = None,
) -> int:
    """Purge expired receipt rows from the default receipt store.

    Args:
        project_id: Project identifier whose receipt log should be purged.
        cutoff_days: Maximum age in days to retain.
        now: Optional reference time for deterministic callers.

    Returns:
        Number of receipts removed.

    Raises:
        ValueError: If ``project_id`` is invalid, ``cutoff_days`` is negative,
            or a receipt timestamp is malformed.
        OSError: If the receipt log cannot be rewritten.
    """
    return WorkReceiptStore().purge_expired_receipts(project_id, cutoff_days=cutoff_days, now=now)


def purge_all_project_receipts_retention(
    *,
    cutoff_days: int = 30,
    now: datetime | None = None,
) -> int:
    """Purge expired receipt rows from every project in the default receipt store."""
    return WorkReceiptStore().purge_expired_receipts_all_projects(cutoff_days=cutoff_days, now=now)


def delete_receipts_for_subject(subject: str) -> int:
    """Delete subject-matching receipts from the default receipt store."""
    return WorkReceiptStore().delete_receipts_for_subject(subject)


__all__ = [
    "WORK_RECEIPT_SCHEMA_VERSION",
    "WorkReceiptStore",
    "delete_receipts_for_subject",
    "purge_all_project_receipts_retention",
    "purge_project_receipts_retention",
]
