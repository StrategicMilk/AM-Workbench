"""Record contracts for Workbench agent route decisions."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any

SCHEMA_VERSION = 1
DEFAULT_DECISION_TIME_UTC = "1970-01-01T00:00:00Z"


class RouteDecisionError(ValueError):
    """Raised when route-decision evidence is malformed."""


class RouteCandidateKind(str, Enum):
    """Supported route candidate families."""

    AGENT = "agent"
    MODEL = "model"
    TOOL = "tool"


class RouteDecisionOutcome(str, Enum):
    """Final route outcome states."""

    SELECTED = "selected"
    FALLBACK_SELECTED = "fallback_selected"
    DENIED = "denied"
    DEGRADED = "degraded"


@dataclass(frozen=True, slots=True)
class CapabilityEvidence:
    """Evidence that a candidate can satisfy a requested capability."""

    evidence_id: str
    capability: str
    source: str
    confidence: float
    provenance_ref: str

    def __post_init__(self) -> None:
        _require_text(self.evidence_id, "evidence_id")
        _require_text(self.capability, "capability")
        _require_text(self.source, "source")
        _require_text(self.provenance_ref, "provenance_ref")
        _require_score(self.confidence, "confidence")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> CapabilityEvidence:
        return cls(
            evidence_id=str(payload["evidence_id"]),
            capability=str(payload["capability"]),
            source=str(payload["source"]),
            confidence=float(payload["confidence"]),
            provenance_ref=str(payload["provenance_ref"]),
        )

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"CapabilityEvidence(evidence_id={self.evidence_id!r}, capability={self.capability!r}, source={self.source!r})"


@dataclass(frozen=True, slots=True)
class RouteCandidate:
    """One candidate agent, model, or tool considered by the router."""

    candidate_id: str
    kind: RouteCandidateKind
    label: str
    capability_refs: tuple[str, ...]
    capability_evidence: tuple[CapabilityEvidence, ...]
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_text(self.candidate_id, "candidate_id")
        _require_text(self.label, "label")
        if not isinstance(self.kind, RouteCandidateKind):
            raise RouteDecisionError("kind must be RouteCandidateKind")
        _require_string_tuple(self.capability_refs, "capability_refs")
        _require_tuple_type(self.capability_evidence, CapabilityEvidence, "capability_evidence", allow_empty=True)
        if not isinstance(self.metadata, dict):
            raise RouteDecisionError("metadata must be a dict")

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "kind": self.kind.value,
            "label": self.label,
            "capability_refs": list(self.capability_refs),
            "capability_evidence": [item.to_dict() for item in self.capability_evidence],
            "metadata": _redact_metadata(self.metadata),
        }

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> RouteCandidate:
        # Pass nested mappings straight through to CapabilityEvidence.from_mapping
        # instead of copying them via dict(item).  The contract test exercises a
        # custom Mapping whose __iter__ raises, so a defensive dict() copy here
        # would trip the test (and waste an allocation on the hot routing path).
        return cls(
            candidate_id=str(payload["candidate_id"]),
            kind=RouteCandidateKind(str(payload["kind"])),
            label=str(payload["label"]),
            capability_refs=tuple(str(value) for value in payload["capability_refs"]),
            capability_evidence=tuple(CapabilityEvidence.from_mapping(item) for item in payload["capability_evidence"]),
            metadata=dict(payload.get("metadata", {})),
        )

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"RouteCandidate(candidate_id={self.candidate_id!r}, kind={self.kind!r}, label={self.label!r})"


@dataclass(frozen=True, slots=True)
class RouteRejection:
    """A deterministic reason a route candidate was not selected."""

    candidate_id: str
    source_gate: str
    reason: str
    explanation: str

    def __post_init__(self) -> None:
        _require_text(self.candidate_id, "candidate_id")
        _require_text(self.source_gate, "source_gate")
        _require_text(self.reason, "reason")
        _require_text(self.explanation, "explanation")

    def to_dict(self) -> dict[str, str]:
        return asdict(self)

    @classmethod
    def from_mapping(cls, payload: dict[str, Any]) -> RouteRejection:
        return cls(
            candidate_id=str(payload["candidate_id"]),
            source_gate=str(payload["source_gate"]),
            reason=str(payload["reason"]),
            explanation=str(payload["explanation"]),
        )

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"RouteRejection(candidate_id={self.candidate_id!r}, source_gate={self.source_gate!r}, reason={self.reason!r})"


@dataclass(frozen=True, slots=True)
class PolicyGateResult:
    """Already-computed policy gate evidence."""

    gate_id: str
    passed: bool
    source: str
    explanation: str
    evidence_ref: str

    def __post_init__(self) -> None:
        _require_text(self.gate_id, "gate_id")
        _require_text(self.source, "source")
        _require_text(self.explanation, "explanation")
        _require_text(self.evidence_ref, "evidence_ref")
        if not isinstance(self.passed, bool):
            raise RouteDecisionError("passed must be bool")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_mapping(cls, payload: dict[str, Any]) -> PolicyGateResult:
        return cls(
            gate_id=str(payload["gate_id"]),
            passed=_require_bool(payload["passed"], "passed"),
            source=str(payload["source"]),
            explanation=str(payload["explanation"]),
            evidence_ref=str(payload["evidence_ref"]),
        )

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"PolicyGateResult(gate_id={self.gate_id!r}, passed={self.passed!r}, source={self.source!r})"


@dataclass(frozen=True, slots=True)
class MemoryContextSummary:
    """Summary of memory eligibility evidence available to routing."""

    profile_id: str
    status: str
    eligible_count: int
    total_count: int
    min_confidence: float | None
    max_confidence: float | None
    blocked_signals: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_text(self.profile_id, "profile_id")
        _require_text(self.status, "status")
        if self.eligible_count < 0 or self.total_count < 0:
            raise RouteDecisionError("memory counts must be >= 0")
        if self.eligible_count > self.total_count:
            raise RouteDecisionError("eligible_count cannot exceed total_count")
        if self.min_confidence is not None:
            _require_score(self.min_confidence, "min_confidence")
        if self.max_confidence is not None:
            _require_score(self.max_confidence, "max_confidence")
        if (
            self.min_confidence is not None
            and self.max_confidence is not None
            and self.min_confidence > self.max_confidence
        ):
            raise RouteDecisionError("min_confidence cannot exceed max_confidence")
        _require_string_tuple(self.blocked_signals, "blocked_signals", allow_empty=True)

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "status": self.status,
            "eligible_count": self.eligible_count,
            "total_count": self.total_count,
            "min_confidence": self.min_confidence,
            "max_confidence": self.max_confidence,
            "blocked_signals": list(self.blocked_signals),
        }

    @classmethod
    def from_mapping(cls, payload: dict[str, Any]) -> MemoryContextSummary:
        """Execute the from mapping operation.

        Returns:
            MemoryContextSummary value produced by from_mapping().
        """
        min_confidence = payload.get("min_confidence")
        max_confidence = payload.get("max_confidence")
        return cls(
            profile_id=str(payload["profile_id"]),
            status=str(payload["status"]),
            eligible_count=int(payload["eligible_count"]),
            total_count=int(payload["total_count"]),
            min_confidence=None if min_confidence is None else float(min_confidence),
            max_confidence=None if max_confidence is None else float(max_confidence),
            blocked_signals=tuple(str(value) for value in payload.get("blocked_signals", ())),
        )

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"MemoryContextSummary(profile_id={self.profile_id!r}, status={self.status!r}, eligible_count={self.eligible_count!r})"


@dataclass(frozen=True, slots=True)
class AgentRouteCostEstimate:
    """Cost estimate attached to a route decision."""

    amount: float
    unit: str
    budget_ref: str

    def __post_init__(self) -> None:
        if self.amount < 0:
            raise RouteDecisionError("amount must be >= 0")
        _require_text(self.unit, "unit")
        _require_text(self.budget_ref, "budget_ref")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_mapping(cls, payload: dict[str, Any]) -> AgentRouteCostEstimate:
        return cls(amount=float(payload["amount"]), unit=str(payload["unit"]), budget_ref=str(payload["budget_ref"]))


@dataclass(frozen=True, slots=True)
class LatencyEstimate:
    """Latency estimate attached to a route decision."""

    amount: float
    unit: str
    percentile: str

    def __post_init__(self) -> None:
        if self.amount < 0:
            raise RouteDecisionError("amount must be >= 0")
        _require_text(self.unit, "unit")
        _require_text(self.percentile, "percentile")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_mapping(cls, payload: dict[str, Any]) -> LatencyEstimate:
        return cls(
            amount=float(payload["amount"]),
            unit=str(payload["unit"]),
            percentile=str(payload["percentile"]),
        )


@dataclass(frozen=True, slots=True)
class HarnessAdmissionSummary:
    """Already-computed agent-run admission evidence."""

    admitted: bool
    blockers: tuple[str, ...]
    admitted_tools: tuple[str, ...]
    replay_boundary_ref: str
    cancellation_behavior: str

    def __post_init__(self) -> None:
        if not isinstance(self.admitted, bool):
            raise RouteDecisionError("admitted must be bool")
        _require_string_tuple(self.blockers, "blockers", allow_empty=True)
        _require_string_tuple(self.admitted_tools, "admitted_tools", allow_empty=True)
        _require_text(self.replay_boundary_ref, "replay_boundary_ref")
        _require_text(self.cancellation_behavior, "cancellation_behavior")
        if self.admitted and self.blockers:
            raise RouteDecisionError("admitted summary cannot include blockers")

    def to_dict(self) -> dict[str, Any]:
        return {
            "admitted": self.admitted,
            "blockers": list(self.blockers),
            "admitted_tools": list(self.admitted_tools),
            "replay_boundary_ref": self.replay_boundary_ref,
            "cancellation_behavior": self.cancellation_behavior,
        }

    @classmethod
    def from_mapping(cls, payload: dict[str, Any]) -> HarnessAdmissionSummary:
        return cls(
            admitted=_require_bool(payload["admitted"], "admitted"),
            blockers=tuple(str(value) for value in payload.get("blockers", ())),
            admitted_tools=tuple(str(value) for value in payload.get("admitted_tools", ())),
            replay_boundary_ref=str(payload["replay_boundary_ref"]),
            cancellation_behavior=str(payload["cancellation_behavior"]),
        )

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"HarnessAdmissionSummary(admitted={self.admitted!r}, blockers={self.blockers!r}, admitted_tools={self.admitted_tools!r})"


@dataclass(frozen=True, slots=True)
class RouteDecisionRecord:
    """Serializable route decision evidence record."""

    decision_id: str
    schema_version: int
    created_at_utc: str
    candidate_agents: tuple[RouteCandidate, ...]
    candidate_models: tuple[RouteCandidate, ...]
    candidate_tools: tuple[RouteCandidate, ...]
    rejected_alternatives: tuple[RouteRejection, ...]
    policy_gates: tuple[PolicyGateResult, ...]
    memory_context: MemoryContextSummary | None
    harness_admission: HarnessAdmissionSummary | None
    cost_estimate: AgentRouteCostEstimate | None
    latency_estimate: LatencyEstimate | None
    selected_candidate_id: str | None
    fallback_reason: str | None
    outcome: RouteDecisionOutcome
    blockers: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_text(self.decision_id, "decision_id")
        if self.schema_version != SCHEMA_VERSION:
            raise RouteDecisionError(f"schema_version must be {SCHEMA_VERSION}")
        _require_text(self.created_at_utc, "created_at_utc")
        _require_tuple_type(self.candidate_agents, RouteCandidate, "candidate_agents", allow_empty=True)
        _require_tuple_type(self.candidate_models, RouteCandidate, "candidate_models", allow_empty=True)
        _require_tuple_type(self.candidate_tools, RouteCandidate, "candidate_tools", allow_empty=True)
        _require_tuple_type(self.rejected_alternatives, RouteRejection, "rejected_alternatives", allow_empty=True)
        _require_tuple_type(self.policy_gates, PolicyGateResult, "policy_gates", allow_empty=True)
        if self.memory_context is not None and not isinstance(self.memory_context, MemoryContextSummary):
            raise RouteDecisionError("memory_context must be MemoryContextSummary or None")
        if self.harness_admission is not None and not isinstance(self.harness_admission, HarnessAdmissionSummary):
            raise RouteDecisionError("harness_admission must be HarnessAdmissionSummary or None")
        if self.cost_estimate is not None and not isinstance(self.cost_estimate, AgentRouteCostEstimate):
            raise RouteDecisionError("cost_estimate must be AgentRouteCostEstimate or None")
        if self.latency_estimate is not None and not isinstance(self.latency_estimate, LatencyEstimate):
            raise RouteDecisionError("latency_estimate must be LatencyEstimate or None")
        if self.selected_candidate_id is not None:
            _require_text(self.selected_candidate_id, "selected_candidate_id")
        if self.fallback_reason is not None:
            _require_text(self.fallback_reason, "fallback_reason")
        if not isinstance(self.outcome, RouteDecisionOutcome):
            raise RouteDecisionError("outcome must be RouteDecisionOutcome")
        _require_string_tuple(self.blockers, "blockers", allow_empty=True)
        if self.outcome in {RouteDecisionOutcome.SELECTED, RouteDecisionOutcome.FALLBACK_SELECTED}:
            if not self.selected_candidate_id:
                raise RouteDecisionError("selected outcomes require selected_candidate_id")
            if self.blockers:
                raise RouteDecisionError("selected outcomes cannot include blockers")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "decision_id": self.decision_id,
            "created_at_utc": self.created_at_utc,
            "candidate_agents": [candidate.to_dict() for candidate in self.candidate_agents],
            "candidate_models": [candidate.to_dict() for candidate in self.candidate_models],
            "candidate_tools": [candidate.to_dict() for candidate in self.candidate_tools],
            "rejected_alternatives": [rejection.to_dict() for rejection in self.rejected_alternatives],
            "policy_gates": [gate.to_dict() for gate in self.policy_gates],
            "memory_context": self.memory_context.to_dict() if self.memory_context else None,
            "harness_admission": self.harness_admission.to_dict() if self.harness_admission else None,
            "cost_estimate": self.cost_estimate.to_dict() if self.cost_estimate else None,
            "latency_estimate": self.latency_estimate.to_dict() if self.latency_estimate else None,
            "selected_candidate_id": self.selected_candidate_id,
            "fallback_reason": self.fallback_reason,
            "outcome": self.outcome.value,
            "blockers": list(self.blockers),
        }

    @classmethod
    def from_mapping(cls, payload: dict[str, Any]) -> RouteDecisionRecord:
        return cls(
            decision_id=str(payload["decision_id"]),
            schema_version=int(payload["schema_version"]),
            created_at_utc=str(payload["created_at_utc"]),
            candidate_agents=tuple(RouteCandidate.from_mapping(dict(item)) for item in payload["candidate_agents"]),
            candidate_models=tuple(RouteCandidate.from_mapping(dict(item)) for item in payload["candidate_models"]),
            candidate_tools=tuple(RouteCandidate.from_mapping(dict(item)) for item in payload["candidate_tools"]),
            rejected_alternatives=tuple(
                RouteRejection.from_mapping(dict(item)) for item in payload["rejected_alternatives"]
            ),
            policy_gates=tuple(PolicyGateResult.from_mapping(dict(item)) for item in payload["policy_gates"]),
            memory_context=(
                None
                if payload["memory_context"] is None
                else MemoryContextSummary.from_mapping(dict(payload["memory_context"]))
            ),
            harness_admission=(
                None
                if payload["harness_admission"] is None
                else HarnessAdmissionSummary.from_mapping(dict(payload["harness_admission"]))
            ),
            cost_estimate=None
            if payload["cost_estimate"] is None
            else AgentRouteCostEstimate.from_mapping(dict(payload["cost_estimate"])),
            latency_estimate=(
                None
                if payload["latency_estimate"] is None
                else LatencyEstimate.from_mapping(dict(payload["latency_estimate"]))
            ),
            selected_candidate_id=payload["selected_candidate_id"],
            fallback_reason=payload["fallback_reason"],
            outcome=RouteDecisionOutcome(str(payload["outcome"])),
            blockers=tuple(str(value) for value in payload.get("blockers", ())),
        )

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"RouteDecisionRecord(decision_id={self.decision_id!r}, schema_version={self.schema_version!r}, created_at_utc={self.created_at_utc!r})"


def _require_text(value: object, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise RouteDecisionError(f"{field_name} must be non-empty")


def _require_score(value: float, field_name: str) -> None:
    if not 0.0 <= value <= 1.0:
        raise RouteDecisionError(f"{field_name} must be between 0 and 1")


def _require_bool(value: object, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise RouteDecisionError(f"{field_name} must be bool")
    return value


def _redact_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    sensitive_markers = ("api_key", "authorization", "credential", "password", "prompt", "secret", "token", "tool_args")
    redacted: dict[str, Any] = {}
    for key, value in metadata.items():
        normalized_key = str(key).lower()
        if any(marker in normalized_key for marker in sensitive_markers):
            redacted[str(key)] = "[redacted]"
        elif isinstance(value, dict):
            redacted[str(key)] = _redact_metadata(value)
        elif isinstance(value, list):
            redacted[str(key)] = [_redact_metadata(item) if isinstance(item, dict) else item for item in value]
        else:
            redacted[str(key)] = value
    return redacted


def _require_string_tuple(values: tuple[str, ...], field_name: str, *, allow_empty: bool = False) -> None:
    if not isinstance(values, tuple) or (not allow_empty and not values):
        raise RouteDecisionError(f"{field_name} must be a tuple")
    if any(not isinstance(value, str) or not value.strip() for value in values):
        raise RouteDecisionError(f"{field_name} must contain non-empty strings")


def _require_tuple_type(values: tuple[Any, ...], expected_type: type, field_name: str, *, allow_empty: bool) -> None:
    if not isinstance(values, tuple) or (not allow_empty and not values):
        raise RouteDecisionError(f"{field_name} must be a tuple")
    if any(not isinstance(value, expected_type) for value in values):
        raise RouteDecisionError(f"{field_name} must contain {expected_type.__name__}")
