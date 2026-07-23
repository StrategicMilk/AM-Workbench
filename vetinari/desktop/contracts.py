"""Typed contracts for the AM Workbench desktop launcher."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from vetinari.constants import OUTPUTS_DIR


class LauncherRuntimeMode(StrEnum):
    """Runtime contract for LauncherRuntimeMode."""

    DESKTOP_DEFAULT = "desktop_default"
    BROWSER_OPEN = "browser_open"
    BACKGROUND_ONLY = "background_only"


class LifecycleAction(StrEnum):
    """Runtime contract for LifecycleAction."""

    OPEN = "open"
    CLOSE_WINDOW = "close_window"
    KEEP_IN_BACKGROUND = "keep_in_background"
    STOP = "stop"
    RESTART = "restart"
    QUIT_COMPLETELY = "quit_completely"
    FORCE_QUIT = "force_quit"
    CRASH_RECOVER = "crash_recover"
    VIEW_LOGS = "view_logs"
    RUN_DOCTOR = "run_doctor"
    CHECK_UPDATE = "check_update"
    SUPPORT_BUNDLE = "support_bundle"


class LifecycleCommandOrigin(StrEnum):
    """Trusted caller classes for desktop lifecycle commands."""

    TAURI = "tauri"
    COMPATIBILITY_API = "compatibility_api"
    BROWSER_FALLBACK = "browser_fallback"


class LauncherDecisionAction(StrEnum):
    """Runtime contract for LauncherDecisionAction."""

    START_BACKEND = "start_backend"
    WAIT_FOR_HEALTH = "wait_for_health"
    OPEN_UI = "open_ui"
    KEEP_BACKGROUND = "keep_background"
    STOP_GRACEFUL = "stop_graceful"
    RESTART = "restart"
    QUIT_COMPLETE = "quit_complete"
    FORCE_QUIT = "force_quit"
    RECOVER_CRASH = "recover_crash"
    BLOCK_AWAITING_CONSENT = "block_awaiting_consent"


@dataclass(frozen=True, slots=True)
class SetupStep:
    """Runtime contract for SetupStep."""

    id: str
    kind: str
    automated: bool
    requires_consent: bool
    dependency_pack: str
    editable_after: bool
    remediation: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "automated": self.automated,
            "requires_consent": self.requires_consent,
            "dependency_pack": self.dependency_pack,
            "editable_after": self.editable_after,
            "remediation": self.remediation,
        }

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"SetupStep(id={self.id!r}, kind={self.kind!r}, automated={self.automated!r})"


@dataclass(frozen=True, slots=True)
class HealthGateResult:
    """Runtime contract for HealthGateResult."""

    name: str
    passed: bool = False
    last_checked_at: datetime | None = None
    blockers: tuple[str, ...] = ()
    remediation: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "passed": self.passed,
            "last_checked_at": self.last_checked_at.isoformat() if self.last_checked_at else None,
            "blockers": list(self.blockers),
            "remediation": self.remediation,
        }

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"HealthGateResult(name={self.name!r}, passed={self.passed!r}, last_checked_at={self.last_checked_at!r})"


@dataclass(frozen=True, slots=True)
class ShutdownProtocol:
    """Runtime contract for ShutdownProtocol."""

    grace_window_seconds: float = 10
    checkpoint_active_runs: bool = True
    release_resources: bool = True
    force_after_seconds: float = 30
    record_receipt: bool = True
    default_force_after_seconds: int = 30
    default_grace_seconds: int = 10

    def to_dict(self) -> dict[str, Any]:
        return {
            "grace_window_seconds": self.grace_window_seconds,
            "checkpoint_active_runs": self.checkpoint_active_runs,
            "release_resources": self.release_resources,
            "force_after_seconds": self.force_after_seconds,
            "record_receipt": self.record_receipt,
        }

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"ShutdownProtocol(grace_window_seconds={self.grace_window_seconds!r}, checkpoint_active_runs={self.checkpoint_active_runs!r}, release_resources={self.release_resources!r})"


@dataclass(frozen=True, slots=True)
class CrashRecoveryReport:
    """Runtime contract for CrashRecoveryReport."""

    detected_at: datetime
    last_run_id: str | None
    recovered_state: str
    escalation_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "detected_at": self.detected_at.isoformat(),
            "last_run_id": self.last_run_id,
            "recovered_state": self.recovered_state,
            "escalation_reason": self.escalation_reason,
        }

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"CrashRecoveryReport(detected_at={self.detected_at!r}, last_run_id={self.last_run_id!r}, recovered_state={self.recovered_state!r})"


@dataclass(frozen=True, slots=True)
class SupportBundleSpec:
    """Runtime contract for SupportBundleSpec."""

    destination_path: Path
    included_globs: tuple[str, ...] = ("logs/*.log", f"{OUTPUTS_DIR.name}/workbench/launcher/*.json")
    redacted_globs: tuple[str, ...] = ("**/secret*", "**/credentials*", "**/*.key", "**/.env*")
    max_bytes: int = 10_000_000
    max_matched_files: int = 500
    max_stat_calls: int = 1_000
    allow_recursive_globs: bool = False

    def __post_init__(self) -> None:
        if self.max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        if self.max_matched_files <= 0:
            raise ValueError("max_matched_files must be positive")
        if self.max_stat_calls <= 0:
            raise ValueError("max_stat_calls must be positive")

    def to_dict(self) -> dict[str, Any]:
        return {
            "destination_path": str(self.destination_path),
            "included_globs": list(self.included_globs),
            "redacted_globs": list(self.redacted_globs),
            "max_bytes": self.max_bytes,
            "max_matched_files": self.max_matched_files,
            "max_stat_calls": self.max_stat_calls,
            "allow_recursive_globs": self.allow_recursive_globs,
        }

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"SupportBundleSpec(destination_path={self.destination_path!r}, included_globs={self.included_globs!r}, redacted_globs={self.redacted_globs!r})"


@dataclass(frozen=True, slots=True)
class LauncherStatus:
    """Runtime contract for LauncherStatus."""

    mode: LauncherRuntimeMode | str
    backend_pid: int | None
    ui_url: str | None
    gates: tuple[HealthGateResult, ...]
    warnings: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()

    @property
    def is_ready(self) -> bool:
        return bool(self.gates) and all(gate.passed for gate in self.gates) and not self.errors

    def to_dict(self) -> dict[str, Any]:
        """Execute the to dict operation.

        Returns:
            dict[str, Any] value produced by to_dict().
        """
        mode = self.mode.value if isinstance(self.mode, LauncherRuntimeMode) else str(self.mode)
        return {
            "mode": mode,
            "backend_pid": self.backend_pid,
            "ui_url": self.ui_url,
            "gates": [gate.to_dict() for gate in self.gates],
            "warnings": list(self.warnings),
            "errors": list(self.errors),
            "is_ready": self.is_ready,
        }

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"LauncherStatus(mode={self.mode!r}, backend_pid={self.backend_pid!r}, ui_url={self.ui_url!r})"


@dataclass(frozen=True, slots=True)
class LauncherDecision:
    """Runtime contract for LauncherDecision."""

    action: LauncherDecisionAction
    reasons: tuple[str, ...]
    retryable: bool = False
    requires_user_consent: bool = False
    escalation: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action.value,
            "reasons": list(self.reasons),
            "retryable": self.retryable,
            "requires_user_consent": self.requires_user_consent,
            "escalation": self.escalation,
        }

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"LauncherDecision(action={self.action!r}, reasons={self.reasons!r}, retryable={self.retryable!r})"


@dataclass(frozen=True, slots=True)
class LifecycleCommandRequest:
    """Deny-by-default lifecycle command request shared by Tauri and API callers."""

    action: str
    origin: LifecycleCommandOrigin | str
    admin_equivalent: bool = False
    force: bool = False
    mode: LauncherRuntimeMode | str | None = None
    shell_window_visible: bool | None = None

    def normalized_origin(self) -> LifecycleCommandOrigin:
        """Return the typed command origin or raise for unknown callers.

        Returns:
            Normalized lifecycle command origin.
        """
        raw_origin = self.origin.value if isinstance(self.origin, LifecycleCommandOrigin) else self.origin
        return LifecycleCommandOrigin(raw_origin)

    def normalized_action(self) -> LifecycleAction:
        return LifecycleAction(self.action)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible request payload.

        Returns:
            Lifecycle command request as primitive JSON values.
        """
        origin = self.origin.value if isinstance(self.origin, LifecycleCommandOrigin) else str(self.origin)
        mode = self.mode.value if isinstance(self.mode, LauncherRuntimeMode) else self.mode
        return {
            "action": self.action,
            "origin": origin,
            "admin_equivalent": self.admin_equivalent,
            "force": self.force,
            "mode": mode,
            "shell_window_visible": self.shell_window_visible,
        }

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return (
            "LifecycleCommandRequest("
            f"action={self.action!r}, origin={self.origin!r}, admin_equivalent={self.admin_equivalent!r})"
        )


@dataclass(frozen=True, slots=True)
class LifecycleCommandResult:
    """Result returned by the lifecycle command boundary."""

    accepted: bool
    action: str
    decision: LauncherDecision
    status: LauncherStatus
    shutdown: dict[str, Any] | None = None
    recovery: dict[str, Any] | None = None
    denial_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "action": self.action,
            "decision": self.decision.to_dict(),
            "status": self.status.to_dict(),
            "shutdown": self.shutdown,
            "recovery": self.recovery,
            "denial_reason": self.denial_reason,
        }

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return (
            "LifecycleCommandResult("
            f"accepted={self.accepted!r}, action={self.action!r}, denial_reason={self.denial_reason!r})"
        )


class LauncherError(Exception):
    """Launcher lifecycle failure with user-visible remediation."""


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
]
