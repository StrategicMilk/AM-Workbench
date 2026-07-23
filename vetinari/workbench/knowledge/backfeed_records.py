"""Record contracts and helpers for governed knowledge backfeed."""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from math import isfinite
from typing import Any, Self

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1

SOURCE_KINDS = {
    "agent_discovery",
    "user_correction",
    "eval_failure",
    "project_summary",
    "mobile_note",
    "external_fact",
}
CHANGE_KINDS = {"entity", "relationship"}
CONSUMER_KINDS = {"trusted_context", "export", "eval"}
DECISION_KINDS = {"approved", "rejected", "superseded"}


class KnowledgeBackfeedError(ValueError):
    """Raised when backfeed governance would be incomplete or unsafe."""

    def __init__(self, reason: str, *, proposal_id: str | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.proposal_id = proposal_id

    def __str__(self) -> str:
        if self.proposal_id is None:
            return f"KnowledgeBackfeedError: {self.reason}"
        return f"KnowledgeBackfeedError: {self.reason} (proposal_id={self.proposal_id})"


class BackfeedStatus(str, Enum):
    """Proposal lifecycle states visible in audit and consumer payloads."""

    PROPOSED = "proposed"
    APPROVED = "approved"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"


@dataclass(frozen=True, slots=True)
class BackfeedSource:
    """Evidence source and confidence for one backfeed proposal."""

    kind: str
    source_id: str
    evidence_ref: str
    confidence: float | int | str
    observed_at_utc: str

    def __post_init__(self) -> None:
        if self.kind not in SOURCE_KINDS:
            raise KnowledgeBackfeedError("source_kind_unknown")
        _require_text(self.source_id, "source_id")
        _require_text(self.evidence_ref, "evidence_ref")
        confidence = _coerce_confidence(self.confidence)
        if confidence is None:
            raise KnowledgeBackfeedError("source_confidence_required")
        object.__setattr__(self, "confidence", confidence)
        _require_utc(self.observed_at_utc, "observed_at_utc")

    def to_payload(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "source_id": self.source_id,
            "evidence_ref": self.evidence_ref,
            "confidence": self.confidence,
            "observed_at_utc": self.observed_at_utc,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> Self:
        return cls(
            kind=_require_payload_str(payload, "kind"),
            source_id=_require_payload_str(payload, "source_id"),
            evidence_ref=_require_payload_str(payload, "evidence_ref"),
            confidence=payload.get("confidence"),
            observed_at_utc=_require_payload_str(payload, "observed_at_utc"),
        )

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"BackfeedSource(kind={self.kind!r}, source_id={self.source_id!r}, evidence_ref={self.evidence_ref!r})"


@dataclass(frozen=True, slots=True)
class BackfeedScope:
    """Explicit scope and consumer permissions for a proposal."""

    scope_id: str
    boundary: str
    allowed_consumers: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_text(self.scope_id, "scope_id")
        _require_text(self.boundary, "boundary")
        consumers = tuple(str(item) for item in self.allowed_consumers)
        if not consumers:
            raise KnowledgeBackfeedError("scope_consumers_required")
        unknown = set(consumers) - CONSUMER_KINDS
        if unknown:
            raise KnowledgeBackfeedError(f"scope_consumers_unknown: {sorted(unknown)}")
        object.__setattr__(self, "allowed_consumers", consumers)

    def to_payload(self) -> dict[str, Any]:
        return {
            "scope_id": self.scope_id,
            "boundary": self.boundary,
            "allowed_consumers": list(self.allowed_consumers),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> Self:
        return cls(
            scope_id=_require_payload_str(payload, "scope_id"),
            boundary=_require_payload_str(payload, "boundary"),
            allowed_consumers=tuple(_require_sequence(payload, "allowed_consumers")),
        )


@dataclass(frozen=True, slots=True)
class BackfeedChange:
    """Proposed entity or relationship change."""

    change_kind: str
    subject_ref: str
    payload: Mapping[str, str]
    relationship_target_ref: str = ""
    deprecates: tuple[str, ...] = ()
    supersedes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.change_kind not in CHANGE_KINDS:
            raise KnowledgeBackfeedError("change_kind_unknown")
        _require_text(self.subject_ref, "subject_ref")
        if self.change_kind == "relationship":
            _require_text(self.relationship_target_ref, "relationship_target_ref")
        object.__setattr__(self, "payload", _string_mapping(self.payload, "payload"))
        object.__setattr__(self, "deprecates", _string_tuple(self.deprecates, "deprecates", allow_empty=True))
        object.__setattr__(self, "supersedes", _string_tuple(self.supersedes, "supersedes", allow_empty=True))

    def to_payload(self) -> dict[str, Any]:
        return {
            "change_kind": self.change_kind,
            "subject_ref": self.subject_ref,
            "relationship_target_ref": self.relationship_target_ref,
            "payload": dict(self.payload),
            "deprecates": list(self.deprecates),
            "supersedes": list(self.supersedes),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> Self:
        return cls(
            change_kind=_require_payload_str(payload, "change_kind"),
            subject_ref=_require_payload_str(payload, "subject_ref"),
            relationship_target_ref=str(payload.get("relationship_target_ref", "")),
            payload=_require_mapping(payload, "payload"),
            deprecates=tuple(str(item) for item in payload.get("deprecates", ())),
            supersedes=tuple(str(item) for item in payload.get("supersedes", ())),
        )

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"BackfeedChange(change_kind={self.change_kind!r}, subject_ref={self.subject_ref!r}, payload={self.payload!r})"


@dataclass(frozen=True, slots=True)
class BackfeedProposal:
    """One governed knowledge backfeed candidate."""

    proposal_id: str
    run_id: str
    source: BackfeedSource | Mapping[str, Any] | None
    change: BackfeedChange | Mapping[str, Any] | None
    scope: BackfeedScope | Mapping[str, Any] | None
    reason: str
    affected_workflows: tuple[str, ...]
    proposed_by: str
    created_at_utc: str

    def __post_init__(self) -> None:
        _require_text(self.proposal_id, "proposal_id")
        _require_text(self.run_id, "run_id")
        _require_text(self.reason, "reason")
        _require_text(self.proposed_by, "proposed_by")
        _require_utc(self.created_at_utc, "created_at_utc")
        if self.source is None:
            raise KnowledgeBackfeedError("source_required", proposal_id=self.proposal_id)
        if self.change is None:
            raise KnowledgeBackfeedError("change_required", proposal_id=self.proposal_id)
        if self.scope is None:
            raise KnowledgeBackfeedError("scope_required", proposal_id=self.proposal_id)
        source = self.source if isinstance(self.source, BackfeedSource) else BackfeedSource.from_payload(self.source)
        change = self.change if isinstance(self.change, BackfeedChange) else BackfeedChange.from_payload(self.change)
        scope = self.scope if isinstance(self.scope, BackfeedScope) else BackfeedScope.from_payload(self.scope)
        workflows = _string_tuple(self.affected_workflows, "affected_workflows")
        object.__setattr__(self, "source", source)
        object.__setattr__(self, "change", change)
        object.__setattr__(self, "scope", scope)
        object.__setattr__(self, "affected_workflows", workflows)

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "proposal_id": self.proposal_id,
            "run_id": self.run_id,
            "source": self.source.to_payload(),
            "change": self.change.to_payload(),
            "scope": self.scope.to_payload(),
            "reason": self.reason,
            "affected_workflows": list(self.affected_workflows),
            "proposed_by": self.proposed_by,
            "created_at_utc": self.created_at_utc,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> Self:
        return cls(
            proposal_id=_require_payload_str(payload, "proposal_id"),
            run_id=_require_payload_str(payload, "run_id"),
            source=BackfeedSource.from_payload(_require_mapping(payload, "source")),
            change=BackfeedChange.from_payload(_require_mapping(payload, "change")),
            scope=BackfeedScope.from_payload(_require_mapping(payload, "scope")),
            reason=_require_payload_str(payload, "reason"),
            affected_workflows=tuple(str(item) for item in _require_sequence(payload, "affected_workflows")),
            proposed_by=_require_payload_str(payload, "proposed_by"),
            created_at_utc=_require_payload_str(payload, "created_at_utc"),
        )

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"BackfeedProposal(proposal_id={self.proposal_id!r}, run_id={self.run_id!r}, source={self.source!r})"


@dataclass(frozen=True, slots=True)
class BackfeedWriteResult:
    """Result of an append-only mutation attempt."""

    status: BackfeedStatus
    changed: bool
    proposal_id: str
    record_id: str = ""

    def to_payload(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "changed": self.changed,
            "proposal_id": self.proposal_id,
            "record_id": self.record_id,
        }

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return (
            f"BackfeedWriteResult(status={self.status!r}, changed={self.changed!r}, proposal_id={self.proposal_id!r})"
        )


def _trusted_record_from_proposal(proposal: BackfeedProposal, *, decided_by: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "record_id": _trusted_record_id(proposal.proposal_id),
        "proposal_id": proposal.proposal_id,
        "run_id": proposal.run_id,
        "status": BackfeedStatus.APPROVED.value,
        "source": proposal.source.to_payload(),
        "change": proposal.change.to_payload(),
        "scope": proposal.scope.to_payload(),
        "reason": proposal.reason,
        "affected_workflows": list(proposal.affected_workflows),
        "approved_by": decided_by,
        "approved_at_utc": _utc_now(),
    }


def _decision_payload(
    proposal_id: str,
    *,
    decision: BackfeedStatus,
    decided_by: str,
    rationale: str,
    trusted_record_id: str = "",
    superseded_by: str = "",
) -> dict[str, Any]:
    if decision.value not in DECISION_KINDS:
        raise KnowledgeBackfeedError("decision_unknown", proposal_id=proposal_id)
    return {
        "schema_version": SCHEMA_VERSION,
        "decision_id": f"{proposal_id}:{decision.value}",
        "proposal_id": proposal_id,
        "decision": decision.value,
        "decided_by": decided_by,
        "rationale": rationale,
        "trusted_record_id": trusted_record_id,
        "superseded_by": superseded_by,
        "decided_at_utc": _utc_now(),
    }


def _proposal_payload(event: Mapping[str, Any]) -> dict[str, Any]:
    return dict(_require_mapping(event, "payload"))


def _decision_payload_from_event(event: Mapping[str, Any]) -> dict[str, Any]:
    return dict(_require_mapping(event, "payload"))


def _trusted_record_id(proposal_id: str) -> str:
    return f"trusted:{proposal_id}"


def _coerce_confidence(value: Any) -> float | None:
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        logger.warning("Handled recoverable failure before fallback.", exc_info=True)
        return None
    if not isfinite(confidence) or confidence < 0.0 or confidence > 1.0:
        return None
    return confidence


def _require_text(value: object, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise KnowledgeBackfeedError(f"{field_name}_required")


def _require_utc(value: str, field_name: str) -> None:
    _require_text(value, field_name)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise KnowledgeBackfeedError(f"{field_name}_invalid") from exc
    if parsed.tzinfo is None:
        raise KnowledgeBackfeedError(f"{field_name}_invalid")


def _require_payload_str(payload: Mapping[str, Any], field_name: str) -> str:
    if field_name not in payload:
        raise KnowledgeBackfeedError(f"{field_name}_required")
    value = payload[field_name]
    if not isinstance(value, str) or not value.strip():
        raise KnowledgeBackfeedError(f"{field_name}_required")
    return value


def _require_mapping(payload: Mapping[str, Any], field_name: str) -> Mapping[str, Any]:
    value = payload.get(field_name) if field_name in payload else payload
    if not isinstance(value, Mapping):
        raise KnowledgeBackfeedError(f"{field_name}_required")
    return value


def _require_sequence(payload: Mapping[str, Any], field_name: str) -> tuple[Any, ...]:
    value = payload.get(field_name)
    if not isinstance(value, list | tuple):
        raise KnowledgeBackfeedError(f"{field_name}_required")
    return tuple(value)


def _string_mapping(value: Any, field_name: str) -> dict[str, str]:
    if not isinstance(value, Mapping) or not value:
        raise KnowledgeBackfeedError(f"{field_name}_required")
    result = {str(key): str(item) for key, item in value.items()}
    if any(not key.strip() or not item.strip() for key, item in result.items()):
        raise KnowledgeBackfeedError(f"{field_name}_required")
    return result


def _string_tuple(values: Iterable[str], field_name: str, *, allow_empty: bool = False) -> tuple[str, ...]:
    result = tuple(str(value) for value in values)
    if not allow_empty and not result:
        raise KnowledgeBackfeedError(f"{field_name}_required")
    if any(not value.strip() for value in result):
        raise KnowledgeBackfeedError(f"{field_name}_required")
    return result


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
