"""Earned operator-readiness gates for AM Workbench."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Self


class ReadinessMode(StrEnum):
    """Operator-readiness modes exposed to Workbench consumers."""

    FULL = "full"
    MINIMAL = "minimal"
    RESTRICTED = "restricted"
    BLOCKED = "blocked"


class SignalKind(StrEnum):
    """Dependency signal families that contribute to Workbench readiness."""

    SETUP = "setup"
    IDENTITY = "identity"
    CONFIG = "config"
    PROVIDER = "provider"
    CAPABILITY_PACK = "capability_pack"
    POLICY = "policy"
    SCHEDULER = "scheduler"
    MEMORY = "memory"
    CONNECTOR = "connector"
    TOOL_PIN = "tool_pin"
    RUN_KERNEL = "run_kernel"
    VERIFIED_HISTORY = "verified_history"
    PROBE = "probe"


class SignalStatus(StrEnum):
    """Readable signal statuses. Unknown and unreadable values fail closed."""

    PASSING = "passing"
    VERIFIED = "verified"
    DEGRADED = "degraded"
    CONFIRMATION_REQUIRED = "confirmation_required"
    STALE = "stale"
    UNKNOWN = "unknown"
    MISSING = "missing"
    UNREADABLE = "unreadable"
    CORRUPT = "corrupt"
    INCOMPATIBLE = "incompatible"
    FAILING = "failing"


class FeatureGate(StrEnum):
    """Workbench features guarded by readiness."""

    MISSION_CONTROL = "mission_control"
    LAUNCHER_FIRST_RUN_SETUP = "launcher_first_run_setup"
    AUTOMATION_ADMISSION = "automation_admission"
    PROVIDER_USE = "provider_use"
    CAPABILITY_PACK_USE = "capability_pack_use"
    CONNECTOR_USE = "connector_use"
    RUN_KERNEL_OPERATIONS = "run_kernel_operations"


class FeatureGateDecision(StrEnum):
    """Per-feature admission shape."""

    OPEN = "open"
    CONFIRMATION_REQUIRED = "confirmation_required"
    RESTRICTED = "restricted"
    BLOCKED = "blocked"


class AdmissionDecision(StrEnum):
    """Downstream automation admission result."""

    ALLOW = "allow"
    REQUIRE_CONFIRMATION = "require_confirmation"
    RESTRICT = "restrict"
    BLOCK = "block"


PASSING_STATUSES = frozenset({SignalStatus.PASSING, SignalStatus.VERIFIED})
BLOCKING_STATUSES = frozenset({
    SignalStatus.MISSING,
    SignalStatus.UNREADABLE,
    SignalStatus.CORRUPT,
    SignalStatus.INCOMPATIBLE,
    SignalStatus.FAILING,
})
DEGRADED_STATUSES = frozenset({
    SignalStatus.DEGRADED,
    SignalStatus.CONFIRMATION_REQUIRED,
    SignalStatus.STALE,
    SignalStatus.UNKNOWN,
})


@dataclass(frozen=True, slots=True)
class ReadinessEvidenceRef:
    """Reference to proof that supports a readiness signal."""

    ref: str
    kind: str = "artifact"
    detail: str = ""

    @classmethod
    def from_value(cls, value: Any) -> Self:
        """Execute the from value operation.

        Returns:
            Self value produced by from_value().

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        if isinstance(value, cls):
            return value
        if isinstance(value, str):
            return cls(ref=value)
        if isinstance(value, Mapping):
            return cls(
                ref=str(value.get("ref", "")),
                kind=str(value.get("kind", "artifact")),
                detail=str(value.get("detail", "")),
            )
        raise TypeError(f"unsupported evidence ref: {type(value).__name__}")

    def to_dict(self) -> dict[str, str]:
        """Execute the to dict operation.

        Returns:
            dict[str, str] value produced by to_dict().
        """
        payload = {"ref": self.ref, "kind": self.kind}
        if self.detail:
            payload["detail"] = self.detail
        return payload


@dataclass(frozen=True, slots=True)
class ReadinessSignal:
    """A caller-provided dependency health signal."""

    kind: SignalKind
    status: SignalStatus
    summary: str
    critical: bool = False
    evidence_refs: tuple[ReadinessEvidenceRef, ...] = ()

    @classmethod
    def from_value(cls, kind: SignalKind | str, value: Any, *, critical: bool = False) -> Self:
        """Execute the from value operation.

        Args:
            kind: Kind discriminator used to select the operation branch.
            value: Value processed by the operation.
            critical: Critical value consumed by from_value().

        Returns:
            Self value produced by from_value().

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        signal_kind = SignalKind(kind)
        if isinstance(value, cls):
            return value
        if isinstance(value, str):
            return cls(
                kind=signal_kind,
                status=SignalStatus(value),
                summary=f"{signal_kind.value} is {value}",
                critical=critical,
            )
        if not isinstance(value, Mapping):
            raise TypeError(f"{signal_kind.value} readiness signal must be a mapping or status string")
        status = SignalStatus(str(value.get("status", SignalStatus.UNKNOWN.value)))
        evidence = tuple(ReadinessEvidenceRef.from_value(row) for row in value.get("evidence_refs", ()))
        return cls(
            kind=signal_kind,
            status=status,
            summary=str(value.get("summary", f"{signal_kind.value} is {status.value}")),
            critical=bool(value.get("critical", critical)),
            evidence_refs=evidence,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "status": self.status.value,
            "summary": self.summary,
            "critical": self.critical,
            "evidence_refs": [ref.to_dict() for ref in self.evidence_refs],
        }

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"ReadinessSignal(kind={self.kind!r}, status={self.status!r}, summary={self.summary!r})"


@dataclass(frozen=True, slots=True)
class WorkbenchReadinessPolicy:
    """Static evaluator policy."""

    critical_signals: tuple[SignalKind, ...] = (
        SignalKind.IDENTITY,
        SignalKind.CONFIG,
        SignalKind.POLICY,
        SignalKind.TOOL_PIN,
        SignalKind.RUN_KERNEL,
    )
    expected_signals: tuple[SignalKind, ...] = (
        SignalKind.SETUP,
        SignalKind.IDENTITY,
        SignalKind.CONFIG,
        SignalKind.PROVIDER,
        SignalKind.CAPABILITY_PACK,
        SignalKind.POLICY,
        SignalKind.SCHEDULER,
        SignalKind.MEMORY,
        SignalKind.CONNECTOR,
        SignalKind.TOOL_PIN,
        SignalKind.RUN_KERNEL,
        SignalKind.VERIFIED_HISTORY,
        SignalKind.PROBE,
    )
    full_requires: tuple[SignalKind, ...] = (
        SignalKind.PROVIDER,
        SignalKind.CAPABILITY_PACK,
        SignalKind.CONNECTOR,
        SignalKind.RUN_KERNEL,
        SignalKind.VERIFIED_HISTORY,
        SignalKind.PROBE,
    )
    feature_dependencies: Mapping[FeatureGate, tuple[SignalKind, ...]] = field(
        default_factory=lambda: {
            FeatureGate.MISSION_CONTROL: (SignalKind.POLICY, SignalKind.RUN_KERNEL, SignalKind.PROVIDER),
            FeatureGate.LAUNCHER_FIRST_RUN_SETUP: (SignalKind.IDENTITY, SignalKind.CONFIG),
            FeatureGate.AUTOMATION_ADMISSION: (
                SignalKind.POLICY,
                SignalKind.TOOL_PIN,
                SignalKind.RUN_KERNEL,
                SignalKind.PROVIDER,
            ),
            FeatureGate.PROVIDER_USE: (SignalKind.PROVIDER,),
            FeatureGate.CAPABILITY_PACK_USE: (SignalKind.CAPABILITY_PACK, SignalKind.TOOL_PIN),
            FeatureGate.CONNECTOR_USE: (SignalKind.CONNECTOR, SignalKind.POLICY),
            FeatureGate.RUN_KERNEL_OPERATIONS: (SignalKind.RUN_KERNEL,),
        }
    )

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"WorkbenchReadinessPolicy(critical_signals={self.critical_signals!r}, expected_signals={self.expected_signals!r}, full_requires={self.full_requires!r})"


@dataclass(frozen=True, slots=True)
class WorkbenchReadinessSnapshot:
    """Operator-readable readiness result."""

    mode: ReadinessMode
    reasons: tuple[str, ...]
    recommended_actions: tuple[str, ...]
    feature_gates: Mapping[FeatureGate, FeatureGateDecision]
    signals: tuple[ReadinessSignal, ...]
    evidence_refs: tuple[ReadinessEvidenceRef, ...] = ()
    readiness_kind: str = "operator_readiness"

    def to_dict(self) -> dict[str, Any]:
        return {
            "readiness_kind": self.readiness_kind,
            "mode": self.mode.value,
            "reasons": list(self.reasons),
            "recommended_actions": list(self.recommended_actions),
            "feature_gates": {gate.value: decision.value for gate, decision in self.feature_gates.items()},
            "signals": [signal.to_dict() for signal in self.signals],
            "evidence_refs": [ref.to_dict() for ref in self.evidence_refs],
        }

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"WorkbenchReadinessSnapshot(mode={self.mode!r}, reasons={self.reasons!r}, recommended_actions={self.recommended_actions!r})"


@dataclass(frozen=True, slots=True)
class WorkbenchAdmissionResult:
    """Admission helper output for downstream automation and setup consumers."""

    decision: AdmissionDecision
    feature: FeatureGate
    gate: FeatureGateDecision
    mode: ReadinessMode
    reasons: tuple[str, ...]
    required_actions: tuple[str, ...]

    @property
    def allowed(self) -> bool:
        return self.decision is AdmissionDecision.ALLOW

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision.value,
            "allowed": self.allowed,
            "feature": self.feature.value,
            "gate": self.gate.value,
            "mode": self.mode.value,
            "reasons": list(self.reasons),
            "required_actions": list(self.required_actions),
        }

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"WorkbenchAdmissionResult(decision={self.decision!r}, feature={self.feature!r}, gate={self.gate!r})"


DEFAULT_READINESS_POLICY = WorkbenchReadinessPolicy()


def evaluate_workbench_readiness(
    dependency_snapshots: Mapping[str | SignalKind, Any] | Sequence[ReadinessSignal] | None,
    policy: WorkbenchReadinessPolicy | None = None,
) -> WorkbenchReadinessSnapshot:
    """Evaluate Workbench operator readiness from caller-provided signals.

    Args:
        dependency_snapshots: Dependency snapshots value consumed by evaluate_workbench_readiness().
        policy: Policy value consumed by evaluate_workbench_readiness().

    Returns:
        WorkbenchReadinessSnapshot value produced by evaluate_workbench_readiness().
    """
    active_policy = policy or DEFAULT_READINESS_POLICY
    signals = _normalise_signals(dependency_snapshots or {}, active_policy)
    by_kind = {signal.kind: signal for signal in signals}
    reasons: list[str] = []
    actions: list[str] = []

    for signal in signals:
        if signal.critical and signal.status in BLOCKING_STATUSES:
            reasons.append(f"{signal.kind.value}:{signal.status.value}:{signal.summary}")
            actions.append(f"restore-readable-{signal.kind.value}-state")

    if reasons:
        mode = ReadinessMode.BLOCKED
    else:
        critical_degraded = [signal for signal in signals if signal.critical and signal.status not in PASSING_STATUSES]
        operational_blocked = [
            signal
            for signal in signals
            if not signal.critical and signal.status in BLOCKING_STATUSES and signal.kind in _operational_signal_kinds()
        ]
        if critical_degraded or operational_blocked:
            mode = ReadinessMode.RESTRICTED
            for signal in [*critical_degraded, *operational_blocked]:
                reasons.append(f"{signal.kind.value}:{signal.status.value}:{signal.summary}")
                actions.append(f"verify-or-disable-{signal.kind.value}")
        elif all(by_kind[kind].status in PASSING_STATUSES for kind in active_policy.full_requires):
            mode = ReadinessMode.FULL
            reasons.append("all-required-readiness-signals-verified")
            actions.append("continue-normal-workbench-operations")
        else:
            mode = ReadinessMode.MINIMAL
            for signal in signals:
                if signal.status not in PASSING_STATUSES:
                    reasons.append(f"{signal.kind.value}:{signal.status.value}:{signal.summary}")
                    actions.append(f"complete-{signal.kind.value}-setup-or-keep-feature-gated")

    if mode is not ReadinessMode.FULL and not reasons:
        reasons.append("readiness-is-not-full")
    if mode is not ReadinessMode.FULL and not actions:
        actions.append("inspect-readiness-signals-before-continuing")

    return WorkbenchReadinessSnapshot(
        mode=mode,
        reasons=tuple(dict.fromkeys(reasons)),
        recommended_actions=tuple(dict.fromkeys(actions)),
        feature_gates=_feature_gates(mode, by_kind, active_policy),
        signals=signals,
        evidence_refs=tuple(ref for signal in signals for ref in signal.evidence_refs),
    )


def evaluate_workbench_admission(
    snapshot_or_dependencies: WorkbenchReadinessSnapshot
    | Mapping[str | SignalKind, Any]
    | Sequence[ReadinessSignal]
    | None,
    *,
    feature: FeatureGate | str = FeatureGate.AUTOMATION_ADMISSION,
    policy: WorkbenchReadinessPolicy | None = None,
) -> WorkbenchAdmissionResult:
    """Return a downstream admission decision for a feature gate.

    Returns:
        WorkbenchAdmissionResult value produced by evaluate_workbench_admission().
    """
    snapshot = (
        snapshot_or_dependencies
        if isinstance(snapshot_or_dependencies, WorkbenchReadinessSnapshot)
        else evaluate_workbench_readiness(snapshot_or_dependencies, policy=policy)
    )
    gate_feature = FeatureGate(feature)
    gate = snapshot.feature_gates.get(gate_feature, FeatureGateDecision.BLOCKED)
    if snapshot.mode is ReadinessMode.BLOCKED or gate is FeatureGateDecision.BLOCKED:
        decision = AdmissionDecision.BLOCK
    elif gate is FeatureGateDecision.RESTRICTED:
        decision = AdmissionDecision.RESTRICT
    elif gate is FeatureGateDecision.CONFIRMATION_REQUIRED:
        decision = AdmissionDecision.REQUIRE_CONFIRMATION
    else:
        decision = AdmissionDecision.ALLOW
    return WorkbenchAdmissionResult(
        decision=decision,
        feature=gate_feature,
        gate=gate,
        mode=snapshot.mode,
        reasons=snapshot.reasons,
        required_actions=snapshot.recommended_actions,
    )


def _normalise_signals(
    dependency_snapshots: Mapping[str | SignalKind, Any] | Sequence[ReadinessSignal],
    policy: WorkbenchReadinessPolicy,
) -> tuple[ReadinessSignal, ...]:
    if isinstance(dependency_snapshots, Sequence) and not isinstance(dependency_snapshots, (str, bytes, bytearray)):
        provided = {signal.kind: signal for signal in dependency_snapshots}
    else:
        provided = {}
        for raw_kind, raw_value in dict(dependency_snapshots).items():
            kind = SignalKind(raw_kind)
            provided[kind] = ReadinessSignal.from_value(kind, raw_value, critical=kind in policy.critical_signals)

    signals: list[ReadinessSignal] = []
    for kind in policy.expected_signals:
        if kind in provided:
            signal = provided[kind]
            if signal.critical == (kind in policy.critical_signals):
                signals.append(signal)
            else:
                signals.append(
                    ReadinessSignal(
                        kind=signal.kind,
                        status=signal.status,
                        summary=signal.summary,
                        critical=kind in policy.critical_signals,
                        evidence_refs=signal.evidence_refs,
                    )
                )
            continue
        status = SignalStatus.MISSING if kind in policy.critical_signals else SignalStatus.UNKNOWN
        signals.append(
            ReadinessSignal(
                kind=kind,
                status=status,
                summary=f"{kind.value} state was not supplied",
                critical=kind in policy.critical_signals,
            )
        )
    return tuple(signals)


def _feature_gates(
    mode: ReadinessMode,
    signals: Mapping[SignalKind, ReadinessSignal],
    policy: WorkbenchReadinessPolicy,
) -> dict[FeatureGate, FeatureGateDecision]:
    gates: dict[FeatureGate, FeatureGateDecision] = {}
    for gate, dependencies in policy.feature_dependencies.items():
        dependency_signals = [signals[kind] for kind in dependencies]
        dependency_statuses = [signal.status for signal in dependency_signals]
        if mode is ReadinessMode.BLOCKED or any(
            signal.critical and signal.status in BLOCKING_STATUSES for signal in dependency_signals
        ):
            gates[gate] = FeatureGateDecision.BLOCKED
        elif any(signal.status in BLOCKING_STATUSES for signal in dependency_signals):
            gates[gate] = FeatureGateDecision.RESTRICTED
        elif mode is ReadinessMode.RESTRICTED:
            gates[gate] = (
                FeatureGateDecision.CONFIRMATION_REQUIRED
                if gate is FeatureGate.LAUNCHER_FIRST_RUN_SETUP
                else FeatureGateDecision.RESTRICTED
            )
        elif mode is ReadinessMode.MINIMAL:
            gates[gate] = _minimal_gate(gate, dependency_statuses)
        else:
            gates[gate] = FeatureGateDecision.OPEN
    return gates


def _minimal_gate(gate: FeatureGate, dependency_statuses: Sequence[SignalStatus]) -> FeatureGateDecision:
    if gate is FeatureGate.LAUNCHER_FIRST_RUN_SETUP and all(
        status in PASSING_STATUSES for status in dependency_statuses
    ):
        return FeatureGateDecision.OPEN
    if gate is FeatureGate.RUN_KERNEL_OPERATIONS and all(status in PASSING_STATUSES for status in dependency_statuses):
        return FeatureGateDecision.OPEN
    if all(status in PASSING_STATUSES for status in dependency_statuses):
        return FeatureGateDecision.CONFIRMATION_REQUIRED
    if any(status in DEGRADED_STATUSES for status in dependency_statuses):
        return FeatureGateDecision.RESTRICTED
    return FeatureGateDecision.BLOCKED


def _operational_signal_kinds() -> frozenset[SignalKind]:
    return frozenset({
        SignalKind.PROVIDER,
        SignalKind.CAPABILITY_PACK,
        SignalKind.SCHEDULER,
        SignalKind.MEMORY,
        SignalKind.CONNECTOR,
        SignalKind.PROBE,
    })


__all__ = [
    "AdmissionDecision",
    "FeatureGate",
    "FeatureGateDecision",
    "ReadinessEvidenceRef",
    "ReadinessMode",
    "ReadinessSignal",
    "SignalKind",
    "SignalStatus",
    "WorkbenchAdmissionResult",
    "WorkbenchReadinessPolicy",
    "WorkbenchReadinessSnapshot",
    "evaluate_workbench_admission",
    "evaluate_workbench_readiness",
]
