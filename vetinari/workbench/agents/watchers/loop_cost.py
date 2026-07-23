"""Loop, cost, and state-predicate watcher assessment."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from math import isfinite
from typing import Any
from uuid import uuid4

from vetinari.workbench.agents.watchers.loop_cost_records import (
    EvidenceSummary,
    LoopCostDetectedPattern,
    LoopCostReasonCode,
    LoopCostWatcherAction,
    LoopCostWatcherDecision,
    LoopCostWatcherInput,
    PolicyVerdictSnapshot,
    ResourceCounterSnapshot,
    RetrySignature,
    StatePredicate,
    TraceEventRef,
)

logger = logging.getLogger(__name__)


_SCHEMA_VERSION = "1.0"
_REPEATED_TOOL_THRESHOLD = 3
_LOW_VALUE_RETRY_THRESHOLD = 3


def assess_loop_cost_state(watcher_input: LoopCostWatcherInput) -> LoopCostWatcherDecision:
    """Assess loop, cost, and state predicates without mutating runtime state.

    Returns:
        LoopCostWatcherDecision value produced by assess_loop_cost_state().
    """
    reasons: list[LoopCostReasonCode] = []
    patterns: list[LoopCostDetectedPattern] = []
    actions: list[LoopCostWatcherAction] = []
    evidence = _evidence_summary(watcher_input)

    if not evidence.any_refs():
        reasons.append(LoopCostReasonCode.MISSING_EVIDENCE)
        patterns.append(LoopCostDetectedPattern.MISSING_EVIDENCE)
        actions.append(LoopCostWatcherAction.ASK_USER)

    _assess_policy_verdicts(watcher_input.policy_verdicts, reasons, patterns, actions)
    _assess_trace_events(watcher_input.trace_events, reasons, patterns, actions)
    _assess_resource_counters(watcher_input.resource_counters, reasons, patterns, actions)
    _assess_retry_signatures(watcher_input.retry_signatures, reasons, patterns, actions)
    _assess_state_predicates(watcher_input.state_predicates, reasons, patterns, actions)

    if not reasons:
        reasons.append(LoopCostReasonCode.ALLOWED)
    action = _most_severe(actions)
    return LoopCostWatcherDecision(
        schema_version=_SCHEMA_VERSION,
        decision_id=f"loop-cost-decision-{uuid4().hex}",
        run_id=watcher_input.run_id,
        actor_id=watcher_input.actor_id,
        recommended_action=action,
        reason_codes=tuple(dict.fromkeys(reasons)),
        detected_patterns=tuple(dict.fromkeys(patterns)),
        evidence_summary=evidence,
        decided_at_utc=_utc_now_iso(),
        summary=_summary_for(action, reasons, watcher_input.summary),
        degraded=action is not LoopCostWatcherAction.OBSERVE,
    )


def loop_cost_input_from_metadata(
    *,
    run_id: str,
    actor_id: str,
    metadata: dict[str, Any],
    evidence_refs: tuple[str, ...],
    summary: str = "",
) -> LoopCostWatcherInput | None:
    """Build a watcher input from observation metadata when the branch is requested.

    Returns:
        LoopCostWatcherInput | None value produced by loop_cost_input_from_metadata().
    """
    raw = metadata.get("loop_cost_state")
    if not isinstance(raw, dict):
        return None
    return LoopCostWatcherInput(
        run_id=_text(raw.get("run_id")) or run_id,
        actor_id=_text(raw.get("actor_id")) or actor_id,
        trace_events=tuple(_trace_event(item) for item in raw.get("trace_events", ()) if isinstance(item, dict)),
        policy_verdicts=tuple(
            _policy_verdict(item) for item in raw.get("policy_verdicts", ()) if isinstance(item, dict)
        ),
        resource_counters=tuple(
            _resource_counter(item) for item in raw.get("resource_counters", ()) if isinstance(item, dict)
        ),
        retry_signatures=tuple(
            _retry_signature(item) for item in raw.get("retry_signatures", ()) if isinstance(item, dict)
        ),
        state_predicates=tuple(
            _state_predicate(item) for item in raw.get("state_predicates", ()) if isinstance(item, dict)
        ),
        evidence_summary=EvidenceSummary(
            evidence_refs=_clean_tuple(raw.get("evidence_refs", evidence_refs)),
            trace_event_refs=_clean_tuple(raw.get("trace_event_refs", ())),
            policy_verdict_refs=_clean_tuple(raw.get("policy_verdict_refs", ())),
            resource_counter_refs=_clean_tuple(raw.get("resource_counter_refs", ())),
            retry_signature_refs=_clean_tuple(raw.get("retry_signature_refs", ())),
            predicate_refs=_clean_tuple(raw.get("predicate_refs", ())),
        ),
        observed_at_utc=_text(raw.get("observed_at_utc")),
        summary=_text(raw.get("summary")) or summary,
    )


def _assess_policy_verdicts(
    verdicts: tuple[PolicyVerdictSnapshot, ...],
    reasons: list[LoopCostReasonCode],
    patterns: list[LoopCostDetectedPattern],
    actions: list[LoopCostWatcherAction],
) -> None:
    for verdict in verdicts:
        value = verdict.value.strip().lower()
        mode = verdict.mode.strip().lower()
        state = verdict.state.strip().lower()
        if state not in {"fresh", "stale", "unknown"}:
            reasons.append(LoopCostReasonCode.UNKNOWN_POLICY_STATE)
            patterns.append(LoopCostDetectedPattern.UNKNOWN_POLICY_STATE)
            actions.append(LoopCostWatcherAction.STRICT_BLOCK)
        if state == "unknown":
            reasons.append(LoopCostReasonCode.UNKNOWN_POLICY_STATE)
            patterns.append(LoopCostDetectedPattern.UNKNOWN_POLICY_STATE)
            actions.append(LoopCostWatcherAction.STRICT_BLOCK)
        if state == "stale":
            reasons.append(LoopCostReasonCode.STALE_APPROVAL_STATE)
            patterns.append(LoopCostDetectedPattern.STALE_APPROVAL_STATE)
            actions.append(LoopCostWatcherAction.ASK_USER)
        if value not in {"allow", "warn", "block", "escalate"}:
            reasons.append(LoopCostReasonCode.UNKNOWN_POLICY_VERDICT)
            patterns.append(LoopCostDetectedPattern.UNKNOWN_POLICY_STATE)
            actions.append(LoopCostWatcherAction.STRICT_BLOCK)
        if value == "block" or mode == "strict":
            reasons.append(LoopCostReasonCode.POLICY_STRICT_BLOCK)
            actions.append(LoopCostWatcherAction.STRICT_BLOCK)


def _assess_trace_events(
    events: tuple[TraceEventRef, ...],
    reasons: list[LoopCostReasonCode],
    patterns: list[LoopCostDetectedPattern],
    actions: list[LoopCostWatcherAction],
) -> None:
    if len(events) < _REPEATED_TOOL_THRESHOLD:
        return
    tail = events[-_REPEATED_TOOL_THRESHOLD:]
    signatures = {(event.tool_name, event.signature) for event in tail}
    if len(signatures) != 1:
        return
    if all(event.failed_call or not event.succeeded for event in tail):
        reasons.append(LoopCostReasonCode.FAILED_TOOL_CALL_LOOP)
        patterns.append(LoopCostDetectedPattern.FAILED_TOOL_LOOP)
        actions.append(LoopCostWatcherAction.PAUSE)
        return
    if len(events) >= _REPEATED_TOOL_THRESHOLD + 2:
        reasons.append(LoopCostReasonCode.REPEATED_TOOL_LOOP)
        patterns.append(LoopCostDetectedPattern.REPEATED_TOOL_LOOP)
        actions.append(LoopCostWatcherAction.PAUSE)


def _assess_resource_counters(
    counters: tuple[ResourceCounterSnapshot, ...],
    reasons: list[LoopCostReasonCode],
    patterns: list[LoopCostDetectedPattern],
    actions: list[LoopCostWatcherAction],
) -> None:
    for counter in counters:
        cost = _number(counter.cost_usd)
        budget = _number(counter.budget_usd)
        if not counter.readable or cost is None or budget is None or budget <= 0:
            reasons.append(LoopCostReasonCode.UNREADABLE_COST_COUNTER)
            patterns.append(LoopCostDetectedPattern.UNREADABLE_COUNTER)
            actions.append(LoopCostWatcherAction.ASK_USER)
            continue
        ratio = cost / budget
        if ratio >= counter.strict_block_ratio:
            reasons.append(LoopCostReasonCode.COST_STRICT_BLOCK)
            patterns.append(LoopCostDetectedPattern.HIGH_COST_DRIFT)
            actions.append(LoopCostWatcherAction.STRICT_BLOCK)
        elif ratio >= counter.approval_ratio:
            reasons.append(LoopCostReasonCode.COST_APPROVAL_REQUIRED)
            patterns.append(LoopCostDetectedPattern.HIGH_COST_DRIFT)
            actions.append(LoopCostWatcherAction.ASK_USER)
        elif ratio >= counter.warning_ratio:
            reasons.append(LoopCostReasonCode.COST_WARNING_DRIFT)
            patterns.append(LoopCostDetectedPattern.HIGH_COST_DRIFT)
            actions.append(LoopCostWatcherAction.DOWNGRADE)


def _assess_retry_signatures(
    retries: tuple[RetrySignature, ...],
    reasons: list[LoopCostReasonCode],
    patterns: list[LoopCostDetectedPattern],
    actions: list[LoopCostWatcherAction],
) -> None:
    for retry in retries:
        if retry.attempt_count >= _LOW_VALUE_RETRY_THRESHOLD and not _clean_tuple(retry.new_evidence_refs):
            reasons.append(LoopCostReasonCode.LOW_VALUE_SELF_RETRY_COLLAPSE)
            patterns.append(LoopCostDetectedPattern.LOW_VALUE_SELF_RETRY)
            actions.append(LoopCostWatcherAction.PAUSE)


def _assess_state_predicates(
    predicates: tuple[StatePredicate, ...],
    reasons: list[LoopCostReasonCode],
    patterns: list[LoopCostDetectedPattern],
    actions: list[LoopCostWatcherAction],
) -> None:
    now = datetime.now(timezone.utc)
    for predicate in predicates:
        observed = _parse_utc(predicate.observed_at_utc)
        if observed is None:
            reasons.append(LoopCostReasonCode.MISSING_PREDICATE_TIMESTAMP)
            patterns.append(LoopCostDetectedPattern.STALE_STATE_PREDICATE)
            actions.append(LoopCostWatcherAction.STRICT_BLOCK)
        elif (now - observed).total_seconds() > predicate.max_age_seconds:
            reasons.append(LoopCostReasonCode.STALE_STATE_PREDICATE)
            patterns.append(LoopCostDetectedPattern.STALE_STATE_PREDICATE)
            actions.append(LoopCostWatcherAction.ASK_USER)
        if predicate.value != predicate.expected_value:
            reasons.append(LoopCostReasonCode.POLICY_STATE_MISMATCH)
            patterns.append(LoopCostDetectedPattern.POLICY_STATE_MISMATCH)
            actions.append(LoopCostWatcherAction.STRICT_BLOCK)
        if predicate.policy_state == "stale":
            reasons.append(LoopCostReasonCode.STALE_APPROVAL_STATE)
            patterns.append(LoopCostDetectedPattern.STALE_APPROVAL_STATE)
            actions.append(LoopCostWatcherAction.ASK_USER)
        elif predicate.policy_state not in {"fresh", "stale"}:
            reasons.append(LoopCostReasonCode.UNKNOWN_POLICY_STATE)
            patterns.append(LoopCostDetectedPattern.UNKNOWN_POLICY_STATE)
            actions.append(LoopCostWatcherAction.STRICT_BLOCK)


def _evidence_summary(watcher_input: LoopCostWatcherInput) -> EvidenceSummary:
    provided = watcher_input.evidence_summary
    return EvidenceSummary(
        trace_event_refs=provided.trace_event_refs
        or tuple(event.event_ref for event in watcher_input.trace_events if event.event_ref.strip()),
        policy_verdict_refs=provided.policy_verdict_refs
        or tuple(verdict.verdict_ref for verdict in watcher_input.policy_verdicts if verdict.verdict_ref.strip()),
        resource_counter_refs=provided.resource_counter_refs
        or tuple(counter.counter_ref for counter in watcher_input.resource_counters if counter.counter_ref.strip()),
        retry_signature_refs=provided.retry_signature_refs
        or tuple(retry.signature_ref for retry in watcher_input.retry_signatures if retry.signature_ref.strip()),
        predicate_refs=provided.predicate_refs
        or tuple(
            predicate.predicate_ref for predicate in watcher_input.state_predicates if predicate.predicate_ref.strip()
        ),
        evidence_refs=provided.evidence_refs,
    )


def _most_severe(actions: list[LoopCostWatcherAction]) -> LoopCostWatcherAction:
    severity = {
        LoopCostWatcherAction.OBSERVE: 0,
        LoopCostWatcherAction.PAUSE: 1,
        LoopCostWatcherAction.DOWNGRADE: 2,
        LoopCostWatcherAction.ASK_USER: 3,
        LoopCostWatcherAction.STRICT_BLOCK: 4,
    }
    return max(actions or [LoopCostWatcherAction.OBSERVE], key=lambda action: severity[action])


def _summary_for(
    action: LoopCostWatcherAction,
    reasons: list[LoopCostReasonCode],
    fallback: str,
) -> str:
    if fallback and action is LoopCostWatcherAction.OBSERVE:
        return fallback
    reason_text = ", ".join(reason.value for reason in dict.fromkeys(reasons))
    return f"loop/cost/state watcher recommends {action.value}: {reason_text}"


def _trace_event(raw: dict[str, Any]) -> TraceEventRef:
    return TraceEventRef(
        event_ref=_text(raw.get("event_ref")),
        tool_name=_text(raw.get("tool_name")),
        signature=_text(raw.get("signature")),
        succeeded=bool(raw.get("succeeded")),
        failed_call=bool(raw.get("failed_call")),
    )


def _policy_verdict(raw: dict[str, Any]) -> PolicyVerdictSnapshot:
    return PolicyVerdictSnapshot(
        verdict_ref=_text(raw.get("verdict_ref")),
        value=_text(raw.get("value")) or "unknown",
        mode=_text(raw.get("mode")) or "observe",
        state=_text(raw.get("state")) or "fresh",
        reason_code=_text(raw.get("reason_code")),
    )


def _resource_counter(raw: dict[str, Any]) -> ResourceCounterSnapshot:
    warning_ratio = _number(raw.get("warning_ratio", 0.8))
    approval_ratio = _number(raw.get("approval_ratio", 1.0))
    strict_block_ratio = _number(raw.get("strict_block_ratio", 2.0))
    readable = bool(raw.get("readable", True)) and None not in (warning_ratio, approval_ratio, strict_block_ratio)
    return ResourceCounterSnapshot(
        counter_ref=_text(raw.get("counter_ref")),
        cost_usd=raw.get("cost_usd"),
        budget_usd=raw.get("budget_usd"),
        warning_ratio=warning_ratio or 0.8,
        approval_ratio=approval_ratio or 1.0,
        strict_block_ratio=strict_block_ratio or 2.0,
        readable=readable,
    )


def _retry_signature(raw: dict[str, Any]) -> RetrySignature:
    return RetrySignature(
        signature_ref=_text(raw.get("signature_ref")),
        signature=_text(raw.get("signature")),
        attempt_count=_int_or_zero(raw.get("attempt_count", 0)),
        evidence_refs=_clean_tuple(raw.get("evidence_refs", ())),
        new_evidence_refs=_clean_tuple(raw.get("new_evidence_refs", ())),
        hypothesis=_text(raw.get("hypothesis")),
    )


def _state_predicate(raw: dict[str, Any]) -> StatePredicate:
    return StatePredicate(
        predicate_ref=_text(raw.get("predicate_ref")),
        name=_text(raw.get("name")),
        value=_text(raw.get("value")),
        expected_value=_text(raw.get("expected_value")),
        observed_at_utc=_text(raw.get("observed_at_utc")),
        max_age_seconds=_int_or_default(raw.get("max_age_seconds", 300), 300),
        policy_state=_text(raw.get("policy_state")) or "fresh",
    )


def _clean_tuple(values: object) -> tuple[str, ...]:
    if not isinstance(values, (list, tuple)):
        return ()
    return tuple(str(value) for value in values if str(value).strip())


def _text(value: object) -> str:
    return str(value).strip() if value is not None else ""


def _number(value: float | int | str | None) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        logger.warning("Handled recoverable failure before fallback.", exc_info=True)
        return None
    return number if isfinite(number) else None


def _int_or_zero(value: object) -> int:
    return _int_or_default(value, 0)


def _int_or_default(value: object, default: int) -> int:
    if not isinstance(value, (str, bytes, int, float)):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        logger.warning("Handled recoverable failure before fallback.", exc_info=True)
        return default


def _parse_utc(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        logger.warning("Handled recoverable failure before fallback.", exc_info=True)
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


__all__ = [
    "EvidenceSummary",
    "LoopCostDetectedPattern",
    "LoopCostReasonCode",
    "LoopCostWatcherAction",
    "LoopCostWatcherDecision",
    "LoopCostWatcherInput",
    "PolicyVerdictSnapshot",
    "ResourceCounterSnapshot",
    "RetrySignature",
    "StatePredicate",
    "TraceEventRef",
    "assess_loop_cost_state",
    "loop_cost_input_from_metadata",
]
