"""Channel-neutral approval risk context for Workbench actions."""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

from vetinari.workbench.policy.verdicts import ActionVerdict, EvidenceLink, VerdictValue
from vetinari.workbench.policy_explainability import PolicyExplanation

logger = logging.getLogger(__name__)


SCHEMA_VERSION = "1.0"


class RiskContextEntryPoint(str, Enum):
    """Entry points that can launch an equivalent approval request."""

    DESKTOP_UI = "desktop_ui"
    MOBILE = "mobile"
    AUTOMATION = "automation"
    AGENT_WATCHER = "agent_watcher"
    CLI = "cli"
    IMPORTED_WORKFLOW = "imported_workflow"


class RiskRollbackStatus(str, Enum):
    """Rollback state that must be explicit before approval reuse."""

    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    PARTIAL = "partial"
    NOT_APPLICABLE = "not_applicable"
    UNKNOWN = "unknown"


class RiskContextDecision(str, Enum):
    """Approval treatment derived from policy, rollback, and explanation state."""

    ALLOW = "allow"
    WARN = "warn"
    REQUIRE_APPROVAL = "require_approval"
    DENY = "deny"
    DEGRADED = "degraded"


class RiskContextErrorReason(str, Enum):
    """Stable fail-closed reasons for corrupt or incomplete risk context."""

    MISSING_VERDICT = "missing_verdict"
    MISSING_AFFECTED_ASSETS = "missing_affected_assets"
    MISSING_AUTHORITY = "missing_authority"
    MISSING_ROLLBACK_STATUS = "missing_rollback_status"
    MISSING_EXPLANATION = "missing_explanation"
    MISSING_EVIDENCE = "missing_evidence"
    INVALID_ENTRY_POINT = "invalid_entry_point"
    UNREADABLE_PRIOR_DECISION = "unreadable_prior_decision"
    SCHEMA_VERSION_MISMATCH = "schema_version_mismatch"


class RiskContextError(ValueError):
    """Typed fail-closed signal for risk-context construction."""

    def __init__(self, reason_code: RiskContextErrorReason, message: str = "") -> None:
        self.reason_code = reason_code
        self.message = message or reason_code.value
        super().__init__(str(self))

    def __str__(self) -> str:
        return f"RiskContextError[{self.reason_code.value}]: {self.message}"


@dataclass(frozen=True, slots=True)
class AffectedAsset:
    """One asset affected by the proposed action."""

    asset_id: str
    kind: str
    project_scope: str
    operation: str
    display_label: str

    def __post_init__(self) -> None:
        _require_text(self.asset_id, "asset_id", RiskContextErrorReason.MISSING_AFFECTED_ASSETS)
        _require_text(self.kind, "kind", RiskContextErrorReason.MISSING_AFFECTED_ASSETS)
        _require_text(self.project_scope, "project_scope", RiskContextErrorReason.MISSING_AFFECTED_ASSETS)
        _require_text(self.operation, "operation", RiskContextErrorReason.MISSING_AFFECTED_ASSETS)
        _require_text(self.display_label, "display_label", RiskContextErrorReason.MISSING_AFFECTED_ASSETS)

    def to_payload(self) -> dict[str, str]:
        return asdict(self)

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"AffectedAsset(asset_id={self.asset_id!r}, kind={self.kind!r}, project_scope={self.project_scope!r})"


@dataclass(frozen=True, slots=True)
class ToolAuthoritySummary:
    """Authority and capability context for the tool surface involved."""

    tool_surface_id: str
    authority_refs: tuple[str, ...]
    capability_pack_refs: tuple[str, ...]
    capability_diff_state: str

    def __post_init__(self) -> None:
        _require_text(self.tool_surface_id, "tool_surface_id", RiskContextErrorReason.MISSING_AUTHORITY)
        authority_refs = _string_tuple(self.authority_refs)
        capability_pack_refs = _string_tuple(self.capability_pack_refs)
        if not authority_refs:
            raise RiskContextError(RiskContextErrorReason.MISSING_AUTHORITY, "authority_refs are required")
        _require_text(self.capability_diff_state, "capability_diff_state", RiskContextErrorReason.MISSING_AUTHORITY)
        object.__setattr__(self, "authority_refs", authority_refs)
        object.__setattr__(self, "capability_pack_refs", capability_pack_refs)

    def to_payload(self) -> dict[str, object]:
        return {
            "tool_surface_id": self.tool_surface_id,
            "authority_refs": list(self.authority_refs),
            "capability_pack_refs": list(self.capability_pack_refs),
            "capability_diff_state": self.capability_diff_state,
        }

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"ToolAuthoritySummary(tool_surface_id={self.tool_surface_id!r}, authority_refs={self.authority_refs!r}, capability_pack_refs={self.capability_pack_refs!r})"


@dataclass(frozen=True, slots=True)
class PriorDecisionSummary:
    """Prior similar approval or rejection used only for comparison."""

    decision_id: str
    material_fingerprint: str
    outcome: str
    decided_by: str
    decided_at_utc: str

    def __post_init__(self) -> None:
        for field_name in ("decision_id", "material_fingerprint", "outcome", "decided_by", "decided_at_utc"):
            _require_text(
                getattr(self, field_name),
                field_name,
                RiskContextErrorReason.UNREADABLE_PRIOR_DECISION,
            )

    def to_payload(self) -> dict[str, str]:
        return asdict(self)

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"PriorDecisionSummary(decision_id={self.decision_id!r}, material_fingerprint={self.material_fingerprint!r}, outcome={self.outcome!r})"


@dataclass(frozen=True, slots=True)
class RiskContext:
    """Canonical risk context shared by later approval-channel integrations."""

    context_id: str
    action_id: str
    entry_point: RiskContextEntryPoint
    decision: RiskContextDecision
    material_fingerprint: str
    verdict_snapshot: dict[str, Any]
    affected_assets: tuple[AffectedAsset, ...]
    tool_authority: ToolAuthoritySummary
    rollback_status: RiskRollbackStatus
    recovery_note: str
    prior_decisions: tuple[PriorDecisionSummary, ...]
    explanation_snapshot: dict[str, Any]
    evidence_links: tuple[dict[str, str], ...]
    replay_metadata: dict[str, Any]
    generated_at_utc: str
    audit_payload: dict[str, Any]
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise RiskContextError(RiskContextErrorReason.SCHEMA_VERSION_MISMATCH, f"expected {SCHEMA_VERSION}")
        _require_text(self.context_id, "context_id", RiskContextErrorReason.MISSING_VERDICT)
        _require_text(self.action_id, "action_id", RiskContextErrorReason.MISSING_VERDICT)
        _require_text(self.material_fingerprint, "material_fingerprint", RiskContextErrorReason.MISSING_VERDICT)
        object.__setattr__(self, "entry_point", RiskContextEntryPoint(self.entry_point))
        object.__setattr__(self, "decision", RiskContextDecision(self.decision))
        object.__setattr__(self, "rollback_status", RiskRollbackStatus(self.rollback_status))
        object.__setattr__(self, "affected_assets", tuple(self.affected_assets))
        object.__setattr__(self, "prior_decisions", tuple(self.prior_decisions))
        object.__setattr__(self, "evidence_links", tuple(dict(link) for link in self.evidence_links))

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"RiskContext(context_id={self.context_id!r}, action_id={self.action_id!r}, entry_point={self.entry_point!r})"


def build_risk_context(
    *,
    context_id: str,
    entry_point: RiskContextEntryPoint | str,
    verdict: ActionVerdict | None,
    affected_assets: tuple[AffectedAsset, ...],
    tool_authority: ToolAuthoritySummary | None,
    rollback_status: RiskRollbackStatus | str | None,
    explanation: PolicyExplanation | None,
    recovery_note: str,
    prior_decisions: tuple[PriorDecisionSummary, ...] = (),
    generated_at_utc: str | None = None,
    audit_payload: dict[str, Any] | None = None,
    on_error: Literal["raise", "degrade"] = "raise",
) -> RiskContext:
    """Join upstream safety artifacts into one channel-neutral risk context.

    Returns:
        Newly constructed risk context value.

    Raises:
        Exception: Propagates validation or runtime failures from the underlying operation.
    """
    try:
        _validate_inputs(verdict, affected_assets, tool_authority, rollback_status, explanation, prior_decisions)
        assert verdict is not None
        assert tool_authority is not None
        assert rollback_status is not None
        assert explanation is not None
        entry = _entry_point_or_raise(entry_point)
        rollback = RiskRollbackStatus(rollback_status)
        verdict_snapshot = _verdict_snapshot(verdict)
        explanation_snapshot = _explanation_snapshot(explanation)
        evidence_links = _evidence_payload(verdict.evidence_links)
        decision = _risk_decision(verdict, tool_authority, rollback, affected_assets, explanation)
        replay_metadata = dict(verdict_snapshot["replay_metadata"])
        generated = generated_at_utc or _utc_now_iso()
        material_fingerprint = _material_fingerprint(
            verdict_snapshot=verdict_snapshot,
            affected_assets=affected_assets,
            tool_authority=tool_authority,
            rollback_status=rollback,
            prior_decisions=prior_decisions,
            explanation_snapshot=explanation_snapshot,
        )
        return RiskContext(
            context_id=context_id,
            action_id=verdict.action_id,
            entry_point=entry,
            decision=decision,
            material_fingerprint=material_fingerprint,
            verdict_snapshot=verdict_snapshot,
            affected_assets=tuple(affected_assets),
            tool_authority=tool_authority,
            rollback_status=rollback,
            recovery_note=recovery_note,
            prior_decisions=tuple(prior_decisions),
            explanation_snapshot=explanation_snapshot,
            evidence_links=evidence_links,
            replay_metadata=replay_metadata,
            generated_at_utc=generated,
            audit_payload=audit_payload or {},
        )
    except (RiskContextError, ValueError):
        logger.warning("Handled recoverable failure before fallback.", exc_info=True)
        if on_error == "raise":
            raise
        return _degraded_context(context_id=context_id, entry_point=entry_point, recovery_note=recovery_note)


def render_approval_risk_frame(context: RiskContext) -> dict[str, object]:
    """Render a strict JSON-serializable risk frame for approval prompts.

    Returns:
        dict[str, object] value produced by render_approval_risk_frame().
    """
    risk_frame = {
        "decision": context.decision.value,
        "material_fingerprint": context.material_fingerprint,
        "verdict": dict(context.verdict_snapshot),
        "affected_assets": [asset.to_payload() for asset in context.affected_assets],
        "tool_authority": context.tool_authority.to_payload(),
        "rollback": {
            "status": context.rollback_status.value,
            "recovery_note": context.recovery_note,
        },
        "prior_decisions": [decision.to_payload() for decision in context.prior_decisions],
        "explanation": dict(context.explanation_snapshot),
        "evidence_links": [dict(link) for link in context.evidence_links],
        "replay_metadata": dict(context.replay_metadata),
        "audit_payload": dict(context.audit_payload),
    }
    return {
        "schema_version": context.schema_version,
        "context_id": context.context_id,
        "action_id": context.action_id,
        "entry_point": context.entry_point.value,
        "generated_at_utc": context.generated_at_utc,
        "risk_frame": risk_frame,
    }


def _validate_inputs(
    verdict: ActionVerdict | None,
    affected_assets: tuple[AffectedAsset, ...],
    tool_authority: ToolAuthoritySummary | None,
    rollback_status: RiskRollbackStatus | str | None,
    explanation: PolicyExplanation | None,
    prior_decisions: tuple[PriorDecisionSummary, ...],
) -> None:
    if verdict is None:
        raise RiskContextError(RiskContextErrorReason.MISSING_VERDICT)
    if verdict.schema_version != SCHEMA_VERSION:
        raise RiskContextError(RiskContextErrorReason.SCHEMA_VERSION_MISMATCH)
    if not verdict.evidence_links:
        raise RiskContextError(RiskContextErrorReason.MISSING_EVIDENCE, "verdict evidence_links are required")
    if not affected_assets:
        raise RiskContextError(RiskContextErrorReason.MISSING_AFFECTED_ASSETS)
    if tool_authority is None:
        raise RiskContextError(RiskContextErrorReason.MISSING_AUTHORITY)
    if rollback_status is None:
        raise RiskContextError(RiskContextErrorReason.MISSING_ROLLBACK_STATUS)
    rollback = RiskRollbackStatus(rollback_status)
    if rollback is RiskRollbackStatus.UNKNOWN:
        raise RiskContextError(RiskContextErrorReason.MISSING_ROLLBACK_STATUS, "rollback status cannot be unknown")
    if explanation is None:
        raise RiskContextError(RiskContextErrorReason.MISSING_EXPLANATION)
    for decision in prior_decisions:
        if not isinstance(decision, PriorDecisionSummary):
            raise RiskContextError(RiskContextErrorReason.UNREADABLE_PRIOR_DECISION)


def _risk_decision(
    verdict: ActionVerdict,
    tool_authority: ToolAuthoritySummary,
    rollback_status: RiskRollbackStatus,
    affected_assets: tuple[AffectedAsset, ...],
    explanation: PolicyExplanation,
) -> RiskContextDecision:
    if explanation.degraded:
        return RiskContextDecision.DEGRADED
    if not explanation.allowed or verdict.value is VerdictValue.BLOCK:
        return RiskContextDecision.DENY
    if verdict.value is VerdictValue.ESCALATE:
        return RiskContextDecision.REQUIRE_APPROVAL
    if rollback_status in {RiskRollbackStatus.UNAVAILABLE, RiskRollbackStatus.PARTIAL}:
        return RiskContextDecision.REQUIRE_APPROVAL
    if tool_authority.capability_diff_state not in {"unchanged", "compatible"}:
        return RiskContextDecision.REQUIRE_APPROVAL
    if any(asset.operation in {"delete", "destructive", "external_publish"} for asset in affected_assets):
        return RiskContextDecision.REQUIRE_APPROVAL
    if verdict.value is VerdictValue.WARN:
        return RiskContextDecision.WARN
    return RiskContextDecision.ALLOW


def _material_fingerprint(
    *,
    verdict_snapshot: dict[str, Any],
    affected_assets: tuple[AffectedAsset, ...],
    tool_authority: ToolAuthoritySummary,
    rollback_status: RiskRollbackStatus,
    prior_decisions: tuple[PriorDecisionSummary, ...],
    explanation_snapshot: dict[str, Any],
) -> str:
    material = {
        "verdict": {
            key: verdict_snapshot[key] for key in ("value", "mode", "risk_domain", "reason_code", "policy_version")
        },
        "affected_assets": sorted(
            (
                {
                    "asset_id": asset.asset_id,
                    "project_scope": asset.project_scope,
                    "operation": asset.operation,
                }
                for asset in affected_assets
            ),
            key=lambda row: (row["asset_id"], row["operation"]),
        ),
        "authority": {
            "authority_refs": sorted(tool_authority.authority_refs),
            "capability_pack_refs": sorted(tool_authority.capability_pack_refs),
            "capability_diff_state": tool_authority.capability_diff_state,
        },
        "rollback_status": rollback_status.value,
        "prior_decisions": sorted(
            (
                {
                    "material_fingerprint": decision.material_fingerprint,
                    "outcome": decision.outcome,
                }
                for decision in prior_decisions
            ),
            key=lambda row: (row["material_fingerprint"], row["outcome"]),
        ),
        "explanation": {
            "decision_kind": explanation_snapshot["decision_kind"],
            "allowed": explanation_snapshot["allowed"],
            "degraded": explanation_snapshot["degraded"],
            "denial_reasons": sorted(explanation_snapshot["denial_reasons"]),
        },
    }
    encoded = json.dumps(material, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _verdict_snapshot(verdict: ActionVerdict) -> dict[str, Any]:
    return verdict.to_schema_payload()


def _explanation_snapshot(explanation: PolicyExplanation) -> dict[str, Any]:
    return {
        "allowed": explanation.allowed,
        "policy_id": explanation.policy_id,
        "policy_source": explanation.policy_source,
        "decision_kind": explanation.decision_kind,
        "reasons": list(explanation.reasons),
        "denial_reasons": list(explanation.denial_reasons),
        "exposures": asdict(explanation.exposures),
        "budget": asdict(explanation.budget),
        "trace": asdict(explanation.trace),
        "failure_behavior": explanation.failure_behavior,
        "source_caveats": list(explanation.source_caveats),
        "tool_caveats": list(explanation.tool_caveats),
        "capability_pack_status": explanation.capability_pack_status,
        "degraded": explanation.degraded,
    }


def _evidence_payload(evidence_links: tuple[EvidenceLink, ...]) -> tuple[dict[str, str], ...]:
    return tuple(
        {
            "evidence_id": link.evidence_id,
            "kind": link.kind,
            "ref": link.ref,
            "summary": link.summary,
        }
        for link in evidence_links
    )


def _degraded_context(
    *,
    context_id: str,
    entry_point: RiskContextEntryPoint | str,
    recovery_note: str,
) -> RiskContext:
    generated = _utc_now_iso()
    entry = _safe_degraded_entry_point(entry_point)
    authority = ToolAuthoritySummary(
        tool_surface_id="unavailable",
        authority_refs=("unavailable",),
        capability_pack_refs=(),
        capability_diff_state="unknown",
    )
    return RiskContext(
        context_id=context_id or "unavailable",
        action_id="unavailable",
        entry_point=entry,
        decision=RiskContextDecision.DEGRADED,
        material_fingerprint="degraded",
        verdict_snapshot=_degraded_verdict_snapshot(generated),
        affected_assets=(
            AffectedAsset(
                asset_id="unavailable",
                kind="unknown",
                project_scope="unknown",
                operation="unknown",
                display_label="Unavailable risk context",
            ),
        ),
        tool_authority=authority,
        rollback_status=RiskRollbackStatus.UNAVAILABLE,
        recovery_note=recovery_note,
        prior_decisions=(),
        explanation_snapshot=_degraded_explanation_snapshot(),
        evidence_links=(_degraded_evidence_link(),),
        replay_metadata=_degraded_replay_metadata(generated),
        generated_at_utc=generated,
        audit_payload={
            "degraded": True,
            "replay_ref": "risk-context:degraded",
            "evidence_refs": ["risk-context:degraded"],
        },
    )


def _entry_point_or_raise(entry_point: RiskContextEntryPoint | str) -> RiskContextEntryPoint:
    try:
        return RiskContextEntryPoint(entry_point)
    except ValueError as exc:
        raise RiskContextError(
            RiskContextErrorReason.INVALID_ENTRY_POINT,
            f"unsupported risk-context entry point: {entry_point!r}",
        ) from exc


def _safe_degraded_entry_point(entry_point: RiskContextEntryPoint | str) -> RiskContextEntryPoint:
    try:
        return RiskContextEntryPoint(entry_point)
    except ValueError:
        logger.warning(
            "Risk context entry point failed closed to imported workflow.",
            extra={"entry_point": str(entry_point)},
            exc_info=True,
        )
        return RiskContextEntryPoint.IMPORTED_WORKFLOW


def _degraded_evidence_link() -> dict[str, str]:
    return {
        "evidence_id": "risk-context-degraded",
        "kind": "external",
        "ref": "risk-context:degraded",
        "summary": "risk context construction failed closed before approval",
    }


def _degraded_verdict_snapshot(generated: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "verdict_id": "verdict-degraded",
        "value": "block",
        "mode": "strict",
        "risk_domain": "approval",
        "reason_code": "missing_required_risk_context",
        "action_id": "unavailable",
        "actor_id": "unavailable",
        "run_id": "unavailable",
        "evidence_links": [_degraded_evidence_link()],
        "replay_metadata": _degraded_replay_metadata(generated),
        "policy_version": "risk-context-degraded",
        "evaluated_at_utc": generated,
        "summary": "missing required risk context",
        "details": {"degraded": True},
    }


def _degraded_explanation_snapshot() -> dict[str, Any]:
    return {
        "allowed": False,
        "policy_id": "risk-context-degraded",
        "policy_source": "risk_context",
        "decision_kind": "deny",
        "reasons": [],
        "denial_reasons": ["missing risk context"],
        "exposures": {},
        "budget": {},
        "trace": {},
        "failure_behavior": "deny-before-use",
        "degraded": True,
    }


def _degraded_replay_metadata(generated: str) -> dict[str, str]:
    return {
        "schema_version": SCHEMA_VERSION,
        "policy_version": "risk-context-degraded",
        "mode": "strict",
        "captured_at_utc": generated,
    }


def _require_text(value: str, field_name: str, reason: RiskContextErrorReason) -> None:
    if not isinstance(value, str) or not value.strip():
        raise RiskContextError(reason, f"{field_name} is required")


def _string_tuple(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(str(value).strip() for value in values if str(value).strip())


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
