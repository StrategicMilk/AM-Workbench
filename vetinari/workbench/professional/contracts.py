"""Contracts for promoting professional-mode workflow outcomes."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum

from vetinari.workbench.rigor import RigorLevel


class PromotedArtifactKind(str, Enum):
    """Runtime contract for PromotedArtifactKind."""

    CHECKLIST = "checklist"
    DOCUMENT_PACKET = "document_packet"
    PROFESSIONAL_MEMO = "professional_memo"
    SOURCE_BACKED_NOTE = "source_backed_note"
    REMINDER = "reminder"
    EVIDENCE_NOTEBOOK_ENTRY = "evidence_notebook_entry"
    MEETING_PREP_BRIEF = "meeting_prep_brief"


@dataclass(frozen=True, slots=True)
class PromotedArtifactRecord:
    """Typed artifact promotion record with enough provenance to audit later."""

    artifact_id: str
    artifact_kind: PromotedArtifactKind | str
    project_id: str
    created_at_utc: str
    provenance: tuple[tuple[str, str], ...]
    source_card_ids: tuple[str, ...]
    tool_card_ids: tuple[str, ...]
    claim_promotion_decision_ref: str
    mode_lens_id: str
    rigor_level: RigorLevel | str
    authority_ref: str
    evidence_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_non_empty(self.artifact_id, "artifact_id")
        _require_non_empty(self.project_id, "project_id")
        _parse_utc(self.created_at_utc, "created_at_utc")
        object.__setattr__(self, "artifact_kind", PromotedArtifactKind(self.artifact_kind))
        object.__setattr__(self, "rigor_level", RigorLevel(self.rigor_level))
        object.__setattr__(self, "source_card_ids", _string_tuple(self.source_card_ids))
        object.__setattr__(self, "tool_card_ids", _string_tuple(self.tool_card_ids))
        object.__setattr__(self, "evidence_refs", _string_tuple(self.evidence_refs))
        if not self.provenance:
            raise ValueError("provenance must be non-empty")
        for key, value in self.provenance:
            _require_non_empty(key, "provenance key")
            _require_non_empty(value, "provenance value")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"PromotedArtifactRecord(artifact_id={self.artifact_id!r}, artifact_kind={self.artifact_kind!r}, project_id={self.project_id!r})"


def _parse_utc(value: str, field_name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field_name} must be ISO-8601 UTC") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field_name} must include UTC timezone")
    return parsed.astimezone(timezone.utc)


def _require_non_empty(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be non-empty")


def _string_tuple(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,) if value.strip() else ()
    if isinstance(value, tuple):
        return tuple(str(item) for item in value if str(item).strip())
    if isinstance(value, list):
        return tuple(str(item) for item in value if str(item).strip())
    return (str(value),)


__all__ = [
    "PromotedArtifactKind",
    "PromotedArtifactRecord",
]
