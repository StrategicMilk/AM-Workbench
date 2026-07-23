"""Relationship inference proposal engine."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Any

from vetinari.workbench.knowledge_graph import RelationKind

from .contracts import NO_REVERSAL, RefinementEventKind, RefinementJournalEntry


def propose_relationships(records: Sequence[Any], snapshot: Any) -> tuple[RefinementJournalEntry, ...]:
    """Execute the propose relationships operation.

    Args:
        records: Typed record consumed by the operation.
        snapshot: Snapshot value consumed by propose_relationships().

    Returns:
        tuple[RefinementJournalEntry, ...] value produced by propose_relationships().
    """
    proposals = []
    for record in records:
        memory_id = str(getattr(record, "memory_id", getattr(record, "episode_id", "")))
        if not memory_id:
            continue
        grounding = snapshot.retrieve(memory_id, limit=1)
        proposals.extend(
            (
                RefinementJournalEntry(
                    event_id=f"relationship-{memory_id}-{entity_id}",
                    kind=RefinementEventKind.RELATIONSHIP_INFERRED,
                    before_ref=memory_id,
                    after_ref=entity_id,
                    reasons=(RelationKind.SEMANTICALLY_RELATED.value,),
                    evidence_refs=tuple(grounding.entity_ids or (memory_id,)),
                    decided_at=datetime.now(timezone.utc),
                    reversal_token=NO_REVERSAL,
                    actor="memory-refinement-relationships",
                )
            )
            for entity_id in grounding.entity_ids
        )
    return tuple(proposals)


__all__ = ["propose_relationships"]
