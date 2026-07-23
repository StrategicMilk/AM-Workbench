"""Confidence decay proposal engine for governed memory records."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Any

from vetinari.workbench.knowledge_vault import compute_decayed_confidence

from .contracts import NO_REVERSAL, RefinementEventKind, RefinementJournalEntry


def apply_decay(
    records: Sequence[Any], *, half_life_days: float, floor: float, now: datetime
) -> tuple[RefinementJournalEntry, ...]:
    """Execute the apply decay operation.

    Returns:
        tuple[RefinementJournalEntry, ...] value produced by apply_decay().
    """
    entries = []
    now_utc = now.astimezone(timezone.utc)
    for record in records:
        confidence = float(getattr(record, "confidence", getattr(record, "importance", 0.5)))
        created = getattr(record, "created_at_utc", now_utc.isoformat())
        created_at = datetime.fromisoformat(str(created).replace("Z", "+00:00"))
        age_days = max(0.0, (now_utc - created_at.astimezone(timezone.utc)).total_seconds() / 86400)
        decayed = compute_decayed_confidence(confidence, age_days, half_life_days, floor)
        kind = (
            RefinementEventKind.REJECTION
            if decayed <= floor and confidence > floor
            else RefinementEventKind.CONFIDENCE_DECAY
        )
        if decayed < confidence or kind is RefinementEventKind.REJECTION:
            memory_id = str(getattr(record, "memory_id", getattr(record, "episode_id", "memory")))
            entries.append(
                RefinementJournalEntry(
                    event_id=f"decay-{memory_id}",
                    kind=kind,
                    before_ref=memory_id,
                    after_ref=f"{decayed:.4f}",
                    reasons=("confidence-decay",),
                    evidence_refs=(memory_id,),
                    decided_at=now_utc,
                    reversal_token=NO_REVERSAL,
                    actor="memory-refinement-decay",
                )
            )
    return tuple(entries)


__all__ = ["apply_decay"]
