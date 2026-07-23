"""Adaptive tuning config loader."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

import yaml

from vetinari.constants import OUTPUTS_DIR, PROJECT_ROOT
from vetinari.workbench.adaptive_tuning.contracts import AdaptiveTuningError, RiskTier
from vetinari.workbench.adaptive_tuning.policy import AdaptiveTuningPolicy
from vetinari.workbench.adaptive_tuning.store import DEFAULT_ADAPTIVE_TUNING_STATE_ROOT

DEFAULT_ADAPTIVE_TUNING_CONFIG = PROJECT_ROOT / "config" / "workbench" / "adaptive_tuning.yaml"
_LEGACY_ADAPTIVE_TUNING_STATE_ROOT = OUTPUTS_DIR / "workbench" / "adaptive_tuning"


@dataclass(frozen=True, slots=True)
class AdaptiveTuningConfig:
    """Parsed adaptive tuning config."""

    policy: AdaptiveTuningPolicy
    risk_tiers: tuple[RiskTier, ...]
    excluded_sensitive_surfaces: tuple[str, ...]
    evidence_window_days: int
    state_root: Path

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"AdaptiveTuningConfig(policy={self.policy!r}, risk_tiers={self.risk_tiers!r}, excluded_sensitive_surfaces={self.excluded_sensitive_surfaces!r})"


def load_adaptive_tuning_config(path: str | Path = DEFAULT_ADAPTIVE_TUNING_CONFIG) -> AdaptiveTuningConfig:
    """Load config from YAML and fail closed for unsafe or unreadable values.

    Returns:
        Resolved adaptive tuning config value.

    Raises:
        Exception: Propagates validation or runtime failures from the underlying operation.
    """
    config_path = Path(path)
    try:
        payload = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise AdaptiveTuningError("config-unreadable", str(config_path)) from exc
    except yaml.YAMLError as exc:
        raise AdaptiveTuningError("config-corrupt", str(config_path)) from exc
    if not isinstance(payload, dict):
        raise AdaptiveTuningError("config-invalid", "root must be a mapping")

    policy_payload = _mapping(payload.get("policy"), "policy")
    risk_tiers = tuple(_risk_tier(item) for item in _sequence(payload.get("risk_tiers"), "risk_tiers"))
    if set(risk_tiers) != set(RiskTier):
        raise AdaptiveTuningError("config-risk-tiers-incomplete")
    excluded = tuple(
        str(item) for item in _sequence(payload.get("excluded_sensitive_surfaces"), "excluded_sensitive_surfaces")
    )
    if not excluded:
        raise AdaptiveTuningError("config-sensitive-surfaces-missing")
    evidence_window_days = _positive_int(payload.get("evidence_window_days"), "evidence_window_days")
    state_root = Path(str(payload.get("state_root", DEFAULT_ADAPTIVE_TUNING_STATE_ROOT)))
    if not state_root.parts or state_root.is_absolute():
        raise AdaptiveTuningError("config-state-root-invalid")
    if state_root == _LEGACY_ADAPTIVE_TUNING_STATE_ROOT:
        state_root = DEFAULT_ADAPTIVE_TUNING_STATE_ROOT

    policy = AdaptiveTuningPolicy(
        consent_required=_bool(policy_payload.get("consent_required", True), "consent_required"),
        explicit_consent_granted=_bool(
            policy_payload.get("explicit_consent_granted", False), "explicit_consent_granted"
        ),
        allow_low_risk_auto_apply=_bool(
            policy_payload.get("allow_low_risk_auto_apply", False), "allow_low_risk_auto_apply"
        ),
        min_confidence=float(policy_payload.get("min_confidence", 0.65)),
        evidence_stale_after_days=evidence_window_days,
        high_risk_measurement_stale_after_days=_positive_int(
            policy_payload.get("high_risk_measurement_stale_after_days", 14),
            "high_risk_measurement_stale_after_days",
        ),
        medium_risk_requires_preview=_bool(
            policy_payload.get("medium_risk_requires_preview", True), "medium_risk_requires_preview"
        ),
        medium_risk_requires_approval=_bool(
            policy_payload.get("medium_risk_requires_approval", True), "medium_risk_requires_approval"
        ),
        high_risk_requires_tests=_bool(
            policy_payload.get("high_risk_requires_tests", True), "high_risk_requires_tests"
        ),
        high_risk_requires_rollback=_bool(
            policy_payload.get("high_risk_requires_rollback", True), "high_risk_requires_rollback"
        ),
        high_risk_requires_promotion_evidence=_bool(
            policy_payload.get("high_risk_requires_promotion_evidence", True),
            "high_risk_requires_promotion_evidence",
        ),
        anti_sycophancy_non_overridable=_bool(
            policy_payload.get("anti_sycophancy_non_overridable", True),
            "anti_sycophancy_non_overridable",
        ),
        allow_host_network_mutation=_bool(
            policy_payload.get("allow_host_network_mutation", False), "allow_host_network_mutation"
        ),
    )
    if policy.allow_host_network_mutation:
        raise AdaptiveTuningError("config-host-network-mutation-forbidden")
    if not 0 <= policy.min_confidence <= 1:
        raise AdaptiveTuningError("config-confidence-invalid")
    return AdaptiveTuningConfig(policy, risk_tiers, excluded, evidence_window_days, state_root)


def _mapping(value: Any, field_name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise AdaptiveTuningError("config-mapping-required", field_name)
    return value


def _sequence(value: Any, field_name: str) -> list[Any]:
    if not isinstance(value, list) or not value:
        raise AdaptiveTuningError("config-list-required", field_name)
    return value


def _risk_tier(value: object) -> RiskTier:
    raw_value = value.value if isinstance(value, Enum) else value
    try:
        return RiskTier(raw_value)
    except ValueError as exc:
        raise AdaptiveTuningError("config-risk-tier-unknown", str(value)) from exc


def _bool(value: object, field_name: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in {"true", "1", "yes", "on"}:
            return True
        if normalized in {"false", "0", "no", "off"}:
            return False
    raise AdaptiveTuningError("config-bool-required", field_name)


def _positive_int(value: object, field_name: str) -> int:
    if not isinstance(value, int) or value <= 0:
        raise AdaptiveTuningError("config-positive-int-required", field_name)
    return value
