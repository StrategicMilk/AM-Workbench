"""Approval Chain adapters for Channel Hub payloads."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from vetinari.workbench.approval_chain import ApprovalChainRequest, ApprovalChannel, get_workbench_approval_chain
from vetinari.workbench.policy.verdicts import RiskDomain

from .contracts import SCHEMA_VERSION

ApprovalResolverProvider = Callable[[], Any]


def route_channel_approval_request(
    *,
    channel_id: str,
    run_id: str,
    actor_id: str,
    action_id: str,
    action_type: str,
    action_fingerprint: str,
    summary: str,
    project_id: str = "default",
    session_id: str = "channel-hub",
    risk_domain: RiskDomain | str = RiskDomain.APPROVAL,
    approval_sources: tuple[str, ...] = ("channel_hub",),
    resolver_provider: ApprovalResolverProvider = get_workbench_approval_chain,
) -> dict[str, Any]:
    """Execute the route channel approval request operation.

    Returns:
        Outcome produced by route_channel_approval_request().
    """
    request = ApprovalChainRequest(
        project_id=project_id,
        session_id=session_id,
        channel=ApprovalChannel.MOBILE.value
        if channel_id == "mobile-companion"
        else ApprovalChannel.NOTIFICATION.value,
        action_id=action_id,
        action_type=action_type,
        actor_id=actor_id,
        run_id=run_id,
        risk_domain=getattr(risk_domain, "value", risk_domain),
        summary=summary,
        action_fingerprint=action_fingerprint,
        approval_sources=approval_sources,
        authority_refs=("workbench-channel-hub",),
        metadata={"channel_id": channel_id},
        details={"channel_id": channel_id},
        readiness_feature="automation_admission",
        governance_mode="strict",
    )
    decision = resolver_provider().resolve(request)
    payload = decision.to_dict()
    payload["schema_version"] = SCHEMA_VERSION
    payload["channel_id"] = channel_id
    payload["run_id"] = run_id
    payload["actor_id"] = actor_id
    payload["action_fingerprint"] = decision.action_fingerprint
    return payload


def apply_channel_approval_update(
    *,
    decision_id: str,
    channel_id: str,
    actor_id: str,
    approved: bool,
    resolver_provider: ApprovalResolverProvider = get_workbench_approval_chain,
) -> dict[str, Any]:
    """Execute the apply channel approval update operation.

    Returns:
        dict[str, Any] value produced by apply_channel_approval_update().
    """
    resolver = resolver_provider()
    decision = resolver.lookup_decision(decision_id) if hasattr(resolver, "lookup_decision") else None
    decision_payload = decision.to_dict() if decision is not None else {}
    return {
        "schema_version": SCHEMA_VERSION,
        "decision_id": decision_id,
        "channel_id": channel_id,
        "actor_id": actor_id,
        "approved": bool(approved),
        "accepted": bool(approved and decision_payload.get("allowed", False)),
        "matched_step": str(decision_payload.get("matched_step", "decision_unavailable")),
        "fallback_rule": str(decision_payload.get("fallback_rule", "deny_by_default")),
        "ordered_trace": list(decision_payload.get("ordered_trace", ())),
        "action_fingerprint": str(decision_payload.get("action_fingerprint", "")),
        "run_id": str(decision_payload.get("run_id", "")),
    }
