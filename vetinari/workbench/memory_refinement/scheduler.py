"""Quiet-window and resource-aware refinement scheduler."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from .contracts import NO_REVERSAL, RefinementEventKind, RefinementJournalEntry, RefinementWindow


class RefinementScheduler:
    """Runtime contract for RefinementScheduler."""

    def __init__(
        self,
        journal: Any,
        decay_fn: Callable[[], tuple[RefinementJournalEntry, ...]],
        duplicate_fn: Callable[[], tuple[RefinementJournalEntry, ...]],
        relationship_fn: Callable[[], tuple[RefinementJournalEntry, ...]],
        *,
        quiet_window: RefinementWindow | None = None,
        resource_busy_check: Callable[[], bool] | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self.journal = journal
        self.decay_fn = decay_fn
        self.duplicate_fn = duplicate_fn
        self.relationship_fn = relationship_fn
        self.quiet_window = quiet_window
        self.resource_busy_check = resource_busy_check
        self.clock = clock

    def run_cycle(self) -> tuple[RefinementJournalEntry, ...]:
        """Execute the run cycle operation.

        Returns:
            Outcome produced by run_cycle().
        """
        now = self.clock().astimezone(timezone.utc)
        if self.quiet_window and self.quiet_window.start_at <= now < self.quiet_window.end_at:
            return self._record_skip("quiet-window-skip", RefinementEventKind.QUIET_WINDOW_SKIP, now)
        if self.resource_busy_check and self.resource_busy_check():
            return self._record_skip("resource-busy", RefinementEventKind.RESOURCE_BUSY_DEFERRAL, now)
        entries = tuple(self.decay_fn()) + tuple(self.duplicate_fn()) + tuple(self.relationship_fn())
        for entry in entries:
            self.journal.record(entry)
        return entries

    def _record_skip(self, reason: str, kind: RefinementEventKind, now: datetime) -> tuple[RefinementJournalEntry, ...]:
        entry = RefinementJournalEntry(
            event_id=f"{kind.value}-{int(now.timestamp())}",
            kind=kind,
            before_ref="scheduler",
            after_ref="",
            reasons=(reason,),
            evidence_refs=("scheduler",),
            decided_at=now,
            reversal_token=NO_REVERSAL,
            actor="memory-refinement-scheduler",
        )
        self.journal.record(entry)
        return (entry,)


__all__ = ["RefinementScheduler"]
