"""Record contracts for loop, cost, and state-predicate watcher assessment."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class LoopCostWatcherAction(str, Enum):
    """Intervention actions exposed by the loop/cost/state watcher."""

    OBSERVE = "observe"
    PAUSE = "pause"
    DOWNGRADE = "downgrade"
    ASK_USER = "ask_user"
    STRICT_BLOCK = "strict_block"


class LoopCostDetectedPattern(str, Enum):
    """Replayable pattern classes detected by the watcher."""

    REPEATED_TOOL_LOOP = "repeated_tool_loop"
    FAILED_TOOL_LOOP = "failed_tool_loop"
    HIGH_COST_DRIFT = "high_cost_drift"
    STALE_APPROVAL_STATE = "stale_approval_state"
    STALE_STATE_PREDICATE = "stale_state_predicate"
    POLICY_STATE_MISMATCH = "policy_state_mismatch"
    LOW_VALUE_SELF_RETRY = "low_value_self_retry"
    UNREADABLE_COUNTER = "unreadable_counter"
    UNKNOWN_POLICY_STATE = "unknown_policy_state"
    UNKNOWN_POLICY_VERDICT = "unknown_policy_verdict"
    MISSING_EVIDENCE = "missing_evidence"


class LoopCostReasonCode(str, Enum):
    """Machine-readable reason codes for watcher decisions."""

    ALLOWED = "allowed"
    MISSING_EVIDENCE = "missing_evidence"
    REPEATED_TOOL_LOOP = "repeated_tool_loop"
    FAILED_TOOL_CALL_LOOP = "failed_tool_call_loop"
    COST_WARNING_DRIFT = "cost_warning_drift"
    COST_APPROVAL_REQUIRED = "cost_approval_required"
    COST_STRICT_BLOCK = "cost_strict_block"
    UNREADABLE_COST_COUNTER = "unreadable_cost_counter"
    STALE_APPROVAL_STATE = "stale_approval_state"
    STALE_STATE_PREDICATE = "stale_state_predicate"
    MISSING_PREDICATE_TIMESTAMP = "missing_predicate_timestamp"
    POLICY_STATE_MISMATCH = "policy_state_mismatch"
    LOW_VALUE_SELF_RETRY_COLLAPSE = "low_value_self_retry_collapse"
    POLICY_STRICT_BLOCK = "policy_strict_block"
    UNKNOWN_POLICY_STATE = "unknown_policy_state"
    UNKNOWN_POLICY_VERDICT = "unknown_policy_verdict"


@dataclass(frozen=True, slots=True)
class TraceEventRef:
    """Compact trace event facts used to identify repeated tool patterns."""

    event_ref: str
    tool_name: str
    signature: str
    succeeded: bool
    failed_call: bool = False

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return (
            f"TraceEventRef(event_ref={self.event_ref!r}, tool_name={self.tool_name!r}, signature={self.signature!r})"
        )


@dataclass(frozen=True, slots=True)
class PolicyVerdictSnapshot:
    """Stable policy verdict facts consumed without owning policy files."""

    verdict_ref: str
    value: str
    mode: str = "observe"
    state: str = "fresh"
    reason_code: str = ""

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"PolicyVerdictSnapshot(verdict_ref={self.verdict_ref!r}, value={self.value!r}, mode={self.mode!r})"


@dataclass(frozen=True, slots=True)
class ResourceCounterSnapshot:
    """Cost/resource counter facts for budget-drift decisions."""

    counter_ref: str
    cost_usd: float | int | str | None
    budget_usd: float | int | str | None
    warning_ratio: float = 0.8
    approval_ratio: float = 1.0
    strict_block_ratio: float = 2.0
    readable: bool = True

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"ResourceCounterSnapshot(counter_ref={self.counter_ref!r}, cost_usd={self.cost_usd!r}, budget_usd={self.budget_usd!r})"


@dataclass(frozen=True, slots=True)
class RetrySignature:
    """Self-retry facts used to detect low-value retry collapse."""

    signature_ref: str
    signature: str
    attempt_count: int
    evidence_refs: tuple[str, ...] = ()
    new_evidence_refs: tuple[str, ...] = ()
    hypothesis: str = ""

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"RetrySignature(signature_ref={self.signature_ref!r}, signature={self.signature!r}, attempt_count={self.attempt_count!r})"


@dataclass(frozen=True, slots=True)
class StatePredicate:
    """State predicate facts that must remain fresh and policy-aligned."""

    predicate_ref: str
    name: str
    value: str
    expected_value: str
    observed_at_utc: str
    max_age_seconds: int = 300
    policy_state: str = "fresh"

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"StatePredicate(predicate_ref={self.predicate_ref!r}, name={self.name!r}, value={self.value!r})"


@dataclass(frozen=True, slots=True)
class EvidenceSummary:
    """Durable refs preserved for Mission Control replay."""

    trace_event_refs: tuple[str, ...] = ()
    policy_verdict_refs: tuple[str, ...] = ()
    resource_counter_refs: tuple[str, ...] = ()
    retry_signature_refs: tuple[str, ...] = ()
    predicate_refs: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()

    def any_refs(self) -> bool:
        return any(
            (
                self.trace_event_refs,
                self.policy_verdict_refs,
                self.resource_counter_refs,
                self.retry_signature_refs,
                self.predicate_refs,
                self.evidence_refs,
            ),
        )

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"EvidenceSummary(trace_event_refs={self.trace_event_refs!r}, policy_verdict_refs={self.policy_verdict_refs!r}, resource_counter_refs={self.resource_counter_refs!r})"


@dataclass(frozen=True, slots=True)
class LoopCostWatcherInput:
    """Side-effect-free input contract for loop/cost/state assessment."""

    run_id: str
    actor_id: str
    trace_events: tuple[TraceEventRef, ...] = ()
    policy_verdicts: tuple[PolicyVerdictSnapshot, ...] = ()
    resource_counters: tuple[ResourceCounterSnapshot, ...] = ()
    retry_signatures: tuple[RetrySignature, ...] = ()
    state_predicates: tuple[StatePredicate, ...] = ()
    evidence_summary: EvidenceSummary = field(default_factory=EvidenceSummary)
    observed_at_utc: str = ""
    summary: str = ""

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"LoopCostWatcherInput(run_id={self.run_id!r}, actor_id={self.actor_id!r}, trace_events={self.trace_events!r})"


@dataclass(frozen=True, slots=True)
class LoopCostWatcherDecision:
    """Replayable loop/cost/state decision payload."""

    schema_version: str
    decision_id: str
    run_id: str
    actor_id: str
    recommended_action: LoopCostWatcherAction
    reason_codes: tuple[LoopCostReasonCode, ...]
    detected_patterns: tuple[LoopCostDetectedPattern, ...]
    evidence_summary: EvidenceSummary
    decided_at_utc: str
    summary: str
    degraded: bool

    def to_schema_payload(self) -> dict[str, Any]:
        """Return a JSON-schema-ready payload for Mission Control consumers.

        Returns:
            dict[str, Any] value produced by to_schema_payload().
        """
        payload = asdict(self)
        payload["recommended_action"] = self.recommended_action.value
        payload["reason_codes"] = [reason.value for reason in self.reason_codes]
        payload["detected_patterns"] = [pattern.value for pattern in self.detected_patterns]
        payload["evidence_summary"] = {key: list(value) for key, value in payload["evidence_summary"].items()}
        return payload

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"LoopCostWatcherDecision(schema_version={self.schema_version!r}, decision_id={self.decision_id!r}, run_id={self.run_id!r})"
