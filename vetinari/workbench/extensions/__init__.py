"""Workbench extension marketplace contracts."""

from __future__ import annotations

from vetinari.workbench.extensions.contracts import (
    ExtensionManifest,
    ExtensionRiskReason,
    ExtensionRiskStatus,
    ExtensionRiskVerdict,
    ExtensionSourceKind,
    MarketplaceMetadata,
    SecretRequest,
)
from vetinari.workbench.extensions.sandbox_handles import (
    SandboxAdmission,
    SandboxHandle,
    admit_sandbox_operation,
)

__all__ = [
    "ExtensionManifest",
    "ExtensionRiskReason",
    "ExtensionRiskStatus",
    "ExtensionRiskVerdict",
    "ExtensionSourceKind",
    "MarketplaceMetadata",
    "SandboxAdmission",
    "SandboxHandle",
    "SecretRequest",
    "admit_sandbox_operation",
]
