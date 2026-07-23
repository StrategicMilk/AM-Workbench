"""Approval-chain request and decision records."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from vetinari.workbench.governance_modes.contracts import GovernanceMode
from vetinari.workbench.policy.verdicts import ActionInput, EvidenceLink, RiskDomain
from vetinari.workbench.readiness import FeatureGate

SCHEMA_VERSION = "1.0"


class ApprovalChainError(RuntimeError):
    """Fail-closed approval-chain error."""


class ApprovalChainOutcome(str, Enum):
    """Final approval-chain outcomes."""

    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_HUMAN_APPROVAL = "require_human_approval"


class ApprovalChainReason(str, Enum):
    """Stable approval-chain reason codes."""

    CAPABILITY_CLASSIFIED = "capability_classified"
    POLICY_STATE_UNREADABLE = "policy_state_unreadable"
    POLICY_HARD_DENY = "policy_hard_deny"
    PROTECTED_PATH = "protected_path"
    DESTRUCTIVE_ACTION = "destructive_action"
    DLP_RISK = "dlp_risk"
    TOOL_PIN_UNVERIFIED = "tool_pin_unverified"
    READINESS_UNAVAILABLE = "readiness_unavailable"
    READINESS_BLOCKED = "readiness_blocked"
    GOVERNANCE_UNAVAILABLE = "governance_unavailable"
    GOVERNANCE_BLOCKED = "governance_blocked"
    SESSION_ALLOW = "session_allow"
    HUMAN_APPROVAL = "human_approval"
    DENY_BY_DEFAULT = "deny_by_default"


class ApprovalChannel(str, Enum):
    """Channels that can request equivalent approval decisions."""

    DESKTOP = "desktop"
    MOBILE = "mobile"
    AUTOMATION = "automation"
    NOTIFICATION = "notification"
    RECEIPT = "receipt"
    CLI = "cli"


@dataclass(frozen=True, slots=True)
class ApprovalChainStep:
    """One ordered resolver step."""

    name: str
    status: str
    reason: str
    outcome: str | None = None
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe step payload.

        Returns:
            dict[str, Any] value produced by to_dict().
        """
        payload: dict[str, Any] = {
            "name": self.name,
            "status": self.status,
            "reason": self.reason,
            "outcome": self.outcome,
            "detail": self.detail,
        }
        return payload

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"ApprovalChainStep(name={self.name!r}, status={self.status!r}, reason={self.reason!r})"


@dataclass(frozen=True, slots=True)
class ApprovalChainRequest:
    """Request facts consumed by the ordered approval-chain resolver."""

    project_id: str
    session_id: str
    channel: ApprovalChannel | str
    action_id: str
    action_type: str
    actor_id: str
    run_id: str
    risk_domain: RiskDomain | str
    summary: str
    action_fingerprint: str = ""
    evidence_links: tuple[EvidenceLink | dict[str, Any], ...] = ()
    authority_refs: tuple[str, ...] = ()
    target_paths: tuple[str, ...] = ()
    approval_sources: tuple[str, ...] = ()
    readiness_signals: dict[str, Any] | None = None
    readiness_feature: FeatureGate | str = FeatureGate.AUTOMATION_ADMISSION
    governance_mode: GovernanceMode | str = GovernanceMode.STRICT
    governance_available: bool = False
    hard_deny: bool = False
    destructive: bool = False
    dlp_risk: bool = False
    requires_tool_pin: bool = False
    tool_pin_verified: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        channel = _coerce_channel(self.channel)
        object.__setattr__(self, "channel", channel)
        object.__setattr__(self, "risk_domain", _coerce_enum(RiskDomain, self.risk_domain))
        object.__setattr__(self, "readiness_feature", _coerce_enum(FeatureGate, self.readiness_feature))
        object.__setattr__(self, "governance_mode", _coerce_enum(GovernanceMode, self.governance_mode))
        object.__setattr__(self, "evidence_links", tuple(_coerce_evidence_link(link) for link in self.evidence_links))
        object.__setattr__(self, "authority_refs", _clean_tuple(self.authority_refs))
        object.__setattr__(self, "target_paths", _clean_tuple(self.target_paths))
        object.__setattr__(self, "approval_sources", _clean_tuple(self.approval_sources))
        for field_name in ("project_id", "session_id", "action_id", "action_type", "actor_id", "run_id", "summary"):
            if not str(getattr(self, field_name)).strip():
                raise ApprovalChainError(f"{field_name} is required")

    @property
    def fingerprint(self) -> str:
        """Return the caller-provided or material action fingerprint."""
        if self.action_fingerprint.strip():
            return self.action_fingerprint.strip()
        material = {
            "project_id": self.project_id,
            "action_id": self.action_id,
            "action_type": self.action_type,
            "risk_domain": self.risk_domain.value,
            "summary": self.summary,
            "target_paths": list(self.target_paths),
        }
        encoded = json.dumps(material, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def to_action_input(self) -> ActionInput:
        """Convert to the existing policy-verdict classifier input."""
        return ActionInput(
            action_id=self.action_id,
            action_type=self.action_type,
            actor_id=self.actor_id,
            run_id=self.run_id,
            risk_domain=self.risk_domain,
            summary=self.summary,
            evidence_links=self.evidence_links,
            authority_refs=self.authority_refs,
            details=self.details,
            metadata={**self.metadata, "approval_chain_fingerprint": self.fingerprint},
        )

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"ApprovalChainRequest(project_id={self.project_id!r}, session_id={self.session_id!r}, channel={self.channel!r})"


@dataclass(frozen=True, slots=True)
class ApprovalChainDecision:
    """Schema-ready approval-chain decision."""

    decision_id: str
    schema_version: str
    project_id: str
    session_id: str
    action_id: str
    action_fingerprint: str
    channel: str
    outcome: ApprovalChainOutcome
    allowed: bool
    human_approval_required: bool
    matched_step: str
    fallback_rule: str
    ordered_trace: tuple[ApprovalChainStep, ...]
    receipt_payload: dict[str, Any]
    rendered_explanation: str
    decided_at_utc: str

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe decision payload."""
        return {
            "decision_id": self.decision_id,
            "schema_version": self.schema_version,
            "project_id": self.project_id,
            "session_id": self.session_id,
            "action_id": self.action_id,
            "action_fingerprint": self.action_fingerprint,
            "channel": self.channel,
            "outcome": self.outcome.value,
            "allowed": self.allowed,
            "human_approval_required": self.human_approval_required,
            "matched_step": self.matched_step,
            "fallback_rule": self.fallback_rule,
            "ordered_trace": [step.to_dict() for step in self.ordered_trace],
            "receipt_payload": self.receipt_payload,
            "rendered_explanation": self.rendered_explanation,
            "decided_at_utc": self.decided_at_utc,
        }

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"ApprovalChainDecision(decision_id={self.decision_id!r}, schema_version={self.schema_version!r}, project_id={self.project_id!r})"


@dataclass(frozen=True, slots=True)
class _SessionAllowGrant:
    project_id: str
    session_id: str
    channel: str
    action_fingerprint: str
    expires_at_utc: str
    granted_at_utc: str

    def expired(self, now: datetime) -> bool:
        return _parse_utc(self.expires_at_utc) <= now

    def to_dict(self) -> dict[str, str]:
        return {
            "project_id": self.project_id,
            "session_id": self.session_id,
            "channel": self.channel,
            "action_fingerprint": self.action_fingerprint,
            "expires_at_utc": self.expires_at_utc,
            "granted_at_utc": self.granted_at_utc,
        }

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"_SessionAllowGrant(project_id={self.project_id!r}, session_id={self.session_id!r}, channel={self.channel!r})"


def _coerce_evidence_link(value: EvidenceLink | dict[str, Any]) -> EvidenceLink:
    if isinstance(value, EvidenceLink):
        return value
    if isinstance(value, dict):
        return EvidenceLink(
            evidence_id=str(value.get("evidence_id", "")),
            kind=str(value.get("kind", "")),
            ref=str(value.get("ref", "")),
            summary=str(value.get("summary", "")),
        )
    raise ApprovalChainError("evidence_links must contain EvidenceLink values or mappings")


def _coerce_channel(value: ApprovalChannel | str) -> ApprovalChannel:
    if isinstance(value, ApprovalChannel):
        return value
    return _coerce_enum(ApprovalChannel, value)


def _coerce_enum(enum_type: type[Enum], value: Enum | str) -> Any:
    raw_value = value.value if isinstance(value, Enum) else value
    return enum_type(raw_value)


def _clean_tuple(values: tuple[str, ...] | list[str] | Any) -> tuple[str, ...]:
    if values is None:
        return ()
    if isinstance(values, (str, bytes)):
        return (str(values).strip(),) if str(values).strip() else ()
    return tuple(str(value).strip() for value in values if str(value).strip())


def _allow_key(project_id: str, session_id: str, channel: str, action_fingerprint: str) -> tuple[str, str, str, str]:
    return (project_id, session_id, channel, action_fingerprint)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
