"""Decision rendering helpers for Workbench approval chains."""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

from vetinari.workbench.approval_chain_models import (
    SCHEMA_VERSION,
    ApprovalChainDecision,
    ApprovalChainOutcome,
    ApprovalChainReason,
    ApprovalChainRequest,
    ApprovalChainStep,
    _utc_now,
)


def render_approval_chain_explanation(decision_payload: dict[str, Any]) -> str:
    """Render a deterministic explanation matching the receipt payload.

    Returns:
        str value produced by render_approval_chain_explanation().
    """
    compact = _compact_explanation_payload(decision_payload)
    return json.dumps(compact, sort_keys=True, separators=(",", ":"))


def _compact_explanation_payload(decision_payload: dict[str, Any]) -> dict[str, Any]:
    required_keys = (
        "outcome",
        "matched_step",
        "fallback_rule",
        "human_approval_required",
        "ordered_trace",
    )
    missing = tuple(key for key in required_keys if key not in decision_payload)
    if missing:
        raise ValueError(f"approval-chain explanation payload missing required keys: {', '.join(missing)}")
    return {key: decision_payload[key] for key in required_keys}


def _build_decision(
    *,
    request: ApprovalChainRequest,
    outcome: ApprovalChainOutcome,
    matched_step: str,
    fallback_rule: str,
    ordered_trace: tuple[ApprovalChainStep, ...],
    reason: ApprovalChainReason,
    detail: str,
) -> ApprovalChainDecision:
    payload = {
        "schema_version": SCHEMA_VERSION,
        "decision_id": f"approval-chain-{uuid4().hex}",
        "project_id": request.project_id,
        "session_id": request.session_id,
        "action_id": request.action_id,
        "action_fingerprint": request.fingerprint,
        "channel": request.channel.value,
        "outcome": outcome.value,
        "allowed": outcome is ApprovalChainOutcome.ALLOW,
        "human_approval_required": outcome is ApprovalChainOutcome.REQUIRE_HUMAN_APPROVAL,
        "matched_step": matched_step,
        "fallback_rule": fallback_rule,
        "ordered_trace": [step.to_dict() for step in ordered_trace],
        "reason": reason.value,
        "detail": detail,
        "decided_at_utc": _utc_now().isoformat(),
    }
    receipt_payload = dict(payload)
    receipt_payload["receipt_kind"] = "workbench_approval_chain_decision"
    rendered = render_approval_chain_explanation(payload)
    return ApprovalChainDecision(
        decision_id=payload["decision_id"],
        schema_version=SCHEMA_VERSION,
        project_id=request.project_id,
        session_id=request.session_id,
        action_id=request.action_id,
        action_fingerprint=request.fingerprint,
        channel=request.channel.value,
        outcome=outcome,
        allowed=outcome is ApprovalChainOutcome.ALLOW,
        human_approval_required=outcome is ApprovalChainOutcome.REQUIRE_HUMAN_APPROVAL,
        matched_step=matched_step,
        fallback_rule=fallback_rule,
        ordered_trace=ordered_trace,
        receipt_payload=receipt_payload,
        rendered_explanation=rendered,
        decided_at_utc=payload["decided_at_utc"],
    )


def _minimal_closed_config() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "ordered_steps": [{"name": "deny_by_default"}],
        "protected_path_prefixes": [],
        "hard_deny_indicators": [],
        "destructive_indicators": [],
        "dlp_indicators": [],
        "tool_pin_indicators": [],
        "session_allow": {"default_ttl_seconds": 60, "max_ttl_seconds": 60},
        "fallback_text": "deny-by-default because approval-chain config is unreadable",
    }


def _bounded_error(exc: Exception) -> str:
    message = f"{type(exc).__name__}: {exc}"
    return message[:240]


def _contains_indicator(request: ApprovalChainRequest, indicators: Any) -> bool:
    haystack = " ".join([
        request.action_type,
        request.summary,
        json.dumps(request.details, sort_keys=True, default=str),
        json.dumps(request.metadata, sort_keys=True, default=str),
    ]).lower()
    return any(str(indicator).lower() in haystack for indicator in indicators if str(indicator).strip())


def _first_protected_path(paths: tuple[str, ...], prefixes: Any) -> str:
    normalized_prefixes = tuple(_normalize_path(prefix) for prefix in prefixes if str(prefix).strip())
    for path in paths:
        normalized = _normalize_path(path)
        if any(normalized.startswith(prefix) for prefix in normalized_prefixes):
            return path
    return ""


def _normalize_path(value: object) -> str:
    return str(value).replace("\\", "/").strip().lower()
