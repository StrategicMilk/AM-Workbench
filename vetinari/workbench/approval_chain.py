"""Ordered Workbench approval-chain resolver."""

from __future__ import annotations

import logging
import threading
from datetime import timedelta
from pathlib import Path
from typing import Any

from vetinari.workbench.approval_chain_config import (
    _DEFAULT_CONFIG_PATH,
    _prepare_approval_chain_config,
    load_approval_chain_config,
)
from vetinari.workbench.approval_chain_decisions import (
    _bounded_error,
    _build_decision,
    _contains_indicator,
    _first_protected_path,
    _minimal_closed_config,
    render_approval_chain_explanation,
)
from vetinari.workbench.approval_chain_models import (
    ApprovalChainDecision,
    ApprovalChainError,
    ApprovalChainOutcome,
    ApprovalChainReason,
    ApprovalChainRequest,
    ApprovalChainStep,
    ApprovalChannel,
    _allow_key,
    _coerce_channel,
    _SessionAllowGrant,
    _utc_now,
)
from vetinari.workbench.governance_modes.contracts import GovernanceEnforcementEffect
from vetinari.workbench.governance_modes.runtime import apply_governance_mode
from vetinari.workbench.policy.verdicts import (
    ActionVerdict,
    VerdictValue,
    classify_action,
)
from vetinari.workbench.readiness import AdmissionDecision, evaluate_workbench_admission

logger = logging.getLogger(__name__)


_TRACE_NOT_EVALUATED = "not_evaluated_after_first_match"
_APPROVAL_CHAIN_INSTANCE: ApprovalChainResolver | None = None
_APPROVAL_CHAIN_LOCK = threading.RLock()


class ApprovalChainResolver:
    """Resolve Workbench action admission with ordered first-match semantics."""

    def __init__(self, *, config_path: Path | str | None = None, config: dict[str, Any] | None = None) -> None:
        self._config_path = Path(config_path) if config_path is not None else _DEFAULT_CONFIG_PATH
        self._config_error = ""
        try:
            self._config = (
                _prepare_approval_chain_config(config)
                if config is not None
                else load_approval_chain_config(self._config_path)
            )
        except ApprovalChainError as exc:
            self._config = _minimal_closed_config()
            self._config_error = str(exc)
        self._lock = threading.RLock()
        self._session_allows: dict[tuple[str, str, str, str], _SessionAllowGrant] = {}
        self._issued_decisions: dict[str, ApprovalChainDecision] = {}
        self._last_decision: ApprovalChainDecision | None = None

    def grant_session_allow(
        self,
        *,
        project_id: str,
        session_id: str,
        channel: ApprovalChannel | str,
        action_fingerprint: str,
        ttl_seconds: int | None = None,
    ) -> dict[str, str]:
        """Grant one scoped, expiring session auto-approval.

        Returns:
            dict[str, str] value produced by grant_session_allow().
        """
        clean_channel = _coerce_channel(channel).value
        ttl = int(ttl_seconds or self._config["session_allow"]["default_ttl_seconds"])
        max_ttl = int(self._config["session_allow"]["max_ttl_seconds"])
        ttl = max(1, min(ttl, max_ttl))
        now = _utc_now()
        grant = _SessionAllowGrant(
            project_id=project_id,
            session_id=session_id,
            channel=clean_channel,
            action_fingerprint=action_fingerprint,
            expires_at_utc=(now + timedelta(seconds=ttl)).isoformat(),
            granted_at_utc=now.isoformat(),
        )
        with self._lock:
            self._session_allows[_allow_key(project_id, session_id, clean_channel, action_fingerprint)] = grant
        return grant.to_dict()

    def revoke_session_allow(
        self,
        *,
        project_id: str,
        session_id: str,
        channel: ApprovalChannel | str,
        action_fingerprint: str,
    ) -> bool:
        """Revoke exactly one scoped session allow grant.

        Returns:
            bool value produced by revoke_session_allow().
        """
        clean_channel = _coerce_channel(channel).value
        with self._lock:
            return (
                self._session_allows.pop(
                    _allow_key(project_id, session_id, clean_channel, action_fingerprint),
                    None,
                )
                is not None
            )

    def explain_last(self) -> dict[str, Any] | None:
        """Return the last decision payload for UI refreshes.

        Returns:
            dict[str, Any] | None value produced by explain_last().
        """
        with self._lock:
            return self._last_decision.to_dict() if self._last_decision is not None else None

    def lookup_decision(self, decision_id: str) -> ApprovalChainDecision | None:
        """Return a decision issued by this resolver in the current process.

        Returns:
            ApprovalChainDecision | None value produced by lookup_decision().
        """
        clean_decision_id = str(decision_id).strip()
        if not clean_decision_id:
            return None
        with self._lock:
            return self._issued_decisions.get(clean_decision_id)

    def resolve(self, request: ApprovalChainRequest) -> ApprovalChainDecision:
        """Resolve a request by ordered, first-match-wins approval semantics.

        Returns:
            ApprovalChainDecision value produced by resolve().
        """
        trace: list[ApprovalChainStep] = []
        decisive: tuple[str, ApprovalChainOutcome, ApprovalChainReason, str, str] | None = None
        verdict: ActionVerdict | None = None

        try:
            verdict = classify_action(request.to_action_input())
            trace.append(
                ApprovalChainStep(
                    name="capability_classification",
                    status="evaluated",
                    reason=ApprovalChainReason.CAPABILITY_CLASSIFIED.value,
                    detail=f"{verdict.value.value}:{verdict.reason_code.value}",
                )
            )
        except Exception as exc:
            trace.append(
                ApprovalChainStep(
                    name="policy_state_unreadable",
                    status="matched",
                    reason=ApprovalChainReason.POLICY_STATE_UNREADABLE.value,
                    outcome=ApprovalChainOutcome.DENY.value,
                    detail=_bounded_error(exc),
                )
            )
            decisive = (
                "policy_state_unreadable",
                ApprovalChainOutcome.DENY,
                ApprovalChainReason.POLICY_STATE_UNREADABLE,
                "deny_by_default",
                _bounded_error(exc),
            )

        if decisive is None and self._config_error:
            decisive = self._match(
                trace,
                "policy_state_unreadable",
                ApprovalChainOutcome.DENY,
                ApprovalChainReason.POLICY_STATE_UNREADABLE,
                "deny_by_default",
                self._config_error,
            )

        if decisive is None:
            for step in self._config["ordered_steps"]:
                step_name = str(step.get("name", "")) if isinstance(step, dict) else str(step)
                if step_name == "capability_classification":
                    continue
                decisive = self._evaluate_configured_step(step_name, trace, request, verdict)
                if decisive is not None:
                    break

        assert decisive is not None
        matched_step, outcome, reason, fallback_rule, detail = decisive
        self._append_unvisited(trace, matched_step)
        decision = _build_decision(
            request=request,
            outcome=outcome,
            matched_step=matched_step,
            fallback_rule=fallback_rule,
            ordered_trace=tuple(trace),
            reason=reason,
            detail=detail,
        )
        with self._lock:
            self._last_decision = decision
            self._issued_decisions[decision.decision_id] = decision
        return decision

    def _evaluate_configured_step(
        self,
        step_name: str,
        trace: list[ApprovalChainStep],
        request: ApprovalChainRequest,
        verdict: ActionVerdict | None,
    ) -> tuple[str, ApprovalChainOutcome, ApprovalChainReason, str, str] | None:
        if step_name == "hard_deny":
            return self._evaluate_hard_deny(trace, request, verdict)
        if step_name == "protected_path":
            return self._evaluate_protected_path(trace, request)
        if step_name == "destructive_action":
            return self._evaluate_destructive_action(trace, request)
        if step_name == "dlp_risk":
            return self._evaluate_dlp_risk(trace, request)
        if step_name == "tool_pin_unverified":
            return self._evaluate_tool_pin(trace, request)
        if step_name == "readiness_gate":
            return self._evaluate_readiness(trace, request)
        if step_name == "governance_gate":
            return self._evaluate_governance(trace, request, verdict)
        if step_name == "session_allow_list":
            return self._evaluate_session_allow(trace, request)
        if step_name == "human_approval_fallback":
            return self._evaluate_human_approval_fallback(trace, request)
        if step_name == "deny_by_default":
            return self._evaluate_deny_by_default(trace)
        return self._match(
            trace,
            step_name or "unknown_config_step",
            ApprovalChainOutcome.DENY,
            ApprovalChainReason.POLICY_STATE_UNREADABLE,
            "deny_by_default",
            f"unsupported approval-chain step {step_name!r}",
        )

    def _evaluate_hard_deny(
        self,
        trace: list[ApprovalChainStep],
        request: ApprovalChainRequest,
        verdict: ActionVerdict | None,
    ) -> tuple[str, ApprovalChainOutcome, ApprovalChainReason, str, str] | None:
        if verdict is not None and verdict.value is VerdictValue.BLOCK:
            return self._match(
                trace,
                "hard_deny",
                ApprovalChainOutcome.DENY,
                ApprovalChainReason.POLICY_HARD_DENY,
                "hard_deny",
                verdict.reason_code.value,
            )
        if request.hard_deny or _contains_indicator(request, self._config["hard_deny_indicators"]):
            return self._match(
                trace,
                "hard_deny",
                ApprovalChainOutcome.DENY,
                ApprovalChainReason.POLICY_HARD_DENY,
                "hard_deny",
                "request matched hard deny indicator",
            )
        trace.append(ApprovalChainStep(name="hard_deny", status="skipped", reason="no_hard_deny"))
        return None

    def _evaluate_protected_path(
        self,
        trace: list[ApprovalChainStep],
        request: ApprovalChainRequest,
    ) -> tuple[str, ApprovalChainOutcome, ApprovalChainReason, str, str] | None:
        protected = _first_protected_path(request.target_paths, self._config["protected_path_prefixes"])
        if protected:
            return self._match(
                trace,
                "protected_path",
                ApprovalChainOutcome.DENY,
                ApprovalChainReason.PROTECTED_PATH,
                "hard_deny",
                protected,
            )
        trace.append(ApprovalChainStep(name="protected_path", status="skipped", reason="no_protected_path"))
        return None

    def _evaluate_destructive_action(
        self,
        trace: list[ApprovalChainStep],
        request: ApprovalChainRequest,
    ) -> tuple[str, ApprovalChainOutcome, ApprovalChainReason, str, str] | None:
        if request.destructive or _contains_indicator(request, self._config["destructive_indicators"]):
            return self._match(
                trace,
                "destructive_action",
                ApprovalChainOutcome.REQUIRE_HUMAN_APPROVAL,
                ApprovalChainReason.DESTRUCTIVE_ACTION,
                "human_approval_required",
                "destructive action cannot use session auto-approve",
            )
        trace.append(ApprovalChainStep(name="destructive_action", status="skipped", reason="not_destructive"))
        return None

    def _evaluate_dlp_risk(
        self,
        trace: list[ApprovalChainStep],
        request: ApprovalChainRequest,
    ) -> tuple[str, ApprovalChainOutcome, ApprovalChainReason, str, str] | None:
        if request.dlp_risk or _contains_indicator(request, self._config["dlp_indicators"]):
            return self._match(
                trace,
                "dlp_risk",
                ApprovalChainOutcome.REQUIRE_HUMAN_APPROVAL,
                ApprovalChainReason.DLP_RISK,
                "human_approval_required",
                "DLP-sensitive action cannot use session auto-approve",
            )
        trace.append(ApprovalChainStep(name="dlp_risk", status="skipped", reason="no_dlp_risk"))
        return None

    def _evaluate_tool_pin(
        self,
        trace: list[ApprovalChainStep],
        request: ApprovalChainRequest,
    ) -> tuple[str, ApprovalChainOutcome, ApprovalChainReason, str, str] | None:
        if (request.requires_tool_pin and not request.tool_pin_verified) or _contains_indicator(
            request,
            self._config["tool_pin_indicators"],
        ):
            return self._match(
                trace,
                "tool_pin_unverified",
                ApprovalChainOutcome.DENY,
                ApprovalChainReason.TOOL_PIN_UNVERIFIED,
                "deny_by_default",
                "required tool pin is missing or unreadable",
            )
        trace.append(ApprovalChainStep(name="tool_pin_unverified", status="skipped", reason="tool_pin_clear"))
        return None

    def _evaluate_readiness(
        self,
        trace: list[ApprovalChainStep],
        request: ApprovalChainRequest,
    ) -> tuple[str, ApprovalChainOutcome, ApprovalChainReason, str, str] | None:
        if request.readiness_signals is None:
            return self._match(
                trace,
                "readiness_unavailable",
                ApprovalChainOutcome.DENY,
                ApprovalChainReason.READINESS_UNAVAILABLE,
                "deny_by_default",
                "readiness signals missing",
            )
        try:
            admission = evaluate_workbench_admission(request.readiness_signals, feature=request.readiness_feature)
        except Exception as exc:
            logger.warning("Handled recoverable failure before fallback.", exc_info=True)
            return self._match(
                trace,
                "readiness_unavailable",
                ApprovalChainOutcome.DENY,
                ApprovalChainReason.READINESS_UNAVAILABLE,
                "deny_by_default",
                _bounded_error(exc),
            )
        if admission.decision is AdmissionDecision.BLOCK:
            return self._match(
                trace,
                "readiness_blocked",
                ApprovalChainOutcome.DENY,
                ApprovalChainReason.READINESS_BLOCKED,
                "deny_by_default",
                "; ".join(admission.reasons),
            )
        if admission.decision in {AdmissionDecision.REQUIRE_CONFIRMATION, AdmissionDecision.RESTRICT}:
            return self._match(
                trace,
                "readiness_confirmation_required",
                ApprovalChainOutcome.REQUIRE_HUMAN_APPROVAL,
                ApprovalChainReason.READINESS_BLOCKED,
                "human_approval_required",
                "; ".join(admission.reasons),
            )
        trace.append(ApprovalChainStep(name="readiness_gate", status="evaluated", reason="readiness_allows"))
        return None

    def _evaluate_governance(
        self,
        trace: list[ApprovalChainStep],
        request: ApprovalChainRequest,
        verdict: ActionVerdict | None,
    ) -> tuple[str, ApprovalChainOutcome, ApprovalChainReason, str, str] | None:
        if not request.governance_available or verdict is None:
            return self._match(
                trace,
                "governance_unavailable",
                ApprovalChainOutcome.DENY,
                ApprovalChainReason.GOVERNANCE_UNAVAILABLE,
                "deny_by_default",
                "governance state missing",
            )
        try:
            decision = apply_governance_mode(mode=request.governance_mode, verdict=verdict)
        except Exception as exc:
            logger.warning("Handled recoverable failure before fallback.", exc_info=True)
            return self._match(
                trace,
                "governance_unavailable",
                ApprovalChainOutcome.DENY,
                ApprovalChainReason.GOVERNANCE_UNAVAILABLE,
                "deny_by_default",
                _bounded_error(exc),
            )
        if decision.enforcement_effect is GovernanceEnforcementEffect.BLOCKED:
            return self._match(
                trace,
                "governance_blocked",
                ApprovalChainOutcome.DENY,
                ApprovalChainReason.GOVERNANCE_BLOCKED,
                "deny_by_default",
                decision.summary,
            )
        if decision.enforcement_effect is GovernanceEnforcementEffect.REQUIRES_REVIEW:
            return self._match(
                trace,
                "governance_review_required",
                ApprovalChainOutcome.REQUIRE_HUMAN_APPROVAL,
                ApprovalChainReason.GOVERNANCE_BLOCKED,
                "human_approval_required",
                decision.summary,
            )
        trace.append(
            ApprovalChainStep(name="governance_gate", status="evaluated", reason=decision.enforcement_effect.value)
        )
        return None

    def _evaluate_session_allow(
        self,
        trace: list[ApprovalChainStep],
        request: ApprovalChainRequest,
    ) -> tuple[str, ApprovalChainOutcome, ApprovalChainReason, str, str] | None:
        now = _utc_now()
        key = _allow_key(request.project_id, request.session_id, request.channel.value, request.fingerprint)
        with self._lock:
            grant = self._session_allows.get(key)
            if grant is not None and grant.expired(now):
                self._session_allows.pop(key, None)
                grant = None
        if grant is None:
            trace.append(ApprovalChainStep(name="session_allow_list", status="skipped", reason="no_matching_grant"))
            return None
        return self._match(
            trace,
            "session_allow_list",
            ApprovalChainOutcome.ALLOW,
            ApprovalChainReason.SESSION_ALLOW,
            "session_allow",
            grant.expires_at_utc,
        )

    def _evaluate_human_approval_fallback(
        self,
        trace: list[ApprovalChainStep],
        request: ApprovalChainRequest,
    ) -> tuple[str, ApprovalChainOutcome, ApprovalChainReason, str, str] | None:
        sources = {source.lower() for source in request.approval_sources}
        if "human" in sources:
            return self._match(
                trace,
                "human_approval_fallback",
                ApprovalChainOutcome.REQUIRE_HUMAN_APPROVAL,
                ApprovalChainReason.HUMAN_APPROVAL,
                "human_approval",
                "human approval source available",
            )
        trace.append(ApprovalChainStep(name="human_approval_fallback", status="skipped", reason="no_human_source"))
        return None

    def _evaluate_deny_by_default(
        self,
        trace: list[ApprovalChainStep],
    ) -> tuple[str, ApprovalChainOutcome, ApprovalChainReason, str, str]:
        return self._match(
            trace,
            "deny_by_default",
            ApprovalChainOutcome.DENY,
            ApprovalChainReason.DENY_BY_DEFAULT,
            "deny_by_default",
            "no trusted approval source available",
        )

    @staticmethod
    def _match(
        trace: list[ApprovalChainStep],
        name: str,
        outcome: ApprovalChainOutcome,
        reason: ApprovalChainReason,
        fallback_rule: str,
        detail: str,
    ) -> tuple[str, ApprovalChainOutcome, ApprovalChainReason, str, str]:
        trace.append(
            ApprovalChainStep(
                name=name,
                status="matched",
                reason=reason.value,
                outcome=outcome.value,
                detail=detail,
            )
        )
        return (name, outcome, reason, fallback_rule, detail)

    @staticmethod
    def _append_unvisited(trace: list[ApprovalChainStep], matched_step: str) -> None:
        visited = {step.name for step in trace}
        trace.extend(
            ApprovalChainStep(name=step_name, status="skipped", reason=_TRACE_NOT_EVALUATED)
            for step_name in (
                "hard_deny",
                "protected_path",
                "destructive_action",
                "dlp_risk",
                "tool_pin_unverified",
                "readiness_gate",
                "governance_gate",
                "session_allow_list",
                "human_approval_fallback",
                "deny_by_default",
            )
            if step_name not in visited and step_name != matched_step
        )


def evaluate_approval_chain(
    request: ApprovalChainRequest, *, resolver: ApprovalChainResolver | None = None
) -> ApprovalChainDecision:
    """Resolve one approval-chain request through the provided or singleton resolver."""
    return (resolver or get_workbench_approval_chain()).resolve(request)


def get_workbench_approval_chain() -> ApprovalChainResolver:
    """Return the process singleton using double-checked locking.

    Returns:
        Value produced for the caller.
    """
    global _APPROVAL_CHAIN_INSTANCE
    if _APPROVAL_CHAIN_INSTANCE is None:
        with _APPROVAL_CHAIN_LOCK:
            if _APPROVAL_CHAIN_INSTANCE is None:
                _APPROVAL_CHAIN_INSTANCE = ApprovalChainResolver()
    return _APPROVAL_CHAIN_INSTANCE


def reset_workbench_approval_chain_for_test() -> None:
    """Reset the approval-chain singleton for deterministic tests."""
    global _APPROVAL_CHAIN_INSTANCE
    with _APPROVAL_CHAIN_LOCK:
        _APPROVAL_CHAIN_INSTANCE = None


__all__ = [
    "ApprovalChainDecision",
    "ApprovalChainError",
    "ApprovalChainOutcome",
    "ApprovalChainReason",
    "ApprovalChainRequest",
    "ApprovalChainResolver",
    "ApprovalChainStep",
    "ApprovalChannel",
    "evaluate_approval_chain",
    "get_workbench_approval_chain",
    "load_approval_chain_config",
    "render_approval_chain_explanation",
    "reset_workbench_approval_chain_for_test",
]
