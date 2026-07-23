"""Capability install-on-demand governance surface."""

from __future__ import annotations

from vetinari.capabilities.detector import detect_missing_capability, probe_capability, register_callable_probe
from vetinari.capabilities.registry import (
    CapabilityRegistry,
    get_capability_registry,
    reset_capability_registry_for_test,
)
from vetinari.capabilities.types import (
    CapabilityApprovalRequired,
    CapabilityHealthState,
    CapabilityInstallApproval,
    CapabilityInstallError,
    CapabilityInstallRequest,
    CapabilityInstallState,
    CapabilityKind,
    CapabilityMetadata,
    CapabilityNotFound,
    CapabilityNotInstalled,
    CapabilityProbeResult,
    CapabilityRegistryError,
    CapabilityRiskLevel,
    CapabilityState,
    DetectionRule,
    DetectionRuleKind,
)

__all__ = [
    "CapabilityApprovalRequired",
    "CapabilityHealthState",
    "CapabilityInstallApproval",
    "CapabilityInstallError",
    "CapabilityInstallRequest",
    "CapabilityInstallState",
    "CapabilityKind",
    "CapabilityMetadata",
    "CapabilityNotFound",
    "CapabilityNotInstalled",
    "CapabilityProbeResult",
    "CapabilityRegistry",
    "CapabilityRegistryError",
    "CapabilityRiskLevel",
    "CapabilityState",
    "DetectionRule",
    "DetectionRuleKind",
    "detect_missing_capability",
    "get_capability_registry",
    "probe_capability",
    "register_callable_probe",
    "reset_capability_registry_for_test",
]
