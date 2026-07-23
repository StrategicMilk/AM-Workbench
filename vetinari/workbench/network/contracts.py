"""Contracts for Workbench network transport optimization."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from vetinari.api.responses import json_safe as _json_safe


class NetworkTransportError(ValueError):
    """Raised when network evidence or policy cannot be trusted."""

    def __init__(self, reason: str, message: str | None = None) -> None:
        super().__init__(f"{reason}: {message}" if message else reason)
        self.reason = reason


class NetworkEvidenceStatus(str, Enum):
    """Runtime contract for NetworkEvidenceStatus."""

    READY = "ready"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"
    BLOCKED = "blocked"


class NetworkSignalKind(str, Enum):
    """Runtime contract for NetworkSignalKind."""

    LATENCY_MS = "latency_ms"
    JITTER_MS = "jitter_ms"
    PACKET_LOSS = "packet_loss"
    DNS_MS = "dns_ms"
    THROUGHPUT_MBPS = "throughput_mbps"
    CONNECTION_CLASS = "connection_class"
    VPN_FIREWALL = "vpn_firewall"
    RATE_LIMIT = "rate_limit"
    PROVIDER_HEALTH = "provider_health"
    CACHE_FRESHNESS = "cache_freshness"


class RecommendationRisk(str, Enum):
    """Runtime contract for RecommendationRisk."""

    SAFE = "safe"
    NEEDS_APPROVAL = "needs_approval"
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class NetworkObservation:
    """One bounded, redacted network evidence point."""

    kind: NetworkSignalKind | str
    status: NetworkEvidenceStatus | str
    value: float | int | str | None
    unit: str
    evidence_id: str
    measured_at_utc: str
    source: str = "local"
    private: bool = False
    stale: bool = False
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        kind = _coerce_enum(NetworkSignalKind, self.kind, "signal-kind-unknown")
        status = _coerce_enum(NetworkEvidenceStatus, self.status, "evidence-status-unknown")
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "status", status)
        _require_text(self.evidence_id, "evidence_id")
        _require_text(self.measured_at_utc, "measured_at_utc")
        if self.private:
            raise NetworkTransportError("private-evidence-not-redacted", self.evidence_id)
        if status is NetworkEvidenceStatus.READY:
            if self.value is None:
                raise NetworkTransportError("ready-observation-missing-value", kind.value)
            if self.stale:
                raise NetworkTransportError("ready-observation-stale", kind.value)
            if kind in _NUMERIC_SIGNAL_KINDS and not isinstance(self.value, (int, float)):
                raise NetworkTransportError("ready-observation-numeric-required", kind.value)
        if isinstance(self.value, (int, float)) and not math.isfinite(float(self.value)):
            raise NetworkTransportError("observation-value-not-finite", kind.value)
        if not isinstance(self.details, dict):
            raise NetworkTransportError("observation-details-invalid", kind.value)

    def to_dict(self) -> dict[str, Any]:
        return _json_safe(self)

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"NetworkObservation(kind={self.kind!r}, status={self.status!r}, value={self.value!r})"


@dataclass(frozen=True, slots=True)
class ProviderHealth:
    """Observed provider status used for online-agent routing."""

    provider_id: str
    status: NetworkEvidenceStatus | str
    latency_ms: float | None = None
    error_rate: float | None = None
    rate_limited: bool = False
    evidence_id: str = "provider-health"

    def __post_init__(self) -> None:
        _require_text(self.provider_id, "provider_id")
        object.__setattr__(self, "status", _coerce_enum(NetworkEvidenceStatus, self.status, "provider-status-unknown"))
        for name in ("latency_ms", "error_rate"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, (int, float)) or not math.isfinite(float(value))):
                raise NetworkTransportError("provider-metric-invalid", name)

    def to_dict(self) -> dict[str, Any]:
        return _json_safe(self)

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return (
            f"ProviderHealth(provider_id={self.provider_id!r}, status={self.status!r}, latency_ms={self.latency_ms!r})"
        )


@dataclass(frozen=True, slots=True)
class NetworkTransportPolicy:
    """Governed policy envelope for transport recommendations."""

    bandwidth_budget_mbps: float
    max_retry_backoff_seconds: float
    cache_ttl_seconds: int
    stale_after_seconds: int
    preferred_providers: tuple[str, ...] = ()
    risky_change_requires_approval: bool = True
    allow_host_network_mutation: bool = False

    def __post_init__(self) -> None:
        for name in ("bandwidth_budget_mbps", "max_retry_backoff_seconds"):
            value = getattr(self, name)
            if not isinstance(value, (int, float)) or not math.isfinite(float(value)) or float(value) <= 0:
                raise NetworkTransportError("policy-number-invalid", name)
        if self.cache_ttl_seconds <= 0 or self.stale_after_seconds <= 0:
            raise NetworkTransportError("policy-duration-invalid")
        if self.allow_host_network_mutation:
            raise NetworkTransportError("host-network-mutation-forbidden")
        object.__setattr__(self, "preferred_providers", tuple(str(item) for item in self.preferred_providers))

    def to_dict(self) -> dict[str, Any]:
        return _json_safe(self)

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"NetworkTransportPolicy(bandwidth_budget_mbps={self.bandwidth_budget_mbps!r}, max_retry_backoff_seconds={self.max_retry_backoff_seconds!r}, cache_ttl_seconds={self.cache_ttl_seconds!r})"


@dataclass(frozen=True, slots=True)
class NetworkApprovalPacket:
    """Non-authoritative packet for a caller-owned approval system."""

    recommendation_id: str
    risk: RecommendationRisk | str
    explanation: str
    rollback_guidance: str
    before_after_evidence_required: tuple[str, ...]
    caller_must_bind_decision: bool = True

    def __post_init__(self) -> None:
        _require_text(self.recommendation_id, "recommendation_id")
        object.__setattr__(self, "risk", _coerce_enum(RecommendationRisk, self.risk, "risk-unknown"))
        _require_text(self.explanation, "explanation")
        _require_text(self.rollback_guidance, "rollback_guidance")
        if not self.before_after_evidence_required:
            raise NetworkTransportError("approval-evidence-missing", self.recommendation_id)

    def to_dict(self) -> dict[str, Any]:
        return _json_safe(self)

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"NetworkApprovalPacket(recommendation_id={self.recommendation_id!r}, risk={self.risk!r}, explanation={self.explanation!r})"


@dataclass(frozen=True, slots=True)
class NetworkRecommendation:
    """Advisory-only optimization recommendation."""

    recommendation_id: str
    title: str
    risk: RecommendationRisk | str
    action: str
    explanation: str
    evidence_ids: tuple[str, ...]
    approval_packet: NetworkApprovalPacket | None = None

    def __post_init__(self) -> None:
        _require_text(self.recommendation_id, "recommendation_id")
        _require_text(self.title, "title")
        _require_text(self.action, "action")
        _require_text(self.explanation, "explanation")
        object.__setattr__(self, "risk", _coerce_enum(RecommendationRisk, self.risk, "risk-unknown"))
        _require_text_tuple(self.evidence_ids, "evidence_ids")
        if self.risk is RecommendationRisk.NEEDS_APPROVAL and self.approval_packet is None:
            raise NetworkTransportError("approval-packet-required", self.recommendation_id)

    def to_dict(self) -> dict[str, Any]:
        return _json_safe(self)

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"NetworkRecommendation(recommendation_id={self.recommendation_id!r}, title={self.title!r}, risk={self.risk!r})"


@dataclass(frozen=True, slots=True)
class NetworkRoutingDecision:
    """Online-agent route decision derived from trusted policy and evidence."""

    decision_id: str
    provider_id: str
    mode: str
    backoff_seconds: float
    use_cache: bool
    refresh_connectors: bool
    reasons: tuple[str, ...]
    evidence_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        for name in ("decision_id", "provider_id", "mode"):
            _require_text(getattr(self, name), name)
        if self.backoff_seconds < 0:
            raise NetworkTransportError("backoff-negative")
        _require_text_tuple(self.reasons, "reasons")
        _require_text_tuple(self.evidence_ids, "evidence_ids")

    def to_dict(self) -> dict[str, Any]:
        return _json_safe(self)

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"NetworkRoutingDecision(decision_id={self.decision_id!r}, provider_id={self.provider_id!r}, mode={self.mode!r})"


_NUMERIC_SIGNAL_KINDS = frozenset({
    NetworkSignalKind.LATENCY_MS,
    NetworkSignalKind.JITTER_MS,
    NetworkSignalKind.PACKET_LOSS,
    NetworkSignalKind.DNS_MS,
    NetworkSignalKind.THROUGHPUT_MBPS,
})


def _coerce_enum(enum_type: type[Enum], value: Enum | str, reason: str) -> Enum:
    if isinstance(value, enum_type):
        return value
    raw_value = value.value if isinstance(value, Enum) else value
    try:
        return enum_type(str(raw_value))
    except ValueError as exc:
        raise NetworkTransportError(reason, str(value)) from exc


def _require_text(value: object, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise NetworkTransportError("text-required", field_name)


def _require_text_tuple(value: tuple[str, ...], field_name: str) -> None:
    if (
        not isinstance(value, tuple)
        or not value
        or any(not isinstance(item, str) or not item.strip() for item in value)
    ):
        raise NetworkTransportError("text-tuple-required", field_name)


__all__ = [
    "NetworkApprovalPacket",
    "NetworkEvidenceStatus",
    "NetworkObservation",
    "NetworkRecommendation",
    "NetworkRoutingDecision",
    "NetworkSignalKind",
    "NetworkTransportError",
    "NetworkTransportPolicy",
    "ProviderHealth",
    "RecommendationRisk",
]
