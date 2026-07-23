"""Universal Workbench event and influence contract.

The weaving layer is import-safe and intentionally side-effect free. It turns
existing Workbench records into typed events, records evidence-backed influence
links between those events, and evaluates whether a pack has a closed loop from
source evidence to an authorized acceptance event.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from vetinari.workbench.weaving.record_shapes import WEAVING_PUBLIC_EXPORTS
from vetinari.workbench.weaving.record_shapes import _record_shape as _record_shape_impl


class WorkbenchWeavingError(ValueError):
    """Raised when an event or influence cannot be trusted."""


class WeavingAuthorityLevel(str, Enum):
    """Authority level attached to an event or influence claim."""

    OBSERVED = "observed"
    SUGGESTED = "suggested"
    PROPOSED = "proposed"
    APPROVED = "approved"
    EXECUTED = "executed"
    PROMOTED = "promoted"


class WorkbenchEventKind(str, Enum):
    """Canonical event vocabulary shared by Workbench subsystems."""

    ASSET_RECORDED = "asset_recorded"
    RUN_RECORDED = "run_recorded"
    TRACE_CAPTURED = "trace_captured"
    EVAL_RECORDED = "eval_recorded"
    PROPOSAL_RECORDED = "proposal_recorded"
    PROMOTION_DECIDED = "promotion_decided"
    LEASE_RECORDED = "lease_recorded"
    AUTOMATION_SIMULATED = "automation_simulated"
    MONITORING_SIGNAL = "monitoring_signal"
    IMPROVEMENT_DECISION = "improvement_decision"
    PACK_ACCEPTANCE = "pack_acceptance"


class WorkbenchSubjectKind(str, Enum):
    """Subjects that may emit universal Workbench events."""

    ASSET = "asset"
    RUN = "run"
    TRACE = "trace"
    EVAL = "eval"
    PROPOSAL = "proposal"
    PROMOTION = "promotion"
    LEASE = "lease"
    AUTOMATION = "automation"
    MONITORING_SIGNAL = "monitoring_signal"
    IMPROVEMENT = "improvement"
    PACK = "pack"


class InfluenceKind(str, Enum):
    """How one Workbench event influenced another."""

    DERIVED_FROM = "derived_from"
    EVIDENCE_FOR = "evidence_for"
    CAUSED_BY = "caused_by"
    APPROVED_BY = "approved_by"
    BLOCKED_BY = "blocked_by"
    SUPERSEDES = "supersedes"
    DEPENDS_ON = "depends_on"


_AUTHORITY_RANK = {
    WeavingAuthorityLevel.OBSERVED: 1,
    WeavingAuthorityLevel.SUGGESTED: 2,
    WeavingAuthorityLevel.PROPOSED: 3,
    WeavingAuthorityLevel.APPROVED: 4,
    WeavingAuthorityLevel.EXECUTED: 5,
    WeavingAuthorityLevel.PROMOTED: 6,
}


@dataclass(frozen=True, slots=True)
class WorkbenchEvent:
    """One evidence-backed event in the universal Workbench ledger."""

    event_id: str
    kind: WorkbenchEventKind
    subject_kind: WorkbenchSubjectKind
    subject_ref: str
    occurred_at_utc: str
    actor: str
    authority_level: WeavingAuthorityLevel
    evidence_refs: tuple[str, ...]
    provenance_refs: tuple[str, ...]
    confidence: float
    project_id: str = "default"
    source_surface: str = ""
    payload: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_text(self.event_id, "event_id")
        _require_text(self.subject_ref, "subject_ref")
        _require_text(self.occurred_at_utc, "occurred_at_utc")
        _require_text(self.actor, "actor")
        _require_text(self.project_id, "project_id")
        _require_string_tuple(self.evidence_refs, "evidence_refs")
        _require_string_tuple(self.provenance_refs, "provenance_refs")
        if not isinstance(self.kind, WorkbenchEventKind):
            raise WorkbenchWeavingError("kind must be WorkbenchEventKind")
        if not isinstance(self.subject_kind, WorkbenchSubjectKind):
            raise WorkbenchWeavingError("subject_kind must be WorkbenchSubjectKind")
        if not isinstance(self.authority_level, WeavingAuthorityLevel):
            raise WorkbenchWeavingError("authority_level must be WeavingAuthorityLevel")
        if not 0.0 < self.confidence <= 1.0:
            raise WorkbenchWeavingError("confidence must be > 0.0 and <= 1.0")

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-schema-compatible payload."""
        return {
            "event_id": self.event_id,
            "kind": self.kind.value,
            "subject_kind": self.subject_kind.value,
            "subject_ref": self.subject_ref,
            "occurred_at_utc": self.occurred_at_utc,
            "actor": self.actor,
            "authority_level": self.authority_level.value,
            "evidence_refs": list(self.evidence_refs),
            "provenance_refs": list(self.provenance_refs),
            "confidence": self.confidence,
            "project_id": self.project_id,
            "source_surface": self.source_surface,
            "payload": dict(self.payload),
        }

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"WorkbenchEvent(event_id={self.event_id!r}, kind={self.kind!r}, subject_kind={self.subject_kind!r})"


@dataclass(frozen=True, slots=True)
class WorkbenchInfluence:
    """Evidence-backed influence edge between two Workbench events."""

    influence_id: str
    source_event_id: str
    target_event_id: str
    kind: InfluenceKind
    authority_level: WeavingAuthorityLevel
    evidence_refs: tuple[str, ...]
    provenance_refs: tuple[str, ...]
    confidence: float
    rationale: str
    created_at_utc: str
    reversible: bool = True

    def __post_init__(self) -> None:
        _require_text(self.influence_id, "influence_id")
        _require_text(self.source_event_id, "source_event_id")
        _require_text(self.target_event_id, "target_event_id")
        if self.source_event_id == self.target_event_id:
            raise WorkbenchWeavingError("influence cannot target its source event")
        if not isinstance(self.kind, InfluenceKind):
            raise WorkbenchWeavingError("kind must be InfluenceKind")
        if not isinstance(self.authority_level, WeavingAuthorityLevel):
            raise WorkbenchWeavingError("authority_level must be WeavingAuthorityLevel")
        _require_string_tuple(self.evidence_refs, "evidence_refs")
        _require_string_tuple(self.provenance_refs, "provenance_refs")
        _require_text(self.rationale, "rationale")
        _require_text(self.created_at_utc, "created_at_utc")
        if not 0.0 < self.confidence <= 1.0:
            raise WorkbenchWeavingError("confidence must be > 0.0 and <= 1.0")

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-schema-compatible payload."""
        return {
            "influence_id": self.influence_id,
            "source_event_id": self.source_event_id,
            "target_event_id": self.target_event_id,
            "kind": self.kind.value,
            "authority_level": self.authority_level.value,
            "evidence_refs": list(self.evidence_refs),
            "provenance_refs": list(self.provenance_refs),
            "confidence": self.confidence,
            "rationale": self.rationale,
            "created_at_utc": self.created_at_utc,
            "reversible": self.reversible,
        }

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"WorkbenchInfluence(influence_id={self.influence_id!r}, source_event_id={self.source_event_id!r}, target_event_id={self.target_event_id!r})"


@dataclass(frozen=True, slots=True)
class ChangePropagationDecision:
    """Fail-closed decision describing whether a change may propagate."""

    target_event_id: str
    propagated: bool
    source_event_ids: tuple[str, ...]
    blockers: tuple[str, ...]
    evidence_refs: tuple[str, ...]

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"ChangePropagationDecision(target_event_id={self.target_event_id!r}, propagated={self.propagated!r}, source_event_ids={self.source_event_ids!r})"


@dataclass(frozen=True, slots=True)
class ClosedLoopAcceptance:
    """Result of checking a pack acceptance event against influence evidence."""

    pack_slug: str
    passed: bool
    acceptance_event_id: str
    influence_ids: tuple[str, ...]
    blockers: tuple[str, ...]
    evidence_refs: tuple[str, ...]

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"ClosedLoopAcceptance(pack_slug={self.pack_slug!r}, passed={self.passed!r}, acceptance_event_id={self.acceptance_event_id!r})"


class WorkbenchWeavingLedger:
    """In-memory, side-effect-free event and influence ledger."""

    def __init__(
        self,
        *,
        events: Sequence[WorkbenchEvent] = (),
        influences: Sequence[WorkbenchInfluence] = (),
    ) -> None:
        self._events: dict[str, WorkbenchEvent] = {}
        self._influences: dict[str, WorkbenchInfluence] = {}
        for event in events:
            self.record_event(event)
        for influence in influences:
            self.record_influence(influence)

    @property
    def events(self) -> tuple[WorkbenchEvent, ...]:
        return tuple(self._events.values())

    @property
    def influences(self) -> tuple[WorkbenchInfluence, ...]:
        return tuple(self._influences.values())

    def record_event(self, event: WorkbenchEvent) -> None:
        """Execute the record event operation.

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        if event.event_id in self._events:
            raise WorkbenchWeavingError(f"duplicate event_id rejected: {event.event_id}")
        self._events[event.event_id] = event

    def record_influence(self, influence: WorkbenchInfluence) -> None:
        """Execute the record influence operation.

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        if influence.influence_id in self._influences:
            raise WorkbenchWeavingError(f"duplicate influence_id rejected: {influence.influence_id}")
        if influence.source_event_id not in self._events:
            raise WorkbenchWeavingError(f"source_event_id not recorded: {influence.source_event_id}")
        if influence.target_event_id not in self._events:
            raise WorkbenchWeavingError(f"target_event_id not recorded: {influence.target_event_id}")
        self._influences[influence.influence_id] = influence

    def event(self, event_id: str) -> WorkbenchEvent:
        """Execute the event operation.

        Returns:
            WorkbenchEvent value produced by event().

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        try:
            return self._events[event_id]
        except KeyError as exc:
            raise WorkbenchWeavingError(f"event_id not recorded: {event_id}") from exc

    def influences_for(self, target_event_id: str) -> tuple[WorkbenchInfluence, ...]:
        """Execute the influences for operation.

        Returns:
            tuple[WorkbenchInfluence, ...] value produced by influences_for().
        """
        self.event(target_event_id)
        return tuple(
            influence for influence in self._influences.values() if influence.target_event_id == target_event_id
        )

    def influenced_by(self, target_event_id: str) -> tuple[WorkbenchEvent, ...]:
        return tuple(self.event(influence.source_event_id) for influence in self.influences_for(target_event_id))

    def evaluate_change_propagation(
        self,
        target_event_id: str,
        *,
        minimum_authority: WeavingAuthorityLevel = WeavingAuthorityLevel.PROPOSED,
    ) -> ChangePropagationDecision:
        """Return fail-closed propagation status for a target event.

        Returns:
            ChangePropagationDecision value produced by evaluate_change_propagation().
        """
        blockers: list[str] = []
        evidence_refs: list[str] = []
        source_ids: list[str] = []
        target = self.event(target_event_id)
        if not authority_at_least(target.authority_level, minimum_authority):
            blockers.append("target_authority_below_minimum")
        evidence_refs.extend(target.evidence_refs)
        influences = self.influences_for(target_event_id)
        if not influences:
            blockers.append("missing_influence_links")
        for influence in influences:
            source_ids.append(influence.source_event_id)
            evidence_refs.extend(influence.evidence_refs)
            if not authority_at_least(influence.authority_level, minimum_authority):
                blockers.append(f"influence_authority_below_minimum:{influence.influence_id}")
        return ChangePropagationDecision(
            target_event_id=target_event_id,
            propagated=not blockers,
            source_event_ids=tuple(source_ids),
            blockers=tuple(blockers),
            evidence_refs=tuple(dict.fromkeys(evidence_refs)),
        )

    def evaluate_closed_loop_acceptance(
        self,
        pack_slug: str,
        *,
        minimum_authority: WeavingAuthorityLevel = WeavingAuthorityLevel.APPROVED,
    ) -> ClosedLoopAcceptance:
        """Require an authorized pack acceptance event influenced by prior evidence.

        Returns:
            ClosedLoopAcceptance value produced by evaluate_closed_loop_acceptance().
        """
        _require_text(pack_slug, "pack_slug")
        candidates = [
            event
            for event in self._events.values()
            if event.kind is WorkbenchEventKind.PACK_ACCEPTANCE and event.payload.get("pack_slug") == pack_slug
        ]
        if not candidates:
            return ClosedLoopAcceptance(
                pack_slug=pack_slug,
                passed=False,
                acceptance_event_id="",
                influence_ids=(),
                blockers=("missing_pack_acceptance_event",),
                evidence_refs=(),
            )
        acceptance = candidates[-1]
        blockers: list[str] = []
        if not authority_at_least(acceptance.authority_level, minimum_authority):
            blockers.append("acceptance_authority_below_minimum")
        inbound = self.influences_for(acceptance.event_id)
        if not inbound:
            blockers.append("missing_influence_links")
        if not acceptance.evidence_refs:
            blockers.append("missing_acceptance_evidence")
        if not acceptance.provenance_refs:
            blockers.append("missing_acceptance_provenance")
        evidence_refs = list(acceptance.evidence_refs)
        for influence in inbound:
            evidence_refs.extend(influence.evidence_refs)
        return ClosedLoopAcceptance(
            pack_slug=pack_slug,
            passed=not blockers,
            acceptance_event_id=acceptance.event_id,
            influence_ids=tuple(influence.influence_id for influence in inbound),
            blockers=tuple(blockers),
            evidence_refs=tuple(dict.fromkeys(evidence_refs)),
        )


def authority_at_least(actual: WeavingAuthorityLevel, required: WeavingAuthorityLevel) -> bool:
    """Return whether ``actual`` is at least as strong as ``required``."""
    return _AUTHORITY_RANK[actual] >= _AUTHORITY_RANK[required]


def event_from_workbench_record(
    record: object,
    *,
    event_id: str,
    occurred_at_utc: str,
    actor: str,
    authority_level: WeavingAuthorityLevel,
    evidence_refs: tuple[str, ...],
    provenance_refs: tuple[str, ...],
    confidence: float,
    project_id: str = "default",
    source_surface: str = "",
) -> WorkbenchEvent:
    """Create a universal event for an existing Workbench runtime record.

    Returns:
        WorkbenchEvent value produced by event_from_workbench_record().
    """
    kind, subject_kind, subject_ref, payload = _record_shape(record)
    return WorkbenchEvent(
        event_id=event_id,
        kind=kind,
        subject_kind=subject_kind,
        subject_ref=subject_ref,
        occurred_at_utc=occurred_at_utc,
        actor=actor,
        authority_level=authority_level,
        evidence_refs=evidence_refs,
        provenance_refs=provenance_refs,
        confidence=confidence,
        project_id=project_id,
        source_surface=source_surface,
        payload=payload,
    )


def pack_acceptance_event(
    *,
    event_id: str,
    pack_slug: str,
    occurred_at_utc: str,
    actor: str,
    passed: bool,
    evidence_refs: tuple[str, ...],
    provenance_refs: tuple[str, ...],
    confidence: float,
    authority_level: WeavingAuthorityLevel = WeavingAuthorityLevel.APPROVED,
    project_id: str = "default",
) -> WorkbenchEvent:
    """Create the canonical closed-loop pack acceptance event."""
    return WorkbenchEvent(
        event_id=event_id,
        kind=WorkbenchEventKind.PACK_ACCEPTANCE,
        subject_kind=WorkbenchSubjectKind.PACK,
        subject_ref=pack_slug,
        occurred_at_utc=occurred_at_utc,
        actor=actor,
        authority_level=authority_level,
        evidence_refs=evidence_refs,
        provenance_refs=provenance_refs,
        confidence=confidence,
        project_id=project_id,
        source_surface="vetinari.workbench.weaving",
        payload={"pack_slug": pack_slug, "passed": passed},
    )


def _record_shape(record: object) -> tuple[WorkbenchEventKind, WorkbenchSubjectKind, str, dict[str, Any]]:
    return _record_shape_impl(
        record,
        event_kind=WorkbenchEventKind,
        subject_kind=WorkbenchSubjectKind,
        error_cls=WorkbenchWeavingError,
    )


def _require_text(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise WorkbenchWeavingError(f"{field_name} must be non-empty")


def _require_string_tuple(values: tuple[str, ...], field_name: str) -> None:
    if not isinstance(values, tuple):
        raise WorkbenchWeavingError(f"{field_name} must be a tuple")
    if not values or not all(isinstance(value, str) and value.strip() for value in values):
        raise WorkbenchWeavingError(f"{field_name} must contain non-empty strings")


__all__ = WEAVING_PUBLIC_EXPORTS
