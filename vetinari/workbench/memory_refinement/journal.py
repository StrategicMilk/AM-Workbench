"""Append-only memory refinement journal."""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

from vetinari.workbench.knowledge_vault import VaultSchemaValidator
from vetinari.workbench.spine_consumers import record_trace_written

from .contracts import RefinementEventKind, RefinementJournalEntry, RefinementJournalSnapshot, RefinementWindow


class MemoryRefinementJournalError(ValueError):
    """Raised when the journal cannot safely append or read an entry."""


class MemoryRefinementJournal:
    """Single-writer-with-lock JSONL refinement journal."""

    def __init__(
        self, path: Path, lock: threading.Lock | None = None, schema_validator: VaultSchemaValidator | None = None
    ) -> None:
        self.path = Path(path)
        self._lock = lock or threading.Lock()
        self._schema_validator = schema_validator or VaultSchemaValidator()

    def record(self, entry: RefinementJournalEntry) -> None:
        """Execute the record operation.

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        try:
            payload = entry.to_dict()
            self._schema_validator.validate(payload)
        except Exception as exc:
            raise MemoryRefinementJournalError(str(exc)) from exc
        body = json.dumps(payload, sort_keys=True) + "\n"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, self.path.open("a", encoding="utf-8") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        # spine_consumers invokes get_spine() and absorbs observability failures.
        record_trace_written(
            trace_id=entry.event_id,
            query_hash="memory_refinement",
            project_id="default",
        )

    def read_window(self, window: RefinementWindow) -> RefinementJournalSnapshot:
        """Execute the read window operation.

        Returns:
            Resolved window value.
        """
        if not self.path.exists():
            return RefinementJournalSnapshot(entries=(), total_entries=0)
        entries = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            entry = RefinementJournalEntry.from_dict(json.loads(line))
            if window.start_at <= entry.decided_at < window.end_at:
                entries.append(entry)
        return RefinementJournalSnapshot(entries=tuple(entries), total_entries=len(entries))

    def reverse(self, event_id: str, *, reason: str) -> RefinementJournalEntry:
        """Execute the reverse operation.

        Returns:
            RefinementJournalEntry value produced by reverse().

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        if not reason.strip():
            raise MemoryRefinementJournalError("reason-required")
        window = RefinementWindow(datetime(1970, 1, 1, tzinfo=timezone.utc), datetime(9999, 1, 1, tzinfo=timezone.utc))
        for entry in self.read_window(window).entries:
            if entry.event_id == event_id:
                reversal = RefinementJournalEntry(
                    event_id=f"reverse-{event_id}",
                    kind=RefinementEventKind.REJECTION,
                    before_ref=entry.after_ref or entry.before_ref,
                    after_ref="",
                    reasons=(reason,),
                    evidence_refs=(event_id,),
                    decided_at=datetime.now(timezone.utc),
                    reversal_token=event_id,
                    actor="memory-refinement-journal",
                )
                self.record(reversal)
                return reversal
        raise MemoryRefinementJournalError("event-not-found")


__all__ = ["MemoryRefinementJournal", "MemoryRefinementJournalError"]
