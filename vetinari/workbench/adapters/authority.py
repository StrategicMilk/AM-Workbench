"""Fail-closed authority contracts for Workbench ecosystem adapters.

The contracts in this module keep tools such as Langfuse, Phoenix, Braintrust,
MLflow, DVC, lakeFS, Label Studio, Temporal, and DBOS behind explicit
Workbench authority decisions. Importing the module is side-effect free: no
files are opened, no handlers are registered, and no external tool is contacted.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from math import isfinite
from typing import Any

logger = logging.getLogger(__name__)


ROUND_TRIP_EVIDENCE_KEY = "evidence_refs"
DEFAULT_ROUND_TRIP_KEYS = ("trace_id", "dataset_revision_id", "annotation_id", "workflow_id")


class AdapterAuthorityError(ValueError):
    """Raised when an adapter exchange is not authorized."""

    def __init__(self, decision: AdapterAuthorityDecision) -> None:
        super().__init__(f"adapter exchange rejected: {', '.join(reason.value for reason in decision.reasons)}")
        self.decision = decision


class AdapterOperation(str, Enum):
    """Allowed adapter operation roles."""

    MIRROR = "mirror"
    EXECUTE = "execute"
    EVALUATE = "evaluate"
    ANNOTATE = "annotate"
    IMPORT_DATASET = "import_dataset"


class AdapterDomain(str, Enum):
    """Workbench artifact domains that adapters can exchange."""

    TRACE = "trace"
    DATASET = "dataset"
    ANNOTATION = "annotation"
    WORKFLOW = "workflow"


class AdapterDirection(str, Enum):
    """Direction of data movement relative to AM Workbench."""

    EXPORT = "export"
    IMPORT = "import"


class AuthorityMode(str, Enum):
    """Whether Workbench or the external tool is the source of truth."""

    WORKBENCH_AUTHORITATIVE = "workbench_authoritative"
    EXTERNAL_READ_ONLY = "external_read_only"
    EXTERNAL_CAN_PROPOSE = "external_can_propose"


class Lossiness(str, Enum):
    """How much information an adapter mapping preserves."""

    LOSSLESS = "lossless"
    LOSSY_SUMMARY = "lossy_summary"
    ID_ONLY = "id_only"


class ConflictBehavior(str, Enum):
    """How conflicting external state is handled."""

    REJECT = "reject"
    PROPOSE_ONLY = "propose_only"
    WORKBENCH_WINS = "workbench_wins"


class PrivacyPolicy(str, Enum):
    """Data privacy behavior for outbound adapter exchanges."""

    LOCAL_ONLY = "local_only"
    REDACTED_EXPORT = "redacted_export"
    ALLOWLISTED_EXPORT = "allowlisted_export"


class StaleDataPolicy(str, Enum):
    """Handling for stale adapter provenance."""

    REJECT = "reject"
    REQUIRE_REFRESH = "require_refresh"
    MARK_DEGRADED = "mark_degraded"


class AdapterDecisionReason(str, Enum):
    """Machine-readable reasons an adapter exchange failed closed."""

    UNSUPPORTED_OPERATION = "unsupported_operation"
    UNSUPPORTED_DOMAIN = "unsupported_domain"
    MISSING_PERMISSION = "missing_permission"
    MISSING_PROVENANCE = "missing_provenance"
    MISSING_EVIDENCE = "missing_evidence"
    MISSING_AUTHORITY = "missing_authority"
    MISSING_PERSISTED_STATE = "missing_persisted_state"
    MISSING_CONFIDENCE = "missing_confidence"
    LOW_CONFIDENCE = "low_confidence"
    UNREADABLE_CAPTURE_TIME = "unreadable_capture_time"
    STALE_PROVENANCE = "stale_provenance"
    LOCAL_ONLY_EXPORT = "local_only_export"
    UNREDACTED_EXPORT = "unredacted_export"
    EXTERNAL_AUTHORITY_CONFLICT = "external_authority_conflict"
    CONFLICT_REJECTED = "conflict_rejected"
    ROUND_TRIP_LOSS = "round_trip_loss"


@dataclass(frozen=True, slots=True)
class ProvenanceMapping:
    """Required external-to-Workbench provenance fields."""

    required_fields: tuple[str, ...]
    identifier_fields: tuple[str, ...] = DEFAULT_ROUND_TRIP_KEYS

    def __post_init__(self) -> None:
        _require_string_tuple(self.required_fields, "provenance.required_fields")
        _require_string_tuple(self.identifier_fields, "provenance.identifier_fields")


@dataclass(frozen=True, slots=True)
class AdapterAuthorityContract:
    """Trust boundary declaration for one external ecosystem adapter."""

    adapter_id: str
    display_name: str
    tool_kind: str
    operations: tuple[AdapterOperation, ...]
    domains: tuple[AdapterDomain, ...]
    authority_mode: AuthorityMode
    workbench_authoritative: bool
    lossiness: Lossiness
    provenance_mapping: ProvenanceMapping
    conflict_behavior: ConflictBehavior
    privacy_policy: PrivacyPolicy
    stale_data_policy: StaleDataPolicy
    required_permissions: tuple[str, ...]
    minimum_confidence: float = 0.8
    max_staleness_hours: int = 24
    evidence_required: bool = True
    round_trip_keys: tuple[str, ...] = DEFAULT_ROUND_TRIP_KEYS

    def __post_init__(self) -> None:
        _require_text(self.adapter_id, "adapter_id")
        _require_text(self.display_name, "display_name")
        _require_text(self.tool_kind, "tool_kind")
        if not self.operations:
            raise ValueError("operations must be non-empty")
        if not self.domains:
            raise ValueError("domains must be non-empty")
        _require_string_tuple(self.required_permissions, "required_permissions")
        if not isinstance(self.workbench_authoritative, bool):
            raise ValueError("workbench_authoritative must be bool")
        if not 0 <= self.minimum_confidence <= 1:
            raise ValueError("minimum_confidence must be between 0 and 1")
        if self.max_staleness_hours <= 0:
            raise ValueError("max_staleness_hours must be > 0")
        _require_string_tuple(self.round_trip_keys, "round_trip_keys")

    @property
    def permission_names(self) -> tuple[str, ...]:
        """All permissions required before an exchange can proceed."""
        operation_permissions = tuple(f"operation:{operation.value}" for operation in self.operations)
        return self.required_permissions + operation_permissions

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"AdapterAuthorityContract(adapter_id={self.adapter_id!r}, display_name={self.display_name!r}, tool_kind={self.tool_kind!r})"


@dataclass(frozen=True, slots=True)
class AdapterExchange:
    """One proposed import/export with authority evidence attached."""

    operation: AdapterOperation
    domain: AdapterDomain
    direction: AdapterDirection
    workbench_id: str
    external_id: str
    payload: Mapping[str, Any]
    provenance: Mapping[str, str]
    evidence_refs: tuple[str, ...]
    permissions: tuple[str, ...]
    confidence: float | int | str | None
    captured_at_utc: str
    authority_ref: str
    persisted_state_ref: str

    def __post_init__(self) -> None:
        _require_text(self.workbench_id, "workbench_id")
        _require_text(self.external_id, "external_id")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"AdapterExchange(operation={self.operation!r}, domain={self.domain!r}, direction={self.direction!r})"


@dataclass(frozen=True, slots=True)
class AdapterAuthorityDecision:
    """Fail-closed authorization result for one adapter exchange."""

    allowed: bool
    degraded: bool
    action: str
    reasons: tuple[AdapterDecisionReason, ...] = ()

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"AdapterAuthorityDecision(allowed={self.allowed!r}, degraded={self.degraded!r}, action={self.action!r})"


@dataclass(frozen=True, slots=True)
class ExternalAdapterRecord:
    """Canonical outbound record used for cross-tool round-trip proof."""

    adapter_id: str
    operation: AdapterOperation
    domain: AdapterDomain
    workbench_id: str
    external_id: str
    payload: Mapping[str, Any]
    provenance: Mapping[str, str]
    evidence_refs: tuple[str, ...]
    confidence: float
    captured_at_utc: str
    authority_ref: str
    persisted_state_ref: str
    round_trip_values: Mapping[str, Any]

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"ExternalAdapterRecord(adapter_id={self.adapter_id!r}, operation={self.operation!r}, domain={self.domain!r})"


@dataclass(frozen=True, slots=True)
class AdapterRoundTripResult:
    """Proof that external adapter conversion preserved required fields."""

    passed: bool
    record: ExternalAdapterRecord
    missing_keys: tuple[str, ...] = ()
    mismatched_keys: tuple[str, ...] = ()
    reasons: tuple[AdapterDecisionReason, ...] = ()

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"AdapterRoundTripResult(passed={self.passed!r}, record={self.record!r}, missing_keys={self.missing_keys!r})"


def build_authority_contract(payload: Mapping[str, Any]) -> AdapterAuthorityContract:
    """Build an adapter authority contract from a schema-shaped payload."""
    return AdapterAuthorityContract(
        adapter_id=str(payload["adapter_id"]),
        display_name=str(payload["display_name"]),
        tool_kind=str(payload["tool_kind"]),
        operations=tuple(AdapterOperation(value) for value in payload["operations"]),
        domains=tuple(AdapterDomain(value) for value in payload["domains"]),
        authority_mode=AuthorityMode(str(payload["authority_mode"])),
        workbench_authoritative=bool(payload["workbench_authoritative"]),
        lossiness=Lossiness(str(payload["lossiness"])),
        provenance_mapping=ProvenanceMapping(
            required_fields=tuple(str(value) for value in payload["provenance_mapping"]["required_fields"]),
            identifier_fields=tuple(
                str(value) for value in payload["provenance_mapping"].get("identifier_fields", DEFAULT_ROUND_TRIP_KEYS)
            ),
        ),
        conflict_behavior=ConflictBehavior(str(payload["conflict_behavior"])),
        privacy_policy=PrivacyPolicy(str(payload["privacy_policy"])),
        stale_data_policy=StaleDataPolicy(str(payload["stale_data_policy"])),
        required_permissions=tuple(str(value) for value in payload["required_permissions"]),
        minimum_confidence=float(payload.get("minimum_confidence", 0.8)),
        max_staleness_hours=int(payload.get("max_staleness_hours", 24)),
        evidence_required=bool(payload.get("evidence_required", True)),
        round_trip_keys=tuple(str(value) for value in payload.get("round_trip_keys", DEFAULT_ROUND_TRIP_KEYS)),
    )


def assess_authority(
    contract: AdapterAuthorityContract,
    exchange: AdapterExchange,
    *,
    now_utc: datetime | None = None,
) -> AdapterAuthorityDecision:
    """Authorize an exchange, failing closed for missing trust inputs.

    Args:
        contract: Contract value consumed by assess_authority().
        exchange: Exchange value consumed by assess_authority().
        now_utc: Now utc value consumed by assess_authority().

    Returns:
        AdapterAuthorityDecision value produced by assess_authority().
    """
    reasons: list[AdapterDecisionReason] = []
    _append_contract_scope_reasons(contract, exchange, reasons)
    _append_trust_signal_reasons(contract, exchange, reasons, now_utc)
    _append_privacy_reasons(contract, exchange, reasons)
    conflict_decision = _conflict_decision(contract, exchange, reasons)
    if conflict_decision is not None:
        return conflict_decision
    if reasons:
        return _rejected_authority_decision(contract, reasons)
    return AdapterAuthorityDecision(allowed=True, degraded=False, action="allow")


def _append_contract_scope_reasons(
    contract: AdapterAuthorityContract,
    exchange: AdapterExchange,
    reasons: list[AdapterDecisionReason],
) -> None:
    if exchange.operation not in contract.operations:
        reasons.append(AdapterDecisionReason.UNSUPPORTED_OPERATION)
    if exchange.domain not in contract.domains:
        reasons.append(AdapterDecisionReason.UNSUPPORTED_DOMAIN)
    permission_set = {permission for permission in exchange.permissions if permission.strip()}
    required_permissions = set(contract.required_permissions)
    required_permissions.add(f"operation:{exchange.operation.value}")
    if not required_permissions <= permission_set:
        reasons.append(AdapterDecisionReason.MISSING_PERMISSION)
    required_provenance = set(contract.provenance_mapping.required_fields)
    present_provenance = {key for key, value in exchange.provenance.items() if key.strip() and str(value).strip()}
    if not required_provenance <= present_provenance:
        reasons.append(AdapterDecisionReason.MISSING_PROVENANCE)


def _append_trust_signal_reasons(
    contract: AdapterAuthorityContract,
    exchange: AdapterExchange,
    reasons: list[AdapterDecisionReason],
    now_utc: datetime | None,
) -> None:
    if contract.evidence_required and not _non_empty_strings(exchange.evidence_refs):
        reasons.append(AdapterDecisionReason.MISSING_EVIDENCE)
    if not exchange.authority_ref.strip():
        reasons.append(AdapterDecisionReason.MISSING_AUTHORITY)
    if not exchange.persisted_state_ref.strip():
        reasons.append(AdapterDecisionReason.MISSING_PERSISTED_STATE)
    confidence = _coerce_confidence(exchange.confidence)
    if confidence is None:
        reasons.append(AdapterDecisionReason.MISSING_CONFIDENCE)
    elif confidence < contract.minimum_confidence:
        reasons.append(AdapterDecisionReason.LOW_CONFIDENCE)
    _append_staleness_reason(contract, exchange, reasons, now_utc)


def _append_staleness_reason(
    contract: AdapterAuthorityContract,
    exchange: AdapterExchange,
    reasons: list[AdapterDecisionReason],
    now_utc: datetime | None,
) -> None:
    captured_at = _parse_utc(exchange.captured_at_utc)
    if captured_at is None:
        reasons.append(AdapterDecisionReason.UNREADABLE_CAPTURE_TIME)
        return
    current = now_utc if now_utc is not None else datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    current = current.astimezone(timezone.utc)
    stale_after = current - timedelta(hours=contract.max_staleness_hours)
    future_slop = current + timedelta(minutes=5)
    if captured_at < stale_after or captured_at > future_slop:
        reasons.append(AdapterDecisionReason.STALE_PROVENANCE)


def _append_privacy_reasons(
    contract: AdapterAuthorityContract,
    exchange: AdapterExchange,
    reasons: list[AdapterDecisionReason],
) -> None:
    if exchange.direction is AdapterDirection.EXPORT and contract.privacy_policy is PrivacyPolicy.LOCAL_ONLY:
        reasons.append(AdapterDecisionReason.LOCAL_ONLY_EXPORT)
    if (
        exchange.direction is AdapterDirection.EXPORT
        and contract.privacy_policy is PrivacyPolicy.REDACTED_EXPORT
        and exchange.payload.get("redacted") is not True
    ):
        reasons.append(AdapterDecisionReason.UNREDACTED_EXPORT)


def _conflict_decision(
    contract: AdapterAuthorityContract,
    exchange: AdapterExchange,
    reasons: list[AdapterDecisionReason],
) -> AdapterAuthorityDecision | None:
    if contract.workbench_authoritative and exchange.payload.get("external_authoritative") is True:
        reasons.append(AdapterDecisionReason.EXTERNAL_AUTHORITY_CONFLICT)
    if exchange.payload.get("conflict") is not True:
        return None
    if contract.conflict_behavior is ConflictBehavior.REJECT:
        reasons.append(AdapterDecisionReason.CONFLICT_REJECTED)
        return None
    if contract.conflict_behavior is ConflictBehavior.PROPOSE_ONLY:
        return AdapterAuthorityDecision(
            allowed=False,
            degraded=True,
            action="propose_only",
            reasons=(AdapterDecisionReason.CONFLICT_REJECTED,),
        )
    return None


def _rejected_authority_decision(
    contract: AdapterAuthorityContract,
    reasons: list[AdapterDecisionReason],
) -> AdapterAuthorityDecision:
    action = "reject"
    if AdapterDecisionReason.STALE_PROVENANCE in reasons:
        if contract.stale_data_policy is StaleDataPolicy.REQUIRE_REFRESH:
            action = "refresh_required"
        elif contract.stale_data_policy is StaleDataPolicy.MARK_DEGRADED:
            action = "mark_degraded"
    return AdapterAuthorityDecision(allowed=False, degraded=True, action=action, reasons=tuple(dict.fromkeys(reasons)))


def export_adapter_record(
    contract: AdapterAuthorityContract,
    exchange: AdapterExchange,
    *,
    now_utc: datetime | None = None,
) -> ExternalAdapterRecord:
    """Create an outbound adapter record only after authority assessment passes.

    Args:
        contract: Contract value consumed by export_adapter_record().
        exchange: Exchange value consumed by export_adapter_record().
        now_utc: Now utc value consumed by export_adapter_record().

    Returns:
        ExternalAdapterRecord value produced by export_adapter_record().

    Raises:
        Exception: Propagates validation or runtime failures from the underlying operation.
    """
    decision = assess_authority(contract, exchange, now_utc=now_utc)
    if not decision.allowed:
        raise AdapterAuthorityError(decision)
    confidence = _coerce_confidence(exchange.confidence)
    assert confidence is not None
    return ExternalAdapterRecord(
        adapter_id=contract.adapter_id,
        operation=exchange.operation,
        domain=exchange.domain,
        workbench_id=exchange.workbench_id,
        external_id=exchange.external_id,
        payload=dict(exchange.payload),
        provenance=dict(exchange.provenance),
        evidence_refs=tuple(exchange.evidence_refs),
        confidence=confidence,
        captured_at_utc=exchange.captured_at_utc,
        authority_ref=exchange.authority_ref,
        persisted_state_ref=exchange.persisted_state_ref,
        round_trip_values=_round_trip_values(contract, exchange),
    )


def prove_round_trip(
    contract: AdapterAuthorityContract,
    exchange: AdapterExchange,
    *,
    now_utc: datetime | None = None,
) -> AdapterRoundTripResult:
    """Export and re-read an adapter record, proving required IDs survived.

    Args:
        contract: Contract value consumed by prove_round_trip().
        exchange: Exchange value consumed by prove_round_trip().
        now_utc: Now utc value consumed by prove_round_trip().

    Returns:
        AdapterRoundTripResult value produced by prove_round_trip().
    """
    record = export_adapter_record(contract, exchange, now_utc=now_utc)
    missing: list[str] = []
    mismatched: list[str] = []
    for key, expected in record.round_trip_values.items():
        actual = record.evidence_refs if key == ROUND_TRIP_EVIDENCE_KEY else record.payload.get(key)
        if actual in (None, "", ()):
            missing.append(key)
        elif actual != expected:
            mismatched.append(key)
    reasons = (AdapterDecisionReason.ROUND_TRIP_LOSS,) if missing or mismatched else ()
    return AdapterRoundTripResult(
        passed=not missing and not mismatched,
        record=record,
        missing_keys=tuple(missing),
        mismatched_keys=tuple(mismatched),
        reasons=reasons,
    )


def _round_trip_values(contract: AdapterAuthorityContract, exchange: AdapterExchange) -> dict[str, Any]:
    values = {key: exchange.payload.get(key) for key in contract.round_trip_keys}
    values[ROUND_TRIP_EVIDENCE_KEY] = tuple(exchange.evidence_refs)
    return values


def _require_text(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be non-empty")


def _require_string_tuple(values: tuple[str, ...], field_name: str) -> None:
    if not isinstance(values, tuple) or not values:
        raise ValueError(f"{field_name} must be a non-empty tuple")
    if not _non_empty_strings(values):
        raise ValueError(f"{field_name} must contain non-empty strings")


def _non_empty_strings(values: Sequence[str]) -> bool:
    return bool(values) and all(isinstance(value, str) and value.strip() for value in values)


def _coerce_confidence(value: float | int | str | None) -> float | None:
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        logger.warning("Handled recoverable failure before fallback.", exc_info=True)
        return None
    if not isfinite(confidence) or not 0 <= confidence <= 1:
        return None
    return confidence


def _parse_utc(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        logger.warning("Handled recoverable failure before fallback.", exc_info=True)
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
