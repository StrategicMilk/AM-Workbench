"""Private utility helpers for personalization contract validation."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any

CONTRACT_PUBLIC_EXPORTS = [
    "BLOCKER_CONFLICT",
    "BLOCKER_DELETE_REQUESTED",
    "BLOCKER_DOWNSTREAM_GATE_FAILED",
    "BLOCKER_DOWNSTREAM_GATE_MISSING",
    "BLOCKER_EXPIRED",
    "BLOCKER_MISSING_ALLOWED_USE",
    "BLOCKER_MISSING_AUDIT_TRAIL",
    "BLOCKER_MISSING_CONFIDENCE",
    "BLOCKER_MISSING_CONSENT",
    "BLOCKER_MISSING_DEDUPE",
    "BLOCKER_MISSING_DEPENDENCY",
    "BLOCKER_MISSING_PROVENANCE",
    "BLOCKER_MISSING_REDACTION",
    "BLOCKER_MISSING_RETENTION",
    "BLOCKER_MISSING_REVOCATION",
    "BLOCKER_MISSING_SENSITIVITY_REVIEW",
    "BLOCKER_MISSING_SPLIT_FIREWALL",
    "BLOCKER_OPAQUE_MODEL_WEIGHTS",
    "BLOCKER_PROFILE_FACT",
    "BLOCKER_RAW_USER_LOG",
    "BLOCKER_REVOKED",
    "BLOCKER_SENSITIVE_CONTEXT",
    "SCHEMA_VERSION",
    "AllowedUse",
    "AuditTrailRef",
    "CandidateInputKind",
    "DependencyGateRefs",
    "PersonalizationContractError",
    "PersonalizationDecision",
    "PersonalizationDecisionStatus",
    "ProfileCard",
    "ProfileRecordKind",
    "ProfileRecordStatus",
    "ProvenanceRef",
    "RetentionPolicyRef",
    "TrainingCandidate",
    "TrainingGovernanceProof",
    "TrainingPromotionTarget",
    "evaluate_profile_card",
    "evaluate_training_candidate",
    "recovery_needed_decision",
    "to_jsonable",
]


def to_jsonable(value: Any) -> Any:
    """Return JSON-compatible values while preserving enum strings.

    Returns:
        Value produced for the caller.
    """
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value) and not isinstance(value, type):
        return {key: to_jsonable(item) for key, item in asdict(value).items()}
    if isinstance(value, tuple):
        return [to_jsonable(item) for item in value]
    if isinstance(value, list):
        return [to_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): to_jsonable(item) for key, item in value.items()}
    return value


def _normalize_now(value: datetime | None) -> datetime:
    now = value or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc)


def _parse_utc(value: str, field_name: str) -> datetime:
    from vetinari.workbench.personalization.contracts import PersonalizationContractError

    _require_text(value, field_name)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PersonalizationContractError(f"{field_name} must be ISO-8601") from exc
    if parsed.tzinfo is None:
        raise PersonalizationContractError(f"{field_name} must include timezone")
    return parsed.astimezone(timezone.utc)


def _require_text(value: object, field_name: str) -> None:
    from vetinari.workbench.personalization.contracts import PersonalizationContractError

    if not _has_text(value):
        raise PersonalizationContractError(f"{field_name} must be non-empty")


def _has_text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _require_confidence(value: object, field_name: str) -> None:
    from vetinari.workbench.personalization.contracts import PersonalizationContractError

    if not isinstance(value, int | float) or not 0.0 < float(value) <= 1.0:
        raise PersonalizationContractError(f"{field_name} must be > 0.0 and <= 1.0")


def _require_tuple_type(
    values: tuple[object, ...],
    expected_type: type[object],
    field_name: str,
    *,
    allow_empty: bool = False,
) -> None:
    from vetinari.workbench.personalization.contracts import PersonalizationContractError

    if not isinstance(values, tuple) or (not allow_empty and not values):
        raise PersonalizationContractError(f"{field_name} must be a non-empty tuple")
    if any(not isinstance(value, expected_type) for value in values):
        raise PersonalizationContractError(f"{field_name} must contain {expected_type.__name__} values")
