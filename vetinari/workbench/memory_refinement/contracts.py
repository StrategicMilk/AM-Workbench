"""Immutable contracts for memory refinement journal events."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class MemoryRefinementError(ValueError):
    """Raised when a refinement journal entry is malformed."""


class RefinementEventKind(str, Enum):
    """Runtime contract for RefinementEventKind."""

    CONSOLIDATION = "consolidation"
    DUPLICATE_MERGE = "duplicate_merge"
    CONFIDENCE_DECAY = "confidence_decay"
    RELATIONSHIP_INFERRED = "relationship_inferred"
    REJECTION = "rejection"
    QUIET_WINDOW_SKIP = "quiet_window_skip"
    RESOURCE_BUSY_DEFERRAL = "resource_busy_deferral"


NO_REVERSAL = ""


@dataclass(frozen=True, slots=True)
class RefinementJournalEntry:
    """Runtime contract for RefinementJournalEntry."""

    event_id: str
    kind: RefinementEventKind
    before_ref: str
    after_ref: str
    reasons: tuple[str, ...]
    evidence_refs: tuple[str, ...]
    decided_at: datetime
    reversal_token: str
    actor: str

    def __post_init__(self) -> None:
        for name in ("event_id", "before_ref", "actor"):
            _non_empty(getattr(self, name), name)
        object.__setattr__(self, "kind", RefinementEventKind(self.kind))
        object.__setattr__(self, "reasons", _strings(self.reasons, "reasons"))
        object.__setattr__(self, "evidence_refs", _strings(self.evidence_refs, "evidence_refs"))
        decided = self.decided_at
        if isinstance(decided, str):
            decided = datetime.fromisoformat(decided.replace("Z", "+00:00"))
        if decided.tzinfo is None:
            raise MemoryRefinementError("decided_at must be timezone-aware")
        object.__setattr__(self, "decided_at", decided.astimezone(timezone.utc))

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "kind": self.kind.value,
            "before_ref": self.before_ref,
            "after_ref": self.after_ref,
            "reasons": list(self.reasons),
            "evidence_refs": list(self.evidence_refs),
            "decided_at": self.decided_at.isoformat(),
            "reversal_token": self.reversal_token,
            "actor": self.actor,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> RefinementJournalEntry:
        return cls(
            event_id=str(payload["event_id"]),
            kind=RefinementEventKind(str(payload["kind"])),
            before_ref=str(payload["before_ref"]),
            after_ref=str(payload.get("after_ref", "")),
            reasons=tuple(str(value) for value in payload["reasons"]),
            evidence_refs=tuple(str(value) for value in payload["evidence_refs"]),
            decided_at=datetime.fromisoformat(str(payload["decided_at"]).replace("Z", "+00:00")),
            reversal_token=str(payload.get("reversal_token", "")),
            actor=str(payload["actor"]),
        )

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"RefinementJournalEntry(event_id={self.event_id!r}, kind={self.kind!r}, before_ref={self.before_ref!r})"


@dataclass(frozen=True, slots=True)
class RefinementWindow:
    """Runtime contract for RefinementWindow."""

    start_at: datetime
    end_at: datetime

    def __post_init__(self) -> None:
        start = _utc(self.start_at, "start_at")
        end = _utc(self.end_at, "end_at")
        if start >= end:
            raise MemoryRefinementError("start_at must be before end_at")
        object.__setattr__(self, "start_at", start)
        object.__setattr__(self, "end_at", end)


@dataclass(frozen=True, slots=True)
class RefinementJournalSnapshot:
    """Runtime contract for RefinementJournalSnapshot."""

    entries: tuple[RefinementJournalEntry, ...]
    total_entries: int

    def to_dict(self) -> dict[str, Any]:
        return {"entries": [entry.to_dict() for entry in self.entries], "total_entries": self.total_entries}


def _utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None:
        raise MemoryRefinementError(f"{field_name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _strings(values: Sequence[Any], field_name: str) -> tuple[str, ...]:
    result = tuple(str(value) for value in values if str(value).strip())
    if not result:
        raise MemoryRefinementError(f"{field_name} must contain a non-empty value")
    return result


def _non_empty(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise MemoryRefinementError(f"{field_name} must be non-empty")


__all__ = [
    "MemoryRefinementError",
    "RefinementEventKind",
    "RefinementJournalEntry",
    "RefinementJournalSnapshot",
    "RefinementWindow",
]
