"""Immutable memory governance lifecycle model.

The records in this module are deterministic facts and decisions. They do not
open files, call models, register callbacks, or persist shared state.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

SCHEMA_VERSION = 1


class MemoryGovernanceError(Exception):
    """Raised when a memory governance record or payload is incomplete."""


class MemoryLifecycleState(str, Enum):
    """Lifecycle states for governed memory candidates."""

    PROPOSED = "proposed"
    VALIDATED = "validated"
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    TOMBSTONED = "tombstoned"
    QUARANTINED = "quarantined"


class SourceTrustTier(str, Enum):
    """Source trust classification for a memory candidate."""

    UNTRUSTED = "untrusted"
    CANDIDATE = "candidate"
    TRUSTED = "trusted"


class MemoryAuthority(str, Enum):
    """Authority requested or returned for a memory lifecycle decision."""

    NONE = "none"
    CANDIDATE = "candidate"
    MEMORY = "memory"
    PROMPT = "prompt"
    PLANNING = "planning"
    ROUTING = "routing"
    POLICY = "policy"


class BoundaryClass(str, Enum):
    """Public/private boundary classification."""

    PUBLIC = "public"
    PRIVATE = "private"
    MIXED = "mixed"
    UNKNOWN = "unknown"


class RetentionClass(str, Enum):
    """Retention state required before a memory can become authoritative."""

    EPHEMERAL = "ephemeral"
    RETAINED = "retained"
    EXPIRED = "expired"
    FORGET_REQUESTED = "forget_requested"


class ConflictStatus(str, Enum):
    """Conflict review state."""

    CLEAR = "clear"
    CONFLICT = "conflict"
    UNKNOWN = "unknown"


class RollbackStatus(str, Enum):
    """Whether rollback metadata is available."""

    PRESENT = "present"
    ABSENT = "absent"


class ApprovalState(str, Enum):
    """Human or policy approval state."""

    APPROVED = "approved"
    REJECTED = "rejected"
    MISSING = "missing"


class PolicyState(str, Enum):
    """Policy evaluation state."""

    APPROVED = "approved"
    BLOCKED = "blocked"
    UNKNOWN = "unknown"


class TaintStatus(str, Enum):
    """Prompt-injection or contamination taint state."""

    CLEAN = "clean"
    TAINTED = "tainted"
    UNKNOWN = "unknown"


class MemoryDecisionResult(str, Enum):
    """Deterministic firewall/lifecycle decision result."""

    ACCEPTED = "accepted"
    BLOCKED = "blocked"
    QUARANTINED = "quarantined"
    TOMBSTONED = "tombstoned"
    SUPERSEDED = "superseded"
    FORGOTTEN = "forgotten"


def _require_non_empty(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MemoryGovernanceError(f"{field_name} must be non-empty")
    return value


def _require_mapping(value: Mapping[str, str], field_name: str, required: tuple[str, ...]) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise MemoryGovernanceError(f"{field_name} must be a mapping")
    result = {str(key): str(item) for key, item in value.items()}
    for key in required:
        if not result.get(key, "").strip():
            raise MemoryGovernanceError(f"{field_name}.{key} must be non-empty")
    return result


def _require_tuple(values: tuple[str, ...], field_name: str) -> tuple[str, ...]:
    if not isinstance(values, tuple):
        raise MemoryGovernanceError(f"{field_name} must be a tuple")
    if not values or not all(isinstance(value, str) and value.strip() for value in values):
        raise MemoryGovernanceError(f"{field_name} must contain at least one non-empty string")
    return values


def _coerce_tuple(values: Any, field_name: str) -> tuple[str, ...]:
    if not isinstance(values, (list, tuple)):
        raise MemoryGovernanceError(f"{field_name} must be a list or tuple")
    return _require_tuple(tuple(str(value) for value in values), field_name)


def _coerce_enum(enum_type: type[Enum], value: Any, field_name: str) -> Enum:
    if isinstance(value, enum_type):
        return value
    raw_value = value.value if isinstance(value, Enum) else value
    try:
        return enum_type(str(raw_value))
    except ValueError as exc:
        raise MemoryGovernanceError(f"{field_name} has unknown value: {value!r}") from exc


@dataclass(frozen=True, slots=True)
class MemoryGovernanceRecord:
    """One governed memory candidate with all authority prerequisites explicit."""

    memory_id: str
    state: MemoryLifecycleState
    provenance: Mapping[str, str]
    scope: str
    source_trust: SourceTrustTier
    authority: MemoryAuthority
    policy_state: PolicyState
    retention: RetentionClass
    boundary: BoundaryClass
    conflict: ConflictStatus
    rollback: Mapping[str, str]
    rollback_status: RollbackStatus
    approval: Mapping[str, str]
    approval_state: ApprovalState
    taint: TaintStatus
    lineage_refs: tuple[str, ...]
    created_at_utc: str

    def __post_init__(self) -> None:
        _require_non_empty(self.memory_id, "memory_id")
        _require_non_empty(self.scope, "scope")
        _require_non_empty(self.created_at_utc, "created_at_utc")
        _require_mapping(self.provenance, "provenance", ("source", "reason"))
        _require_mapping(self.rollback, "rollback", ("plan_id", "strategy"))
        _require_mapping(self.approval, "approval", ("approval_id", "approved_by"))
        _require_tuple(self.lineage_refs, "lineage_refs")
        if not isinstance(self.state, MemoryLifecycleState):
            raise MemoryGovernanceError("state must be MemoryLifecycleState")
        if not isinstance(self.source_trust, SourceTrustTier):
            raise MemoryGovernanceError("source_trust must be SourceTrustTier")
        if not isinstance(self.authority, MemoryAuthority):
            raise MemoryGovernanceError("authority must be MemoryAuthority")
        if not isinstance(self.policy_state, PolicyState):
            raise MemoryGovernanceError("policy_state must be PolicyState")
        if not isinstance(self.retention, RetentionClass):
            raise MemoryGovernanceError("retention must be RetentionClass")
        if not isinstance(self.boundary, BoundaryClass):
            raise MemoryGovernanceError("boundary must be BoundaryClass")
        if not isinstance(self.conflict, ConflictStatus):
            raise MemoryGovernanceError("conflict must be ConflictStatus")
        if not isinstance(self.rollback_status, RollbackStatus):
            raise MemoryGovernanceError("rollback_status must be RollbackStatus")
        if not isinstance(self.approval_state, ApprovalState):
            raise MemoryGovernanceError("approval_state must be ApprovalState")
        if not isinstance(self.taint, TaintStatus):
            raise MemoryGovernanceError("taint must be TaintStatus")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"MemoryGovernanceRecord(memory_id={self.memory_id!r}, state={self.state!r}, provenance={self.provenance!r})"


@dataclass(frozen=True, slots=True)
class MemoryGovernanceDecision:
    """Immutable result returned by governance/firewall evaluation."""

    memory_id: str
    result: MemoryDecisionResult
    state: MemoryLifecycleState
    authority: MemoryAuthority
    blockers: tuple[str, ...]
    prompt_eligible: bool
    planning_eligible: bool
    routing_eligible: bool
    reason: str
    decision_source: str = "memory_firewall"
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_non_empty(self.memory_id, "memory_id")
        _require_non_empty(self.reason, "reason")
        if not isinstance(self.result, MemoryDecisionResult):
            raise MemoryGovernanceError("result must be MemoryDecisionResult")
        if not isinstance(self.state, MemoryLifecycleState):
            raise MemoryGovernanceError("state must be MemoryLifecycleState")
        if not isinstance(self.authority, MemoryAuthority):
            raise MemoryGovernanceError("authority must be MemoryAuthority")
        if not isinstance(self.blockers, tuple):
            raise MemoryGovernanceError("blockers must be a tuple")
        if self.blockers and (self.prompt_eligible or self.planning_eligible or self.routing_eligible):
            raise MemoryGovernanceError("blocked decisions cannot be eligible for authority")
        if self.state is not MemoryLifecycleState.ACTIVE and (
            self.prompt_eligible or self.planning_eligible or self.routing_eligible
        ):
            raise MemoryGovernanceError("non-active decisions cannot be authority eligible")
        if self.decision_source != "memory_firewall":
            raise MemoryGovernanceError("decision_source must be memory_firewall")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"MemoryGovernanceDecision(memory_id={self.memory_id!r}, result={self.result!r}, state={self.state!r})"


def memory_governance_to_payload(
    record: MemoryGovernanceRecord,
    decision: MemoryGovernanceDecision | None = None,
) -> dict[str, Any]:
    """Return a schema-shaped payload for a governed memory record.

    Args:
        record: Typed record consumed by the operation.
        decision: Decision value consumed by memory_governance_to_payload().

    Returns:
        dict[str, Any] value produced by memory_governance_to_payload().
    """
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "memory_id": record.memory_id,
        "state": record.state.value,
        "provenance": dict(record.provenance),
        "scope": record.scope,
        "source_trust": record.source_trust.value,
        "authority": record.authority.value,
        "policy_state": record.policy_state.value,
        "retention": record.retention.value,
        "boundary": record.boundary.value,
        "conflict": record.conflict.value,
        "rollback": dict(record.rollback),
        "rollback_status": record.rollback_status.value,
        "approval": dict(record.approval),
        "approval_state": record.approval_state.value,
        "taint": record.taint.value,
        "lineage_refs": list(record.lineage_refs),
        "created_at_utc": record.created_at_utc,
    }
    payload["firewall_decision"] = memory_decision_to_payload(decision or _default_blocking_decision(record))
    return payload


def _default_blocking_decision(record: MemoryGovernanceRecord) -> MemoryGovernanceDecision:
    return MemoryGovernanceDecision(
        memory_id=record.memory_id,
        result=MemoryDecisionResult.BLOCKED,
        state=record.state,
        authority=MemoryAuthority.NONE,
        blockers=("firewall_evaluation_missing",),
        prompt_eligible=False,
        planning_eligible=False,
        routing_eligible=False,
        reason="memory firewall decision was not supplied",
    )


def memory_decision_to_payload(decision: MemoryGovernanceDecision) -> dict[str, Any]:
    """Return a JSON-safe payload for an immutable governance decision."""
    return {
        "memory_id": decision.memory_id,
        "result": decision.result.value,
        "state": decision.state.value,
        "authority": decision.authority.value,
        "blockers": list(decision.blockers),
        "prompt_eligible": decision.prompt_eligible,
        "planning_eligible": decision.planning_eligible,
        "routing_eligible": decision.routing_eligible,
        "reason": decision.reason,
        "decision_source": decision.decision_source,
        "metadata": dict(decision.metadata),
    }


def validate_memory_governance_payload(payload: Mapping[str, Any]) -> MemoryGovernanceRecord:
    """Parse and validate a memory governance payload, failing closed.

    Returns:
        Validation outcome for memory governance payload.

    Raises:
        Exception: Propagates validation or runtime failures from the underlying operation.
    """
    if not isinstance(payload, Mapping):
        raise MemoryGovernanceError("payload must be an object")
    payload = migrate_memory_governance_payload(payload)
    try:
        return MemoryGovernanceRecord(
            memory_id=str(payload["memory_id"]),
            state=_coerce_enum(MemoryLifecycleState, payload["state"], "state"),
            provenance=dict(payload["provenance"]),
            scope=str(payload["scope"]),
            source_trust=_coerce_enum(SourceTrustTier, payload["source_trust"], "source_trust"),
            authority=_coerce_enum(MemoryAuthority, payload["authority"], "authority"),
            policy_state=_coerce_enum(PolicyState, payload["policy_state"], "policy_state"),
            retention=_coerce_enum(RetentionClass, payload["retention"], "retention"),
            boundary=_coerce_enum(BoundaryClass, payload["boundary"], "boundary"),
            conflict=_coerce_enum(ConflictStatus, payload["conflict"], "conflict"),
            rollback=dict(payload["rollback"]),
            rollback_status=_coerce_enum(RollbackStatus, payload["rollback_status"], "rollback_status"),
            approval=dict(payload["approval"]),
            approval_state=_coerce_enum(ApprovalState, payload["approval_state"], "approval_state"),
            taint=_coerce_enum(TaintStatus, payload["taint"], "taint"),
            lineage_refs=_coerce_tuple(payload["lineage_refs"], "lineage_refs"),
            created_at_utc=str(payload["created_at_utc"]),
        )
    except KeyError as exc:
        raise MemoryGovernanceError(f"missing required payload key: {exc.args[0]}") from exc
    except (TypeError, ValueError) as exc:
        raise MemoryGovernanceError(str(exc)) from exc


def migrate_memory_governance_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Return a current-schema memory payload or fail closed for unknown schemas.

    Legacy schema version 0 did not carry all authority signals. Missing
    authority-critical values are migrated to blocking states so old records can
    be reviewed or quarantined without silently becoming prompt/planning memory.

    Returns:
        Current-schema payload dictionary.

    Raises:
        MemoryGovernanceError: If the payload declares an unknown schema version.
    """
    version = payload.get("schema_version")
    if version == SCHEMA_VERSION:
        return dict(payload)
    if version not in (0, "0", None):
        raise MemoryGovernanceError(f"schema_version must be {SCHEMA_VERSION} or migratable legacy version 0")

    migrated = dict(payload)
    migrated["schema_version"] = SCHEMA_VERSION
    migrated.setdefault("state", MemoryLifecycleState.QUARANTINED.value)
    migrated.setdefault("source_trust", SourceTrustTier.CANDIDATE.value)
    migrated.setdefault("authority", MemoryAuthority.NONE.value)
    migrated.setdefault("policy_state", PolicyState.UNKNOWN.value)
    migrated.setdefault("retention", RetentionClass.EPHEMERAL.value)
    migrated.setdefault("boundary", BoundaryClass.UNKNOWN.value)
    migrated.setdefault("conflict", ConflictStatus.UNKNOWN.value)
    migrated.setdefault("rollback", {"plan_id": "legacy-migration-required", "strategy": "manual review"})
    migrated.setdefault("rollback_status", RollbackStatus.ABSENT.value)
    migrated.setdefault("approval", {"approval_id": "legacy-migration-required", "approved_by": "system"})
    migrated.setdefault("approval_state", ApprovalState.MISSING.value)
    migrated.setdefault("taint", TaintStatus.UNKNOWN.value)
    migrated.setdefault("lineage_refs", ["legacy-migration-required"])
    return migrated
