"""Sensitive life-admin workflow contracts.

The project-id gate mirrors the Wave-13 source-card precedent: ids are
short, path-segment-safe strings and traversal markers fail closed before
any spine, source-card, or tool-card lookup.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum

from vetinari.security.path_canonicalizer import ProjectIdRejected as SharedProjectIdRejected
from vetinari.security.path_canonicalizer import canonicalize_project_id as _shared_canonicalize_project_id
from vetinari.workbench.rigor import RigorLevel


class LifeAdminProjectIdRejected(ValueError):
    """Raised when a project id is not canonical and safe for lookup."""


class SensitiveWorkflowError(Exception):
    """Raised when sensitive-workflow policy state cannot be trusted."""


class SensitiveDomainKind(str, Enum):
    """Runtime contract for SensitiveDomainKind."""

    TAX = "tax"
    FINANCE = "finance"
    LEGAL = "legal"
    MEDICAL = "medical"
    EMPLOYMENT = "employment"
    HOUSING = "housing"
    SAFETY = "safety"
    HOUSEHOLD_PLANNING = "household_planning"
    DOCUMENT_ORGANIZATION = "document_organization"
    MEETING_PREP = "meeting_prep"
    PURCHASE_DECISION = "purchase_decision"
    APPOINTMENT = "appointment"
    GENERAL_PROFESSIONAL = "general_professional"


class WorkflowOutcomeKind(str, Enum):
    """Runtime contract for WorkflowOutcomeKind."""

    CHECKLIST = "checklist"
    DOCUMENT_PACKET = "document_packet"
    PROFESSIONAL_MEMO = "professional_memo"
    SOURCE_BACKED_NOTE = "source_backed_note"
    REMINDER = "reminder"
    EVIDENCE_NOTEBOOK_ENTRY = "evidence_notebook_entry"
    QUESTIONS_FOR_PROFESSIONAL = "questions_for_professional"
    ORGANIZE_DOCUMENTS = "organize_documents"
    EXPLAIN_CONCEPT = "explain_concept"


class WorkflowDecisionKind(str, Enum):
    """Runtime contract for WorkflowDecisionKind."""

    ALLOWED = "allowed"
    DENIED_MISSING_CONTEXT = "denied_missing_context"
    DENIED_AUTHORITY_REQUIRED = "denied_authority_required"
    DENIED_EVIDENCE_REQUIRED = "denied_evidence_required"
    DENIED_FRESHNESS_FAILED = "denied_freshness_failed"
    DENIED_UNKNOWN_JURISDICTION = "denied_unknown_jurisdiction"
    DENIED_PROMOTION_BLOCKED = "denied_promotion_blocked"
    DEGRADED_UNREADABLE_POLICY = "degraded_unreadable_policy"


@dataclass(frozen=True, slots=True)
class SensitiveWorkflowRequest:
    """Request to evaluate a professional or life-admin sensitive workflow."""

    project_id: str
    lens_id: str
    sensitive_domain_kind: SensitiveDomainKind | str
    workflow_outcome_kind: WorkflowOutcomeKind | str
    requested_by: str
    requested_at_utc: str
    jurisdiction: str | None = None
    tax_year: int | None = None
    document_refs: tuple[str, ...] = ()
    authority_ref: str = ""
    evidence_refs: tuple[str, ...] = ()
    claim_kind: str | None = None
    policy_profile_id: str = "default"
    correlation_id: str | None = None
    notes_text: str = ""

    def __post_init__(self) -> None:
        _canonicalize_project_id(self.project_id)
        _require_non_empty(self.lens_id, "lens_id")
        _require_non_empty(self.requested_by, "requested_by")
        _parse_utc(self.requested_at_utc, "requested_at_utc")
        if self.tax_year is not None and not 1900 <= self.tax_year <= 2100:
            raise ValueError("tax_year must be between 1900 and 2100")
        object.__setattr__(self, "sensitive_domain_kind", SensitiveDomainKind(self.sensitive_domain_kind))
        object.__setattr__(self, "workflow_outcome_kind", WorkflowOutcomeKind(self.workflow_outcome_kind))
        object.__setattr__(self, "document_refs", _string_tuple(self.document_refs))
        object.__setattr__(self, "evidence_refs", _string_tuple(self.evidence_refs))

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"SensitiveWorkflowRequest(project_id={self.project_id!r}, lens_id={self.lens_id!r}, sensitive_domain_kind={self.sensitive_domain_kind!r})"


@dataclass(frozen=True, slots=True)
class SensitiveWorkflowDecision:
    """Decision returned by the fail-closed sensitive-workflow runtime."""

    decision_id: str
    request_correlation_id: str
    allowed: bool
    decision_kind: WorkflowDecisionKind | str
    reasons: tuple[str, ...]
    denial_reasons: tuple[str, ...]
    missing_context: tuple[str, ...]
    degraded: bool
    rigor_required: RigorLevel | str
    mode_lens_id: str
    policy_explanation_ref: str
    decided_at_utc: str
    promoted_artifact_kind: str | None = None
    promoted_artifact_id: str | None = None
    staleness_action: str | None = None
    project_id: str = ""
    workflow_outcome_kind: WorkflowOutcomeKind | str | None = None
    requested_by: str = ""
    authority_ref: str = ""
    evidence_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_non_empty(self.decision_id, "decision_id")
        _require_non_empty(self.request_correlation_id, "request_correlation_id")
        _parse_utc(self.decided_at_utc, "decided_at_utc")
        object.__setattr__(self, "decision_kind", WorkflowDecisionKind(self.decision_kind))
        object.__setattr__(self, "rigor_required", RigorLevel(self.rigor_required))
        object.__setattr__(self, "reasons", _string_tuple(self.reasons))
        object.__setattr__(self, "denial_reasons", _string_tuple(self.denial_reasons))
        object.__setattr__(self, "missing_context", _string_tuple(self.missing_context))
        object.__setattr__(self, "evidence_refs", _string_tuple(self.evidence_refs))
        if self.workflow_outcome_kind is not None:
            object.__setattr__(self, "workflow_outcome_kind", WorkflowOutcomeKind(self.workflow_outcome_kind))
        if self.allowed and (self.denial_reasons or self.missing_context):
            raise ValueError("allowed decisions cannot include denial_reasons or missing_context")
        if not self.allowed and not (self.denial_reasons or self.missing_context):
            raise ValueError("denied decisions require denial_reasons or missing_context")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"SensitiveWorkflowDecision(decision_id={self.decision_id!r}, request_correlation_id={self.request_correlation_id!r}, allowed={self.allowed!r})"


def _canonicalize_project_id(raw: str) -> str:
    """Return a canonical project id or fail closed before filesystem/spine use."""
    try:
        return _shared_canonicalize_project_id(raw)
    except SharedProjectIdRejected as exc:
        raise LifeAdminProjectIdRejected(str(exc)) from exc


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
    "LifeAdminProjectIdRejected",
    "SensitiveDomainKind",
    "SensitiveWorkflowDecision",
    "SensitiveWorkflowError",
    "SensitiveWorkflowRequest",
    "WorkflowDecisionKind",
    "WorkflowOutcomeKind",
    "_canonicalize_project_id",
]
