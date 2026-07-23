"""Hardware digital twin profile policy loader."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path

import yaml

from vetinari.constants import PROJECT_ROOT
from vetinari.workbench.hardware.contracts import (
    ObservationKind,
    ProposalRisk,
)

DEFAULT_HARDWARE_PROFILES_PATH = PROJECT_ROOT / "config" / "workbench" / "hardware_profiles.yaml"


class HardwareProfileError(ValueError):
    """Raised when hardware profile policy cannot be trusted."""

    def __init__(self, reason: str, message: str | None = None) -> None:
        super().__init__(f"{reason}: {message}" if message else reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class BenchmarkCategoryPolicy:
    """Validation policy for one observation category."""

    kind: ObservationKind
    label: str
    required: bool
    ready_threshold: float
    degraded_threshold: float
    evidence_required: bool = True
    optional_when_unavailable: bool = False

    def __post_init__(self) -> None:
        _require_text(self.label, "label")
        _require_finite_non_negative(self.ready_threshold, "ready_threshold")
        _require_finite_non_negative(self.degraded_threshold, "degraded_threshold")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"BenchmarkCategoryPolicy(kind={self.kind!r}, label={self.label!r}, required={self.required!r})"


@dataclass(frozen=True, slots=True)
class ProposalRiskPolicy:
    """Governance policy for one proposal risk class."""

    risk: ProposalRisk
    review_required: bool
    rollback_required: bool
    before_after_evidence_required: bool
    locally_executable: bool

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"ProposalRiskPolicy(risk={self.risk!r}, review_required={self.review_required!r}, rollback_required={self.rollback_required!r})"


@dataclass(frozen=True, slots=True)
class HardwareProfilePolicy:
    """Validated hardware digital twin policy set."""

    profile_id: str
    benchmark_categories: tuple[BenchmarkCategoryPolicy, ...]
    risk_policies: tuple[ProposalRiskPolicy, ...]
    stale_after_hours: int

    def __post_init__(self) -> None:
        _require_text(self.profile_id, "profile_id")
        if self.stale_after_hours <= 0:
            raise HardwareProfileError("stale-after-invalid", "stale_after_hours must be positive")
        kinds = [category.kind for category in self.benchmark_categories]
        if set(kinds) != set(ObservationKind):
            missing = sorted(kind.value for kind in set(ObservationKind) - set(kinds))
            raise HardwareProfileError("benchmark-category-coverage-invalid", f"missing={missing}")
        if len(kinds) != len(set(kinds)):
            raise HardwareProfileError("benchmark-category-duplicate")
        risks = [policy.risk for policy in self.risk_policies]
        if set(risks) != set(ProposalRisk):
            missing = sorted(risk.value for risk in set(ProposalRisk) - set(risks))
            raise HardwareProfileError("risk-policy-coverage-invalid", f"missing={missing}")
        if len(risks) != len(set(risks)):
            raise HardwareProfileError("risk-policy-duplicate")

    def category(self, kind: ObservationKind | str) -> BenchmarkCategoryPolicy:
        """Return category policy for a benchmark kind.

        Returns:
            BenchmarkCategoryPolicy value produced by category().

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        selected = _coerce_kind(kind)
        for category in self.benchmark_categories:
            if category.kind is selected:
                return category
        raise HardwareProfileError("benchmark-category-missing", selected.value)

    def risk_policy(self, risk: ProposalRisk | str) -> ProposalRiskPolicy:
        """Return policy for a proposal risk class.

        Returns:
            ProposalRiskPolicy value produced by risk_policy().

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        selected = _coerce_risk(risk)
        for policy in self.risk_policies:
            if policy.risk is selected:
                return policy
        raise HardwareProfileError("risk-policy-missing", selected.value)

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"HardwareProfilePolicy(profile_id={self.profile_id!r}, benchmark_categories={self.benchmark_categories!r}, risk_policies={self.risk_policies!r})"


def load_hardware_profiles(path: Path | str = DEFAULT_HARDWARE_PROFILES_PATH) -> HardwareProfilePolicy:
    """Load and validate hardware profile policy from YAML.

    Returns:
        Resolved hardware profiles value.

    Raises:
        Exception: Propagates validation or runtime failures from the underlying operation.
    """
    profile_path = Path(path)
    if not profile_path.exists():
        raise HardwareProfileError("hardware-profile-config-not-found", str(profile_path))
    try:
        raw = yaml.safe_load(profile_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise HardwareProfileError("hardware-profile-config-unreadable", str(profile_path)) from exc
    except yaml.YAMLError as exc:
        raise HardwareProfileError("hardware-profile-config-malformed", str(profile_path)) from exc
    if not isinstance(raw, dict) or not isinstance(raw.get("hardware_digital_twin"), dict):
        raise HardwareProfileError("hardware-profile-config-invalid", "missing hardware_digital_twin mapping")
    payload = raw["hardware_digital_twin"]
    categories = payload.get("benchmark_categories")
    risk_policies = payload.get("proposal_risk_policies")
    if not isinstance(categories, list):
        raise HardwareProfileError("benchmark-categories-invalid", "benchmark_categories must be a list")
    if not isinstance(risk_policies, list):
        raise HardwareProfileError("risk-policies-invalid", "proposal_risk_policies must be a list")
    return HardwareProfilePolicy(
        profile_id=str(payload.get("profile_id", "")),
        benchmark_categories=tuple(_category_from_mapping(item) for item in categories),
        risk_policies=tuple(_risk_policy_from_mapping(item) for item in risk_policies),
        stale_after_hours=int(payload.get("stale_after_hours", 0)),
    )


def _category_from_mapping(raw: object) -> BenchmarkCategoryPolicy:
    if not isinstance(raw, dict):
        raise HardwareProfileError("benchmark-category-invalid", "category entries must be mappings")
    return BenchmarkCategoryPolicy(
        kind=_coerce_kind(raw.get("kind")),
        label=str(raw.get("label", "")),
        required=bool(raw.get("required", True)),
        ready_threshold=float(raw.get("ready_threshold", -1)),
        degraded_threshold=float(raw.get("degraded_threshold", -1)),
        evidence_required=bool(raw.get("evidence_required", True)),
        optional_when_unavailable=bool(raw.get("optional_when_unavailable", False)),
    )


def _risk_policy_from_mapping(raw: object) -> ProposalRiskPolicy:
    if not isinstance(raw, dict):
        raise HardwareProfileError("risk-policy-invalid", "risk policy entries must be mappings")
    return ProposalRiskPolicy(
        risk=_coerce_risk(raw.get("risk")),
        review_required=bool(raw.get("review_required", False)),
        rollback_required=bool(raw.get("rollback_required", False)),
        before_after_evidence_required=bool(raw.get("before_after_evidence_required", False)),
        locally_executable=bool(raw.get("locally_executable", False)),
    )


def _coerce_kind(value: object) -> ObservationKind:
    if isinstance(value, ObservationKind):
        return value
    raw_value = value.value if isinstance(value, Enum) else value
    try:
        return ObservationKind(raw_value)
    except ValueError as exc:
        raise HardwareProfileError("benchmark-category-unknown", str(value)) from exc


def _coerce_risk(value: object) -> ProposalRisk:
    if isinstance(value, ProposalRisk):
        return value
    raw_value = value.value if isinstance(value, Enum) else value
    try:
        return ProposalRisk(raw_value)
    except ValueError as exc:
        raise HardwareProfileError("risk-policy-unknown", str(value)) from exc


def _require_text(value: object, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise HardwareProfileError(f"{field_name}-missing", f"{field_name} must be non-empty")


def _require_finite_non_negative(value: float, field_name: str) -> None:
    if not isinstance(value, int | float) or value < 0:
        raise HardwareProfileError(f"{field_name}-invalid", f"{field_name} must be non-negative")


__all__ = [
    "DEFAULT_HARDWARE_PROFILES_PATH",
    "BenchmarkCategoryPolicy",
    "HardwareProfileError",
    "HardwareProfilePolicy",
    "ProposalRiskPolicy",
    "load_hardware_profiles",
]
