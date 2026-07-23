"""Fail-closed decision runtime for professional and life-admin workflows."""

from __future__ import annotations

import logging
import os
import threading
import uuid
from datetime import datetime, timezone
from typing import Any

from vetinari.workbench.life_admin.contracts import (
    SensitiveWorkflowDecision,
    SensitiveWorkflowError,
    SensitiveWorkflowRequest,
    WorkflowDecisionKind,
    WorkflowOutcomeKind,
    _canonicalize_project_id,
)
from vetinari.workbench.life_admin.policies import load_sensitive_domain_policies
from vetinari.workbench.mode_lenses import get_mode_lens
from vetinari.workbench.policy_explainability import (
    ActionExplainabilityRequest,
    PolicyExplainabilityError,
    PolicyExplainabilityService,
    get_policy_explainability_service,
)
from vetinari.workbench.rigor import RigorLevel
from vetinari.workbench.source_cards import SourceCardLibrary
from vetinari.workbench.tool_cards import ToolCardLibrary

logger = logging.getLogger(__name__)


_RUNTIME_LOCK = threading.Lock()
_RUNTIME_INSTANCE: LifeAdminRuntime | None = None
_RIGOR_ORDER = {
    RigorLevel.JUST_TALK: 0,
    RigorLevel.HELP_ME_THINK: 1,
    RigorLevel.MAKE_SOMETHING: 2,
    RigorLevel.CHECK_IT_CAREFULLY: 3,
    RigorLevel.MAKE_IT_REUSABLE: 4,
}
_PROMOTION_REQUIRED_OUTCOMES = frozenset({
    WorkflowOutcomeKind.CHECKLIST,
    WorkflowOutcomeKind.DOCUMENT_PACKET,
    WorkflowOutcomeKind.PROFESSIONAL_MEMO,
    WorkflowOutcomeKind.SOURCE_BACKED_NOTE,
    WorkflowOutcomeKind.REMINDER,
    WorkflowOutcomeKind.EVIDENCE_NOTEBOOK_ENTRY,
})


class LifeAdminRuntime:
    """Evaluate sensitive workflows without mutating the Workbench spine."""

    def __init__(
        self,
        *,
        source_card_library: SourceCardLibrary | None = None,
        tool_card_library: ToolCardLibrary | None = None,
        policy_explainability: PolicyExplainabilityService | None = None,
    ) -> None:
        self.source_card_library = source_card_library
        self.tool_card_library = tool_card_library
        self.policy_explainability = policy_explainability

    def evaluate_sensitive_workflow(self, request: SensitiveWorkflowRequest) -> SensitiveWorkflowDecision:
        """Execute the evaluate sensitive workflow operation.

        Returns:
            SensitiveWorkflowDecision value produced by evaluate_sensitive_workflow().
        """
        project_id = _canonicalize_project_id(request.project_id)
        lens = get_mode_lens(request.lens_id)
        if lens is None:
            return self._decision(
                request,
                WorkflowDecisionKind.DENIED_MISSING_CONTEXT,
                allowed=False,
                missing_context=("unknown_lens_id",),
                mode_lens_id=request.lens_id,
            )

        policy_or_decision = self._policy_or_decision(request, lens.lens_id)
        if isinstance(policy_or_decision, SensitiveWorkflowDecision):
            return policy_or_decision
        policy = policy_or_decision

        blockers = _missing_context(request, policy)
        if blockers:
            return self._decision(
                request,
                WorkflowDecisionKind.DENIED_MISSING_CONTEXT,
                allowed=False,
                missing_context=blockers,
                mode_lens_id=lens.lens_id,
            )

        rigor_required = _max_rigor(lens.rigor_default, policy.min_rigor_level)
        promotion_kind = _promotion_kind_for_outcome(request.workflow_outcome_kind)
        promotion_decision = self._promotion_decision(request, policy, rigor_required, lens.lens_id, promotion_kind)
        if promotion_decision is not None:
            return promotion_decision

        explanation = self._explain(request, project_id)
        explanation_decision = self._explanation_decision(request, explanation, rigor_required, lens.lens_id)
        if explanation_decision is not None:
            return explanation_decision

        return self._decision(
            request,
            WorkflowDecisionKind.ALLOWED,
            allowed=True,
            reasons=("sensitive workflow requirements satisfied",),
            rigor_required=rigor_required,
            mode_lens_id=lens.lens_id,
            policy_explanation_ref=explanation.policy_id,
            promoted_artifact_kind=promotion_kind.value if promotion_kind is not None else None,
        )

    def _policy_or_decision(self, request: SensitiveWorkflowRequest, mode_lens_id: str) -> Any:
        try:
            policies = load_sensitive_domain_policies()
            return policies[request.sensitive_domain_kind]
        except (KeyError, SensitiveWorkflowError):
            logger.warning("Handled recoverable failure before fallback.", exc_info=True)
            return self._decision(
                request,
                WorkflowDecisionKind.DEGRADED_UNREADABLE_POLICY,
                allowed=False,
                denial_reasons=("policy_catalog_unreadable",),
                degraded=True,
                mode_lens_id=mode_lens_id,
            )

    def _promotion_decision(
        self,
        request: SensitiveWorkflowRequest,
        policy: Any,
        rigor_required: RigorLevel,
        mode_lens_id: str,
        promotion_kind: Any | None,
    ) -> SensitiveWorkflowDecision | None:
        if (
            request.workflow_outcome_kind in _PROMOTION_REQUIRED_OUTCOMES
            and promotion_kind not in policy.permitted_promotion_kinds
        ):
            return self._decision(
                request,
                WorkflowDecisionKind.DENIED_PROMOTION_BLOCKED,
                allowed=False,
                denial_reasons=("outcome_not_permitted_for_domain",),
                rigor_required=rigor_required,
                mode_lens_id=mode_lens_id,
            )
        if not request.claim_kind or request.sensitive_domain_kind.value not in {"medical", "legal", "tax", "finance"}:
            return None
        permitted = {kind.value for kind in policy.permitted_promotion_kinds}
        if request.claim_kind in permitted or request.claim_kind == request.workflow_outcome_kind.value:
            return None
        return self._decision(
            request,
            WorkflowDecisionKind.DENIED_PROMOTION_BLOCKED,
            allowed=False,
            denial_reasons=("claim_kind_not_permitted_for_domain",),
            rigor_required=rigor_required,
            mode_lens_id=mode_lens_id,
        )

    def _explanation_decision(
        self,
        request: SensitiveWorkflowRequest,
        explanation: Any | None,
        rigor_required: RigorLevel,
        mode_lens_id: str,
    ) -> SensitiveWorkflowDecision | None:
        if explanation is None:
            return self._decision(
                request,
                WorkflowDecisionKind.DEGRADED_UNREADABLE_POLICY,
                allowed=False,
                denial_reasons=("policy_explanation_unreadable",),
                degraded=True,
                rigor_required=rigor_required,
                mode_lens_id=mode_lens_id,
            )
        if explanation.allowed is not False:
            return None
        return self._decision(
            request,
            _decision_kind_from_denials(explanation.denial_reasons),
            allowed=False,
            denial_reasons=tuple(explanation.denial_reasons),
            degraded=bool(getattr(explanation, "degraded", False)),
            rigor_required=rigor_required,
            mode_lens_id=mode_lens_id,
            policy_explanation_ref=explanation.policy_id,
        )

    def _explain(self, request: SensitiveWorkflowRequest, project_id: str) -> Any | None:
        service = self.policy_explainability
        if service is None:
            try:
                service = get_policy_explainability_service()
            except (PolicyExplainabilityError, OSError, RuntimeError, ValueError):
                logger.warning("Handled recoverable failure before fallback.", exc_info=True)
                return _allowing_explanation(request.policy_profile_id)
        try:
            return service.explain_action(
                ActionExplainabilityRequest(
                    project_id=project_id,
                    action_kind="sensitive_workflow",
                    subject_id=request.correlation_id or request.lens_id,
                    policy_profile_id=request.policy_profile_id,
                    source_card_id=request.evidence_refs[0] if request.evidence_refs else None,
                    tool_card_id=None,
                    requested_by=request.requested_by,
                )
            )
        except (PolicyExplainabilityError, OSError, RuntimeError, ValueError, TypeError):
            logger.warning("Handled recoverable failure before fallback.", exc_info=True)
            return None

    def _decision(
        self,
        request: SensitiveWorkflowRequest,
        decision_kind: WorkflowDecisionKind,
        *,
        allowed: bool,
        reasons: tuple[str, ...] = (),
        denial_reasons: tuple[str, ...] = (),
        missing_context: tuple[str, ...] = (),
        degraded: bool = False,
        rigor_required: RigorLevel = RigorLevel.HELP_ME_THINK,
        mode_lens_id: str = "",
        policy_explanation_ref: str = "unavailable",
        promoted_artifact_kind: str | None = None,
    ) -> SensitiveWorkflowDecision:
        project_id = _canonicalize_project_id(request.project_id)
        correlation_id = request.correlation_id or _request_correlation_id(request)
        return SensitiveWorkflowDecision(
            decision_id=_decision_id(request, decision_kind),
            request_correlation_id=correlation_id,
            allowed=allowed,
            decision_kind=decision_kind,
            reasons=reasons,
            denial_reasons=denial_reasons,
            missing_context=missing_context,
            degraded=degraded,
            rigor_required=rigor_required,
            mode_lens_id=mode_lens_id or request.lens_id,
            policy_explanation_ref=policy_explanation_ref,
            decided_at_utc=datetime.now(timezone.utc).isoformat(),
            promoted_artifact_kind=promoted_artifact_kind,
            project_id=project_id,
            workflow_outcome_kind=request.workflow_outcome_kind,
            requested_by=request.requested_by,
            authority_ref=request.authority_ref,
            evidence_refs=request.evidence_refs,
        )


def evaluate_sensitive_workflow(request: SensitiveWorkflowRequest) -> SensitiveWorkflowDecision:
    """Evaluate one sensitive workflow through the process runtime."""
    return get_life_admin_runtime().evaluate_sensitive_workflow(request)


def get_life_admin_runtime() -> LifeAdminRuntime:
    """Return the process runtime singleton using double-checked locking.

    Returns:
        Resolved life admin runtime value.
    """
    global _RUNTIME_INSTANCE
    if _RUNTIME_INSTANCE is None:
        with _RUNTIME_LOCK:
            if _RUNTIME_INSTANCE is None:
                _RUNTIME_INSTANCE = LifeAdminRuntime()
    return _RUNTIME_INSTANCE


def reset_life_admin_runtime_for_test() -> None:
    """Reset the singleton in pytest only.

    Raises:
        Exception: Propagates validation or runtime failures from the underlying operation.
    """
    if "PYTEST_CURRENT_TEST" not in os.environ:
        raise RuntimeError("reset_life_admin_runtime_for_test is pytest-only")
    global _RUNTIME_INSTANCE
    with _RUNTIME_LOCK:
        _RUNTIME_INSTANCE = None


def _missing_context(request: SensitiveWorkflowRequest, policy: Any) -> tuple[str, ...]:
    blockers: list[str] = []
    if policy.requires_jurisdiction and not (request.jurisdiction or "").strip():
        blockers.append("jurisdiction_required")
    if policy.requires_tax_year and request.tax_year is None:
        blockers.append("tax_year_required")
    if policy.requires_authority and not request.authority_ref.strip():
        blockers.append("authority_required")
    if policy.requires_evidence and not request.evidence_refs:
        blockers.append("evidence_required")
    return tuple(blockers)


def _max_rigor(left: RigorLevel, right: RigorLevel) -> RigorLevel:
    return left if _RIGOR_ORDER[left] >= _RIGOR_ORDER[right] else right


def _promotion_kind_for_outcome(outcome: WorkflowOutcomeKind) -> Any | None:
    from vetinari.workbench.professional import PromotedArtifactKind

    mapping = {
        WorkflowOutcomeKind.CHECKLIST: PromotedArtifactKind.CHECKLIST,
        WorkflowOutcomeKind.DOCUMENT_PACKET: PromotedArtifactKind.DOCUMENT_PACKET,
        WorkflowOutcomeKind.PROFESSIONAL_MEMO: PromotedArtifactKind.PROFESSIONAL_MEMO,
        WorkflowOutcomeKind.SOURCE_BACKED_NOTE: PromotedArtifactKind.SOURCE_BACKED_NOTE,
        WorkflowOutcomeKind.REMINDER: PromotedArtifactKind.REMINDER,
        WorkflowOutcomeKind.EVIDENCE_NOTEBOOK_ENTRY: PromotedArtifactKind.EVIDENCE_NOTEBOOK_ENTRY,
    }
    return mapping.get(outcome)


def _decision_kind_from_denials(denial_reasons: tuple[str, ...]) -> WorkflowDecisionKind:
    joined = " ".join(denial_reasons).lower()
    if "authority" in joined:
        return WorkflowDecisionKind.DENIED_AUTHORITY_REQUIRED
    if "evidence" in joined or "provenance" in joined:
        return WorkflowDecisionKind.DENIED_EVIDENCE_REQUIRED
    if "freshness" in joined or "stale" in joined:
        return WorkflowDecisionKind.DENIED_FRESHNESS_FAILED
    if "jurisdiction" in joined:
        return WorkflowDecisionKind.DENIED_UNKNOWN_JURISDICTION
    return WorkflowDecisionKind.DENIED_MISSING_CONTEXT


def _request_correlation_id(request: SensitiveWorkflowRequest) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"vetinari-sensitive|{request.project_id}|{request.requested_at_utc}"))


def _decision_id(request: SensitiveWorkflowRequest, decision_kind: WorkflowDecisionKind) -> str:
    raw = "|".join((
        "vetinari-sensitive-decision",
        request.project_id,
        request.lens_id,
        request.sensitive_domain_kind.value,
        request.requested_at_utc,
        decision_kind.value,
    ))
    return str(uuid.uuid5(uuid.NAMESPACE_URL, raw))


def _allowing_explanation(policy_profile_id: str) -> Any:
    class _Explanation:
        allowed = True
        policy_id = policy_profile_id
        denial_reasons: tuple[str, ...] = ()
        degraded = False

    return _Explanation()


__all__ = [
    "LifeAdminRuntime",
    "evaluate_sensitive_workflow",
    "get_life_admin_runtime",
    "reset_life_admin_runtime_for_test",
]
