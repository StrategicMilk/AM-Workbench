"""Immutable contracts for pinned Workbench executable tool surfaces."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any

SCHEMA_VERSION = "1.0"


class WorkbenchToolTrustError(ValueError):
    """Raised when a tool-surface pin, diff, or approval cannot be trusted."""


class ToolSurfaceKind(str, Enum):
    """Executable tool surface categories that Workbench pins before use."""

    MCP_SERVER = "mcp_server"
    SHELL_COMMAND = "shell_command"
    BROWSER_AUTOMATION = "browser_automation"
    CONNECTOR = "connector"
    SKILL = "skill"
    AUTOMATION = "automation"
    LOCAL_HELPER = "local_helper"


class ToolTransport(str, Enum):
    """Transport or invocation channel for a tool surface."""

    STDIO = "stdio"
    HTTP = "http"
    WEBSOCKET = "websocket"
    BROWSER = "browser"
    CONNECTOR_API = "connector_api"
    LOCAL_PROCESS = "local_process"
    PYTHON = "python"
    NONE = "none"


class ToolPolicyMode(str, Enum):
    """Policy enforcement mode aligned with the Workbench policy verdict contract."""

    OBSERVE = "observe"
    WARN = "warn"
    STRICT = "strict"


class ToolTrustStatus(str, Enum):
    """Trust result emitted before an agent inherits tool authority."""

    ALLOWED = "allowed"
    BLOCKED = "blocked"
    DEGRADED = "degraded"


class ToolTrustReason(str, Enum):
    """Machine-readable fail-closed and approval reasons."""

    ALLOWED = "allowed"
    UNKNOWN_TOOL_SURFACE = "unknown_tool_surface"
    CORRUPT_TOOL_SURFACE = "corrupt_tool_surface"
    MISSING_AUTHORITY = "missing_authority"
    MISSING_PROVENANCE = "missing_provenance"
    STALE_PIN = "stale_pin"
    MISSING_CAPABILITY_DIFF = "missing_capability_diff"
    APPROVAL_REQUIRED = "approval_required"
    APPROVAL_MISMATCH = "approval_mismatch"
    COMMAND_CHANGED = "command_changed"
    HOST_CHANGED = "host_changed"
    TRANSPORT_CHANGED = "transport_changed"
    VERSION_CHANGED = "version_changed"
    PERMISSIONS_EXPANDED = "permissions_expanded"
    TRUST_BOUNDARY_CHANGED = "trust_boundary_changed"


POWER_FIELDS = (
    "surface_kind",
    "command",
    "host",
    "transport",
    "permissions",
    "policy_mode",
    "version",
    "trust_boundary",
)


def _non_empty_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise WorkbenchToolTrustError(f"{field_name} must be non-empty")
    return value.strip()


def _string_tuple(value: object, field_name: str, *, required: bool = False) -> tuple[str, ...]:
    if value is None:
        rows: tuple[str, ...] = ()
    elif isinstance(value, (str, bytes)) or not isinstance(value, tuple | list):
        raise WorkbenchToolTrustError(f"{field_name} must be a list of strings")
    else:
        rows = tuple(str(item).strip() for item in value if str(item).strip())
    if required and not rows:
        raise WorkbenchToolTrustError(f"{field_name} must be non-empty")
    if len(rows) != len(set(rows)):
        raise WorkbenchToolTrustError(f"{field_name} must not contain duplicates")
    return rows


def _string_map(value: object, field_name: str) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise WorkbenchToolTrustError(f"{field_name} must be a mapping")
    return {str(key): str(row) for key, row in value.items()}


@dataclass(frozen=True, slots=True)
class ToolSurfacePin:
    """Pinned manifest for one executable tool surface."""

    surface_id: str
    surface_kind: ToolSurfaceKind
    command: str
    host: str
    transport: ToolTransport
    permissions: tuple[str, ...]
    owner: str
    policy_mode: ToolPolicyMode
    version: str
    authority_refs: tuple[str, ...]
    provenance_refs: tuple[str, ...]
    captured_at_utc: str
    trust_boundary: str
    max_staleness_hours: int = 24
    policy_verdict_refs: tuple[str, ...] = ()
    capability_pack_refs: tuple[str, ...] = ()
    adapter_authority_refs: tuple[str, ...] = ()
    metadata: Mapping[str, str] = field(default_factory=dict)
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise WorkbenchToolTrustError(f"schema_version must be {SCHEMA_VERSION}")
        object.__setattr__(self, "surface_id", _non_empty_text(self.surface_id, "surface_id"))
        object.__setattr__(self, "command", _non_empty_text(self.command, "command"))
        object.__setattr__(self, "host", _non_empty_text(self.host, "host"))
        object.__setattr__(self, "owner", _non_empty_text(self.owner, "owner"))
        object.__setattr__(self, "version", _non_empty_text(self.version, "version"))
        object.__setattr__(self, "captured_at_utc", _non_empty_text(self.captured_at_utc, "captured_at_utc"))
        object.__setattr__(self, "trust_boundary", _non_empty_text(self.trust_boundary, "trust_boundary"))
        object.__setattr__(self, "surface_kind", ToolSurfaceKind(self.surface_kind))
        object.__setattr__(self, "transport", ToolTransport(self.transport))
        object.__setattr__(self, "policy_mode", ToolPolicyMode(self.policy_mode))
        object.__setattr__(
            self, "permissions", tuple(sorted(_string_tuple(self.permissions, "permissions", required=True)))
        )
        object.__setattr__(
            self,
            "authority_refs",
            tuple(sorted(_string_tuple(self.authority_refs, "authority_refs", required=True))),
        )
        object.__setattr__(
            self,
            "provenance_refs",
            tuple(sorted(_string_tuple(self.provenance_refs, "provenance_refs", required=True))),
        )
        object.__setattr__(
            self, "policy_verdict_refs", tuple(sorted(_string_tuple(self.policy_verdict_refs, "policy_verdict_refs")))
        )
        object.__setattr__(
            self,
            "capability_pack_refs",
            tuple(sorted(_string_tuple(self.capability_pack_refs, "capability_pack_refs"))),
        )
        object.__setattr__(
            self,
            "adapter_authority_refs",
            tuple(sorted(_string_tuple(self.adapter_authority_refs, "adapter_authority_refs"))),
        )
        if self.max_staleness_hours < 1:
            raise WorkbenchToolTrustError("max_staleness_hours must be >= 1")
        object.__setattr__(self, "metadata", _string_map(self.metadata, "metadata"))

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> ToolSurfacePin:
        """Build a pin from a schema-shaped mapping."""
        return cls(
            schema_version=str(payload.get("schema_version", SCHEMA_VERSION)),
            surface_id=str(payload.get("surface_id", "")),
            surface_kind=ToolSurfaceKind(str(payload.get("surface_kind", ""))),
            command=str(payload.get("command", "")),
            host=str(payload.get("host", "")),
            transport=ToolTransport(str(payload.get("transport", ""))),
            permissions=_string_tuple(payload.get("permissions"), "permissions", required=True),
            owner=str(payload.get("owner", "")),
            policy_mode=ToolPolicyMode(str(payload.get("policy_mode", ""))),
            version=str(payload.get("version", "")),
            authority_refs=_string_tuple(payload.get("authority_refs"), "authority_refs", required=True),
            provenance_refs=_string_tuple(payload.get("provenance_refs"), "provenance_refs", required=True),
            captured_at_utc=str(payload.get("captured_at_utc", "")),
            trust_boundary=str(payload.get("trust_boundary", "")),
            max_staleness_hours=int(payload.get("max_staleness_hours", 24)),
            policy_verdict_refs=_string_tuple(payload.get("policy_verdict_refs", ()), "policy_verdict_refs"),
            capability_pack_refs=_string_tuple(payload.get("capability_pack_refs", ()), "capability_pack_refs"),
            adapter_authority_refs=_string_tuple(payload.get("adapter_authority_refs", ()), "adapter_authority_refs"),
            metadata=_string_map(payload.get("metadata", {}), "metadata"),
        )

    def power(self) -> dict[str, object]:
        """Return the power-bearing fields that must be approved when changed."""
        return {
            "surface_kind": self.surface_kind.value,
            "command": self.command,
            "host": self.host,
            "transport": self.transport.value,
            "permissions": self.permissions,
            "policy_mode": self.policy_mode.value,
            "version": self.version,
            "trust_boundary": self.trust_boundary,
        }

    def to_schema_payload(self) -> dict[str, Any]:
        """Return a JSON-schema-ready payload."""
        return {
            "schema_version": self.schema_version,
            "surface_id": self.surface_id,
            "surface_kind": self.surface_kind.value,
            "command": self.command,
            "host": self.host,
            "transport": self.transport.value,
            "permissions": list(self.permissions),
            "owner": self.owner,
            "policy_mode": self.policy_mode.value,
            "version": self.version,
            "authority_refs": list(self.authority_refs),
            "provenance_refs": list(self.provenance_refs),
            "captured_at_utc": self.captured_at_utc,
            "trust_boundary": self.trust_boundary,
            "max_staleness_hours": self.max_staleness_hours,
            "policy_verdict_refs": list(self.policy_verdict_refs),
            "capability_pack_refs": list(self.capability_pack_refs),
            "adapter_authority_refs": list(self.adapter_authority_refs),
            "metadata": dict(sorted(self.metadata.items())),
        }

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"ToolSurfacePin(surface_id={self.surface_id!r}, surface_kind={self.surface_kind!r}, command={self.command!r})"


@dataclass(frozen=True, slots=True)
class ToolSurfacePowerChange:
    """One changed power-bearing field in a tool-surface diff."""

    field_name: str
    old_value: object
    new_value: object
    reason: ToolTrustReason

    def __post_init__(self) -> None:
        _non_empty_text(self.field_name, "field_name")
        object.__setattr__(self, "reason", ToolTrustReason(self.reason))

    def to_dict(self) -> dict[str, object]:
        return {
            "field_name": self.field_name,
            "old_value": self.old_value,
            "new_value": self.new_value,
            "reason": self.reason.value,
        }

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"ToolSurfacePowerChange(field_name={self.field_name!r}, old_value={self.old_value!r}, new_value={self.new_value!r})"


@dataclass(frozen=True, slots=True)
class ToolSurfaceCapabilityDiff:
    """Capability delta that must be shown before a tool gains new power."""

    surface_id: str
    old_power: Mapping[str, object]
    new_power: Mapping[str, object]
    changes: tuple[ToolSurfacePowerChange, ...]
    permission_expansions: tuple[str, ...]
    generated_at_utc: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "surface_id", _non_empty_text(self.surface_id, "surface_id"))
        object.__setattr__(
            self,
            "permission_expansions",
            tuple(sorted(_string_tuple(self.permission_expansions, "permission_expansions"))),
        )
        object.__setattr__(self, "generated_at_utc", _non_empty_text(self.generated_at_utc, "generated_at_utc"))
        object.__setattr__(self, "changes", tuple(self.changes))

    @property
    def reasons(self) -> tuple[ToolTrustReason, ...]:
        """Return the stable reason vocabulary represented by this diff."""
        return tuple(dict.fromkeys(change.reason for change in self.changes))

    @property
    def requires_approval(self) -> bool:
        """Return whether the diff changes inherited authority."""
        return bool(self.changes)

    def to_dict(self) -> dict[str, object]:
        return {
            "surface_id": self.surface_id,
            "old_power": dict(self.old_power),
            "new_power": dict(self.new_power),
            "changes": [change.to_dict() for change in self.changes],
            "permission_expansions": list(self.permission_expansions),
            "generated_at_utc": self.generated_at_utc,
            "requires_approval": self.requires_approval,
            "reasons": [reason.value for reason in self.reasons],
        }

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"ToolSurfaceCapabilityDiff(surface_id={self.surface_id!r}, old_power={self.old_power!r}, new_power={self.new_power!r})"


@dataclass(frozen=True, slots=True)
class ToolSurfaceApproval:
    """User approval for a specific old-power to new-power transition."""

    approval_id: str
    surface_id: str
    approved_by: str
    approved_at_utc: str
    old_power: Mapping[str, object]
    new_power: Mapping[str, object]
    diff_reasons: tuple[ToolTrustReason, ...]
    evidence_refs: tuple[str, ...]
    policy_verdict_ref: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "approval_id", _non_empty_text(self.approval_id, "approval_id"))
        object.__setattr__(self, "surface_id", _non_empty_text(self.surface_id, "surface_id"))
        object.__setattr__(self, "approved_by", _non_empty_text(self.approved_by, "approved_by"))
        object.__setattr__(self, "approved_at_utc", _non_empty_text(self.approved_at_utc, "approved_at_utc"))
        object.__setattr__(self, "policy_verdict_ref", _non_empty_text(self.policy_verdict_ref, "policy_verdict_ref"))
        object.__setattr__(self, "diff_reasons", tuple(ToolTrustReason(reason) for reason in self.diff_reasons))
        object.__setattr__(
            self, "evidence_refs", tuple(sorted(_string_tuple(self.evidence_refs, "evidence_refs", required=True)))
        )

    def matches(self, diff: ToolSurfaceCapabilityDiff) -> bool:
        """Return whether this approval authorizes exactly this capability diff."""
        return (
            self.surface_id == diff.surface_id
            and dict(self.old_power) == dict(diff.old_power)
            and dict(self.new_power) == dict(diff.new_power)
            and self.diff_reasons == diff.reasons
        )

    def to_dict(self) -> dict[str, object]:
        """Execute the to dict operation.

        Returns:
            dict[str, object] value produced by to_dict().
        """
        payload = asdict(self)
        payload["diff_reasons"] = [reason.value for reason in self.diff_reasons]
        payload["evidence_refs"] = list(self.evidence_refs)
        return payload

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"ToolSurfaceApproval(approval_id={self.approval_id!r}, surface_id={self.surface_id!r}, approved_by={self.approved_by!r})"


@dataclass(frozen=True, slots=True)
class ToolSurfaceTrustDecision:
    """Fail-closed decision emitted by the tool-trust runtime."""

    surface_id: str
    status: ToolTrustStatus
    allowed: bool
    reasons: tuple[ToolTrustReason, ...]
    checked_at_utc: str
    capability_diff: ToolSurfaceCapabilityDiff | None = None
    approval: ToolSurfaceApproval | None = None
    details: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "surface_id", _non_empty_text(self.surface_id, "surface_id"))
        object.__setattr__(self, "status", ToolTrustStatus(self.status))
        object.__setattr__(self, "reasons", tuple(ToolTrustReason(reason) for reason in self.reasons))
        object.__setattr__(self, "checked_at_utc", _non_empty_text(self.checked_at_utc, "checked_at_utc"))
        object.__setattr__(self, "details", _string_map(self.details, "details"))
        if self.allowed and self.status is not ToolTrustStatus.ALLOWED:
            raise WorkbenchToolTrustError("allowed decisions must use allowed status")

    def require_allowed(self) -> None:
        """Raise a typed error when a caller tries to inherit blocked authority.

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        if not self.allowed:
            reason_text = ", ".join(reason.value for reason in self.reasons)
            raise WorkbenchToolTrustError(f"tool surface {self.surface_id!r} blocked: {reason_text}")

    def to_dict(self) -> dict[str, object]:
        return {
            "surface_id": self.surface_id,
            "status": self.status.value,
            "allowed": self.allowed,
            "reasons": [reason.value for reason in self.reasons],
            "checked_at_utc": self.checked_at_utc,
            "capability_diff": self.capability_diff.to_dict() if self.capability_diff else None,
            "approval": self.approval.to_dict() if self.approval else None,
            "details": dict(sorted(self.details.items())),
        }

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"ToolSurfaceTrustDecision(surface_id={self.surface_id!r}, status={self.status!r}, allowed={self.allowed!r})"
