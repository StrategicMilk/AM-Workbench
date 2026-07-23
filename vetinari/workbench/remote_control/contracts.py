"""Backend contracts for mobile companion control.

The objects in this module describe mobile-originated intent. They do not
execute desktop actions, mount routes, create relays, or grant raw host
authority.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


class RemoteControlDecisionValue(str, Enum):
    """Stable decisions returned by remote-control gates."""

    ALLOW = "allow"
    REQUIRE_APPROVAL = "require_approval"
    BLOCK = "block"
    DEGRADED = "degraded"
    ADVISORY = "advisory"


class RemoteControlFailureReason(str, Enum):
    """Machine-readable fail-closed reasons for companion control."""

    MISSING_IDENTITY = "missing_identity"
    MISSING_DEVICE_POSTURE = "missing_device_posture"
    DEVICE_NOT_ENROLLED = "device_not_enrolled"
    STALE_DEVICE_POSTURE = "stale_device_posture"
    MISSING_SERVICE_BINDING = "missing_service_binding"
    UNSAFE_SERVICE_BINDING = "unsafe_service_binding"
    MISSING_POLICY_VERSION = "missing_policy_version"
    MISSING_EVIDENCE = "missing_evidence"
    MISSING_REPLAY_METADATA = "missing_replay_metadata"
    EXPIRED_INTENT = "expired_intent"
    RAW_DESKTOP_AUTHORITY = "raw_desktop_authority"
    UNTRUSTED_IDENTITY_PATH = "untrusted_identity_path"
    CLOUDFLARE_ACCESS_MISSING = "cloudflare_access_missing"
    RUNTIME_UNKNOWN = "runtime_unknown"
    RUNTIME_DEGRADED = "runtime_degraded"
    MISSING_POLICY_VERDICT = "missing_policy_verdict"
    POLICY_BLOCKED = "policy_blocked"
    RISK_CONTEXT_DEGRADED = "risk_context_degraded"
    WATCHER_MISSING_EVIDENCE = "watcher_missing_evidence"
    WATCHER_STRICT_BLOCK = "watcher_strict_block"
    MISSION_CONTROL_UNAVAILABLE = "mission_control_unavailable"
    DUPLICATE_INTENT = "duplicate_intent"
    APPROVAL_REUSED = "approval_reused"
    APPROVAL_EXPIRED = "approval_expired"
    STALE_MISSION_SNAPSHOT = "stale_mission_snapshot"
    STALE_MATERIAL_FINGERPRINT = "stale_material_fingerprint"
    STALE_POLICY_VERSION = "stale_policy_version"
    WRONG_DEVICE_IDENTITY = "wrong_device_identity"
    APPROVAL_ACTOR_MISMATCH = "approval_actor_mismatch"
    INJECTED_EXECUTOR_REJECTED = "injected_executor_rejected"


class RemoteControlError(ValueError):
    """Typed construction failure for unsafe remote-control payloads."""

    def __init__(self, reason: RemoteControlFailureReason, message: str = "") -> None:
        self.reason = reason
        self.message = message or reason.value
        super().__init__(str(self))

    def __str__(self) -> str:
        return f"RemoteControlError[{self.reason.value}]: {self.message}"


class RemoteIntentKind(str, Enum):
    """Companion-only intent vocabulary."""

    INSPECT_MISSION = "inspect_mission"
    APPROVE_ACTION = "approve_action"
    DENY_ACTION = "deny_action"
    PAUSE_AGENT = "pause_agent"
    RESUME_AGENT = "resume_agent"
    REQUEST_SUMMARY = "request_summary"
    CONTINUE_WORK = "continue_work"
    CHANGE_PRIORITY = "change_priority"
    NOTIFY_WHEN_BLOCKED = "notify_when_blocked"


class RemoteIntentRiskTier(str, Enum):
    """Risk tiers used before desktop-local execution authority evaluates work."""

    OBSERVE = "observe"
    GUIDE = "guide"
    CONTROL = "control"
    APPROVAL = "approval"


class RemoteAccessMode(str, Enum):
    """Supported access paths in preferred order."""

    TAILSCALE_SERVE = "tailscale_serve"
    CLOUDFLARE_ACCESS = "cloudflare_access"
    MANUAL_ADVANCED = "manual_advanced"


_RAW_AUTHORITY_VALUES = {
    "shell",
    "run_command",
    "read_file",
    "write_file",
    "mcp_call",
    "credential_dump",
    "package_install",
    "publish",
    "local_network_probe",
}
_REPLAY_KEYS = (
    "nonce",
    "mission_snapshot_ref",
    "material_fingerprint",
    "approval_state_ref",
)


@dataclass(frozen=True, slots=True)
class RemoteIdentity:
    """Authenticated user identity as attested by the trusted access path."""

    actor_id: str
    subject: str
    provider: str = "tailscale"
    display_name: str = ""
    trusted_proxy_id: str = ""

    def __post_init__(self) -> None:
        if not self.actor_id.strip() or not self.subject.strip() or not self.provider.strip():
            raise RemoteControlError(RemoteControlFailureReason.MISSING_IDENTITY)

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"RemoteIdentity(actor_id={self.actor_id!r}, subject={self.subject!r}, provider={self.provider!r})"


@dataclass(frozen=True, slots=True)
class RemoteDevicePosture:
    """Device identity and enrollment posture for the mobile companion."""

    device_id: str
    user_id: str
    enrolled: bool
    posture_state: str
    attested_at_utc: str
    tailnet_node_id: str = ""

    def __post_init__(self) -> None:
        if not self.device_id.strip() or not self.user_id.strip() or not self.attested_at_utc.strip():
            raise RemoteControlError(RemoteControlFailureReason.MISSING_DEVICE_POSTURE)
        if not self.enrolled:
            raise RemoteControlError(RemoteControlFailureReason.DEVICE_NOT_ENROLLED)
        if self.posture_state.strip().lower() not in {"healthy", "trusted", "compliant"}:
            raise RemoteControlError(RemoteControlFailureReason.STALE_DEVICE_POSTURE)
        _require_fresh_timestamp(self.attested_at_utc, RemoteControlFailureReason.STALE_DEVICE_POSTURE)

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return (
            f"RemoteDevicePosture(device_id={self.device_id!r}, user_id={self.user_id!r}, enrolled={self.enrolled!r})"
        )


@dataclass(frozen=True, slots=True)
class RemoteServiceBinding:
    """Desktop-local service binding seen through a remote access path."""

    service_id: str
    access_mode: RemoteAccessMode | str
    origin: str
    host: str
    port: int
    localhost_only: bool
    trusted_proxy: bool
    public_hostname: str = ""
    cloudflare_access_policy_ref: str = ""

    def __post_init__(self) -> None:
        if not self.service_id.strip() or not self.origin.strip() or not self.host.strip() or self.port <= 0:
            raise RemoteControlError(RemoteControlFailureReason.MISSING_SERVICE_BINDING)
        object.__setattr__(self, "access_mode", RemoteAccessMode(self.access_mode))

    @property
    def is_localhost_origin(self) -> bool:
        return self.localhost_only and self.host in {"127.0.0.1", "::1", "localhost"}

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"RemoteServiceBinding(service_id={self.service_id!r}, access_mode={self.access_mode!r}, origin={self.origin!r})"


@dataclass(frozen=True, slots=True)
class RemoteIntent:
    """Immutable mobile companion intent."""

    intent_id: str
    project_id: str
    run_id: str
    mission_id: str
    kind: RemoteIntentKind | str
    risk_tier: RemoteIntentRiskTier | str
    actor: RemoteIdentity
    device: RemoteDevicePosture
    service_binding: RemoteServiceBinding
    created_at_utc: str
    expires_at_utc: str
    policy_version: str
    evidence_refs: tuple[str, ...]
    replay_metadata: dict[str, str]
    parameters: dict[str, Any] = field(default_factory=dict)
    remote_intent_verified: bool = True

    def __post_init__(self) -> None:
        for field_name in ("intent_id", "project_id", "run_id", "mission_id"):
            _require_text(getattr(self, field_name), RemoteControlFailureReason.RAW_DESKTOP_AUTHORITY, field_name)
        object.__setattr__(self, "kind", _coerce_intent_kind(self.kind))
        object.__setattr__(self, "risk_tier", RemoteIntentRiskTier(self.risk_tier))
        if not isinstance(self.actor, RemoteIdentity):
            raise RemoteControlError(RemoteControlFailureReason.MISSING_IDENTITY)
        if not isinstance(self.device, RemoteDevicePosture):
            raise RemoteControlError(RemoteControlFailureReason.MISSING_DEVICE_POSTURE)
        if not isinstance(self.service_binding, RemoteServiceBinding):
            raise RemoteControlError(RemoteControlFailureReason.MISSING_SERVICE_BINDING)
        if not self.policy_version.strip():
            raise RemoteControlError(RemoteControlFailureReason.MISSING_POLICY_VERSION)
        evidence_refs = _clean_tuple(self.evidence_refs)
        if not evidence_refs:
            raise RemoteControlError(RemoteControlFailureReason.MISSING_EVIDENCE)
        object.__setattr__(self, "evidence_refs", evidence_refs)
        replay_metadata = {str(key): str(value) for key, value in dict(self.replay_metadata).items()}
        if any(not replay_metadata.get(key, "").strip() for key in _REPLAY_KEYS):
            raise RemoteControlError(RemoteControlFailureReason.MISSING_REPLAY_METADATA)
        object.__setattr__(self, "replay_metadata", replay_metadata)
        _require_not_expired(self.expires_at_utc, RemoteControlFailureReason.EXPIRED_INTENT)

    def to_schema_payload(self) -> dict[str, Any]:
        """Return the JSON payload accepted by the remote intent schema.

        Returns:
            dict[str, Any] value produced by to_schema_payload().
        """
        payload = asdict(self)
        payload["kind"] = self.kind.value
        payload["risk_tier"] = self.risk_tier.value
        payload["access_mode"] = self.service_binding.access_mode.value
        payload["service_binding"]["access_mode"] = self.service_binding.access_mode.value
        payload["evidence_refs"] = list(self.evidence_refs)
        return payload

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"RemoteIntent(intent_id={self.intent_id!r}, project_id={self.project_id!r}, run_id={self.run_id!r})"


@dataclass(frozen=True, slots=True)
class RemoteApproval:
    """Advisory approval metadata bound to one remote intent."""

    approval_id: str
    intent_id: str
    approved_by: RemoteIdentity
    device_id: str
    approved_at_utc: str
    expires_at_utc: str
    policy_version: str
    material_fingerprint: str
    mission_snapshot_ref: str
    evidence_refs: tuple[str, ...]
    replay_metadata: dict[str, str]

    def __post_init__(self) -> None:
        for field_name in (
            "approval_id",
            "intent_id",
            "device_id",
            "approved_at_utc",
            "policy_version",
            "material_fingerprint",
            "mission_snapshot_ref",
        ):
            _require_text(getattr(self, field_name), RemoteControlFailureReason.MISSING_REPLAY_METADATA, field_name)
        if not isinstance(self.approved_by, RemoteIdentity):
            raise RemoteControlError(RemoteControlFailureReason.MISSING_IDENTITY)
        if not _clean_tuple(self.evidence_refs):
            raise RemoteControlError(RemoteControlFailureReason.MISSING_EVIDENCE)
        object.__setattr__(self, "evidence_refs", _clean_tuple(self.evidence_refs))
        _require_not_expired(self.expires_at_utc, RemoteControlFailureReason.APPROVAL_EXPIRED)

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"RemoteApproval(approval_id={self.approval_id!r}, intent_id={self.intent_id!r}, approved_by={self.approved_by!r})"


@dataclass(frozen=True, slots=True)
class RemoteControlDecision:
    """Result of access, policy, watcher, and read-model evaluation."""

    value: RemoteControlDecisionValue | str
    summary: str
    failure_reasons: tuple[RemoteControlFailureReason, ...] = ()
    payload: dict[str, Any] = field(default_factory=dict)
    evidence_refs: tuple[str, ...] = ()
    policy_version: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "value", RemoteControlDecisionValue(self.value))
        object.__setattr__(
            self,
            "failure_reasons",
            tuple(
                reason if isinstance(reason, RemoteControlFailureReason) else RemoteControlFailureReason(reason)
                for reason in self.failure_reasons
            ),
        )
        object.__setattr__(self, "evidence_refs", _clean_tuple(self.evidence_refs))

    @property
    def allowed(self) -> bool:
        return self.value in {RemoteControlDecisionValue.ALLOW, RemoteControlDecisionValue.ADVISORY}

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"RemoteControlDecision(value={self.value!r}, summary={self.summary!r}, failure_reasons={self.failure_reasons!r})"


def _coerce_intent_kind(value: RemoteIntentKind | str) -> RemoteIntentKind:
    raw = getattr(value, "value", value)
    if str(raw) in _RAW_AUTHORITY_VALUES:
        raise RemoteControlError(RemoteControlFailureReason.RAW_DESKTOP_AUTHORITY, f"{raw!r} is not a companion intent")
    return RemoteIntentKind(str(raw))


def _clean_tuple(values: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    return tuple(str(value).strip() for value in values if str(value).strip())


def _require_text(value: str, reason: RemoteControlFailureReason, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise RemoteControlError(reason, f"{field_name} is required")


def _require_not_expired(expires_at_utc: str, reason: RemoteControlFailureReason) -> None:
    expires_at = _parse_utc(expires_at_utc)
    if expires_at is None or expires_at <= datetime.now(timezone.utc):
        raise RemoteControlError(reason)


def _require_fresh_timestamp(
    timestamp_utc: str,
    reason: RemoteControlFailureReason,
    *,
    max_age: timedelta = timedelta(minutes=15),
    max_future_skew: timedelta = timedelta(minutes=2),
) -> None:
    timestamp = _parse_utc(timestamp_utc)
    now = datetime.now(timezone.utc)
    if timestamp is None or timestamp < now - max_age or timestamp > now + max_future_skew:
        raise RemoteControlError(reason)


def _parse_utc(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        logger.warning("Handled recoverable failure before fallback.", exc_info=True)
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


__all__ = [
    "RemoteAccessMode",
    "RemoteApproval",
    "RemoteControlDecision",
    "RemoteControlDecisionValue",
    "RemoteControlError",
    "RemoteControlFailureReason",
    "RemoteDevicePosture",
    "RemoteIdentity",
    "RemoteIntent",
    "RemoteIntentKind",
    "RemoteIntentRiskTier",
    "RemoteServiceBinding",
]
