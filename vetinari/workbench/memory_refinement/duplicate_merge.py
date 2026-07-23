"""Duplicate-merge proposal engine."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import datetime, timezone
from itertools import pairwise
from typing import Any

from .contracts import NO_REVERSAL, RefinementEventKind, RefinementJournalEntry

logger = logging.getLogger(__name__)


def propose_duplicate_merges(
    records: Sequence[Any], *, similarity_threshold: float, snapshot: Any | None = None
) -> tuple[RefinementJournalEntry, ...]:
    """Execute the propose duplicate merges operation.

    Returns:
        tuple[RefinementJournalEntry, ...] value produced by propose_duplicate_merges().
    """
    if snapshot is None:
        return (
            RefinementJournalEntry(
                event_id="duplicate-merge-deferral",
                kind=RefinementEventKind.RESOURCE_BUSY_DEFERRAL,
                before_ref="semantic-layer",
                after_ref="",
                reasons=("semantic-snapshot-unavailable",),
                evidence_refs=("semantic-layer",),
                decided_at=datetime.now(timezone.utc),
                reversal_token=NO_REVERSAL,
                actor="memory-refinement-duplicates",
            ),
        )
    proposals = []
    rows = list(records)
    for left, right in pairwise(rows):
        left_id = str(getattr(left, "memory_id", getattr(left, "episode_id", "")))
        right_id = str(getattr(right, "memory_id", getattr(right, "episode_id", "")))
        similarity = _semantic_similarity(snapshot, left_id, right_id)
        if left_id and right_id and similarity is not None and similarity >= similarity_threshold:
            proposals.append(
                RefinementJournalEntry(
                    event_id=f"merge-{right_id}-into-{left_id}",
                    kind=RefinementEventKind.DUPLICATE_MERGE,
                    before_ref=right_id,
                    after_ref=left_id,
                    reasons=(f"semantic-similarity={similarity:.3f}", "similarity-threshold-met"),
                    evidence_refs=(f"semantic:{left_id}:{right_id}", left_id, right_id),
                    decided_at=datetime.now(timezone.utc),
                    reversal_token=NO_REVERSAL,
                    actor="memory-refinement-duplicates",
                )
            )
    return tuple(proposals)


def _semantic_similarity(snapshot: Any, left_id: str, right_id: str) -> float | None:
    if not left_id or not right_id:
        return None
    if hasattr(snapshot, "similarity"):
        value = snapshot.similarity(left_id, right_id)
    elif isinstance(snapshot, dict):
        value = snapshot.get((left_id, right_id), snapshot.get((right_id, left_id)))
    else:
        value = getattr(snapshot, "scores", {}).get((left_id, right_id), None)
    try:
        score = float(value)
    except (TypeError, ValueError):
        logger.warning(
            "Ignoring malformed semantic similarity score.",
            extra={"left_id": left_id, "right_id": right_id},
            exc_info=True,
        )
        return None
    if not 0.0 <= score <= 1.0:
        return None
    return score


__all__ = ["propose_duplicate_merges"]
