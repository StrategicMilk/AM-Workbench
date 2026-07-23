"""Desktop launcher public contracts."""

from __future__ import annotations

from vetinari.desktop.contracts import (
    CrashRecoveryReport,
    HealthGateResult,
    LauncherDecision,
    LauncherDecisionAction,
    LauncherError,
    LauncherRuntimeMode,
    LauncherStatus,
    LifecycleAction,
    LifecycleCommandOrigin,
    LifecycleCommandRequest,
    LifecycleCommandResult,
    SetupStep,
    ShutdownProtocol,
    SupportBundleSpec,
)
from vetinari.desktop.tray import TrayActionId, TrayController, TrayMenuItem

__all__ = [
    "CrashRecoveryReport",
    "HealthGateResult",
    "LauncherDecision",
    "LauncherDecisionAction",
    "LauncherError",
    "LauncherRuntimeMode",
    "LauncherStatus",
    "LifecycleAction",
    "LifecycleCommandOrigin",
    "LifecycleCommandRequest",
    "LifecycleCommandResult",
    "SetupStep",
    "ShutdownProtocol",
    "SupportBundleSpec",
    "TrayActionId",
    "TrayController",
    "TrayMenuItem",
]
