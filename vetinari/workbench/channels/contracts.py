"""Native Workbench Channel Hub contracts."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, is_dataclass
from enum import Enum
from typing import Any

SCHEMA_VERSION = "1.0"


class ChannelType(str, Enum):
    """Runtime contract for ChannelType."""

    DESKTOP = "desktop"
    BROWSER = "browser"
    MOBILE_COMPANION = "mobile_companion"
    CLI = "cli"
    SLACK = "slack"
    DISCORD = "discord"
    SMS = "sms"
    PUSH = "push"
    WEBHOOK = "webhook"


class ChannelCapability(str, Enum):
    """Runtime contract for ChannelCapability."""

    DELIVERY = "delivery"
    APPROVAL = "approval"
    COMMAND = "command"
    MEDIA = "media"
    ACTIVITY = "activity"
    NOTIFICATION = "notification"


class ChannelLifecycleState(str, Enum):
    """Runtime contract for ChannelLifecycleState."""

    ENABLED = "enabled"
    DISABLED = "disabled"
    MAINTENANCE = "maintenance"
    DEPRECATED = "deprecated"


class ChannelHealthState(str, Enum):
    """Runtime contract for ChannelHealthState."""

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    UNKNOWN = "unknown"


class ChannelDeliveryState(str, Enum):
    """Runtime contract for ChannelDeliveryState."""

    DELIVERED = "delivered"
    BLOCKED = "blocked"
    DEGRADED = "degraded"
    APPROVAL_REQUIRED = "approval_required"
    REDACTED = "redacted"


class ChannelBlockedReason(str, Enum):
    """Runtime contract for ChannelBlockedReason."""

    CONFIG_MISSING = "config_missing"
    CONFIG_UNREADABLE = "config_unreadable"
    CHANNEL_UNKNOWN = "channel_unknown"
    CHANNEL_DISABLED = "channel_disabled"
    CHANNEL_UNHEALTHY = "channel_unhealthy"
    CHANNEL_DEGRADED = "channel_degraded"
    DELIVERY_UNAUTHORIZED = "delivery_unauthorized"
    APPROVAL_REQUIRED = "approval_required"
    APPROVAL_DENIED = "approval_denied"
    APPROVAL_UNAVAILABLE = "approval_unavailable"
    REMOTE_INTENT_MISSING = "remote_intent_missing"
    REMOTE_INTENT_DENIED = "remote_intent_denied"
    INVALID_REQUEST = "invalid_request"


class ChannelRedactionState(str, Enum):
    """Runtime contract for ChannelRedactionState."""

    NONE = "none"
    APPLIED = "applied"


class ChannelApprovalMode(str, Enum):
    """Runtime contract for ChannelApprovalMode."""

    NONE = "none"
    REQUIRED = "required"
    OPTIONAL = "optional"


class ChannelCommandPolicy(str, Enum):
    """Runtime contract for ChannelCommandPolicy."""

    NONE = "none"
    LOCAL_ONLY = "local_only"
    REMOTE_INTENT_REQUIRED = "remote_intent_required"
    APPROVAL_CHAIN_REQUIRED = "approval_chain_required"
    REMOTE_INTENT_AND_APPROVAL_CHAIN = "remote_intent_and_approval_chain"


@dataclass(frozen=True, slots=True)
class ChannelRedactionPolicy:
    """Runtime contract for ChannelRedactionPolicy."""

    redact_secret_values: bool = True
    redact_binary_values: bool = True
    redact_local_paths: bool = True
    allowed_attachment_fields: tuple[str, ...] = ("id", "name", "media_type", "size", "sha256")

    def __post_init__(self) -> None:
        object.__setattr__(self, "allowed_attachment_fields", _clean_tuple(self.allowed_attachment_fields))

    def to_dict(self) -> dict[str, Any]:
        return _jsonify(self)

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"ChannelRedactionPolicy(redact_secret_values={self.redact_secret_values!r}, redact_binary_values={self.redact_binary_values!r}, redact_local_paths={self.redact_local_paths!r})"


@dataclass(frozen=True, slots=True)
class ChannelDefinition:
    """Runtime contract for ChannelDefinition."""

    channel_id: str
    channel_type: ChannelType | str
    display_name: str
    lifecycle_state: ChannelLifecycleState | str
    health_state: ChannelHealthState | str
    capabilities: tuple[ChannelCapability | str, ...]
    default_target: str
    redaction_policy: ChannelRedactionPolicy | dict[str, Any] = field(default_factory=ChannelRedactionPolicy)
    approval_policy: ChannelApprovalMode | str = ChannelApprovalMode.NONE
    command_authorization_policy: ChannelCommandPolicy | str = ChannelCommandPolicy.NONE
    health_detail: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for field_name in ("channel_id", "display_name", "default_target"):
            if not str(getattr(self, field_name)).strip():
                raise ValueError(f"{field_name} is required")
        object.__setattr__(self, "channel_id", self.channel_id.strip())
        object.__setattr__(self, "channel_type", ChannelType(_enum_text(self.channel_type)))
        object.__setattr__(self, "lifecycle_state", ChannelLifecycleState(_enum_text(self.lifecycle_state)))
        object.__setattr__(self, "health_state", ChannelHealthState(_enum_text(self.health_state)))
        try:
            capabilities = tuple(ChannelCapability(_enum_text(item)) for item in self.capabilities)
        except ValueError as exc:
            raise ValueError("unknown capability") from exc
        object.__setattr__(self, "capabilities", capabilities)
        policy = self.redaction_policy
        if isinstance(policy, dict):
            policy = ChannelRedactionPolicy(**policy)
        object.__setattr__(self, "redaction_policy", policy)
        object.__setattr__(self, "approval_policy", ChannelApprovalMode(_enum_text(self.approval_policy)))
        object.__setattr__(
            self, "command_authorization_policy", ChannelCommandPolicy(_enum_text(self.command_authorization_policy))
        )
        object.__setattr__(self, "metadata", dict(self.metadata))

    def to_dict(self) -> dict[str, Any]:
        return _jsonify(self)

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"ChannelDefinition(channel_id={self.channel_id!r}, channel_type={self.channel_type!r}, display_name={self.display_name!r})"


@dataclass(frozen=True, slots=True)
class ChannelHubConfig:
    """Runtime contract for ChannelHubConfig with strict schema-version admission."""

    schema_version: str
    channels: tuple[ChannelDefinition | dict[str, Any], ...]
    source: str = ""
    config_error: str = ""

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError(f"unsupported schema_version {self.schema_version!r}; expected {SCHEMA_VERSION!r}")
        object.__setattr__(self, "channels", tuple(_coerce_definition(item) for item in self.channels))

    def to_dict(self) -> dict[str, Any]:
        return _jsonify(self)

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"ChannelHubConfig(schema_version={self.schema_version!r}, channels={self.channels!r}, source={self.source!r})"


@dataclass(frozen=True, slots=True)
class ChannelResolution:
    """Runtime contract for ChannelResolution."""

    channel_id: str
    definition: ChannelDefinition | None
    allowed: bool
    blocked_reason: ChannelBlockedReason | str = ""
    summary: str = ""

    def __post_init__(self) -> None:
        if self.blocked_reason:
            object.__setattr__(self, "blocked_reason", ChannelBlockedReason(_enum_text(self.blocked_reason)))

    def to_dict(self) -> dict[str, Any]:
        return _jsonify(self)

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"ChannelResolution(channel_id={self.channel_id!r}, definition={self.definition!r}, allowed={self.allowed!r})"


@dataclass(frozen=True, slots=True)
class ChannelMediaItem:
    """Runtime contract for ChannelMediaItem."""

    media_id: str
    media_type: str
    payload: Any
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return _jsonify(self)

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"ChannelMediaItem(media_id={self.media_id!r}, media_type={self.media_type!r}, payload={self.payload!r})"


@dataclass(frozen=True, slots=True)
class ChannelDeliveryRequest:
    """Runtime contract for ChannelDeliveryRequest."""

    channel_id: str
    run_id: str
    actor_id: str
    action_id: str
    action_type: str
    summary: str
    payload: dict[str, Any] = field(default_factory=dict)
    media: tuple[ChannelMediaItem | dict[str, Any], ...] = ()
    approval_decision_id: str = ""
    action_fingerprint: str = ""

    def __post_init__(self) -> None:
        for field_name in ("channel_id", "run_id", "actor_id", "action_id", "action_type", "summary"):
            if not str(getattr(self, field_name)).strip():
                raise ValueError(f"{field_name} is required")
        object.__setattr__(self, "payload", dict(self.payload))
        object.__setattr__(self, "media", tuple(_coerce_media(item) for item in self.media))

    def to_dict(self) -> dict[str, Any]:
        return _jsonify(self)

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"ChannelDeliveryRequest(channel_id={self.channel_id!r}, run_id={self.run_id!r}, actor_id={self.actor_id!r})"


@dataclass(frozen=True, slots=True)
class ChannelDeliveryResult:
    """Runtime contract for ChannelDeliveryResult."""

    schema_version: str
    channel_id: str
    state: ChannelDeliveryState | str
    delivered: bool
    blocked_reason: ChannelBlockedReason | str | None
    redaction_applied: ChannelRedactionState | str
    envelope: dict[str, Any]
    activity: dict[str, Any] = field(default_factory=dict)
    ordered_trace: tuple[dict[str, Any], ...] = ()
    matched_step: str = ""
    fallback_rule: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "state", ChannelDeliveryState(_enum_text(self.state)))
        if self.blocked_reason:
            object.__setattr__(self, "blocked_reason", ChannelBlockedReason(_enum_text(self.blocked_reason)))
        object.__setattr__(self, "redaction_applied", ChannelRedactionState(_enum_text(self.redaction_applied)))
        object.__setattr__(self, "envelope", dict(self.envelope))
        object.__setattr__(self, "activity", dict(self.activity))

    def to_dict(self) -> dict[str, Any]:
        return _jsonify(self)

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"ChannelDeliveryResult(schema_version={self.schema_version!r}, channel_id={self.channel_id!r}, state={self.state!r})"


def _enum_text(value: Any) -> str:
    return str(getattr(value, "value", value))


def _coerce_definition(value: ChannelDefinition | dict[str, Any]) -> ChannelDefinition:
    return value if isinstance(value, ChannelDefinition) else ChannelDefinition(**dict(value))


def _coerce_media(value: ChannelMediaItem | dict[str, Any]) -> ChannelMediaItem:
    return value if isinstance(value, ChannelMediaItem) else ChannelMediaItem(**dict(value))


def _clean_tuple(values: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    return tuple(str(value).strip() for value in values if str(value).strip())


def _jsonify(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return {str(key): _jsonify(item) for key, item in asdict(value).items()}
    if isinstance(value, dict):
        return {str(key): _jsonify(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonify(item) for item in value]
    return value
