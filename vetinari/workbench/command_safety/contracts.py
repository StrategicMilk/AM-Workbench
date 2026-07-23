"""Contracts for Workbench command and tool safety decisions."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any

SCHEMA_VERSION = "1.0"


class CommandSafetyError(RuntimeError):
    """Fail-closed command-safety error."""


class CommandSurface(str, Enum):
    """Runtime contract for CommandSurface."""

    SHELL = "shell"
    TERMINAL = "terminal"
    MCP_TOOL = "mcp_tool"
    CONNECTOR = "connector"
    AUTOMATION = "automation"
    PACKAGE_MANAGER = "package_manager"


class CommandSafetyVerdict(str, Enum):
    """Runtime contract for CommandSafetyVerdict."""

    ALLOW = "allow"
    REQUIRE_HUMAN_APPROVAL = "require_human_approval"
    BLOCK = "block"
    DEGRADED_MISSING_POLICY = "degraded_missing_policy"


class CommandSafetyReason(str, Enum):
    """Runtime contract for CommandSafetyReason."""

    SAFE_PREFIX_ALLOWED = "safe-prefix-allowed"
    UNSAFE_OPERATOR = "unsafe-operator"
    DESTRUCTIVE_PATTERN = "destructive-pattern"
    SECRET_EXPOSURE = "secret" + "-exposure"
    BACKGROUND_EXECUTION = "background-execution"
    COMMAND_SUBSTITUTION = "command-substitution"
    NETWORK_EXFILTRATION = "network-exfiltration"
    PROCESS_MUTATION = "process-mutation"
    PACKAGE_MUTATION = "package-mutation"
    PRIVILEGE_ESCALATION = "privilege-escalation"
    PATH_TRAVERSAL = "path-traversal"
    APPROVAL_REQUIRED = "approval-required"
    BLOCKED = "blocked"
    MISSING_POLICY = "missing-policy"
    CORRUPT_POLICY = "corrupt-policy"
    UNKNOWN_PROFILE = "unknown-profile"
    UNKNOWN_TOOL_SURFACE = "unknown-tool-surface"
    TOOL_PIN_DRIFT = "tool-pin-drift"
    STALE_TOOL_PIN = "stale-tool-pin"
    CORRUPT_TOOL_PIN = "corrupt-tool-pin"
    CWD_RECOVERY_NEEDED = "cwd-recovery-needed"
    CORRUPT_CWD_HISTORY = "corrupt-cwd-history"
    CWD_OUTSIDE_ALLOWED_ROOT = "cwd-outside-allowed-root"
    RECEIPT_EMITTED = "receipt-emitted"
    RECEIPT_UNAVAILABLE = "receipt-unavailable"
    DUPLICATE_IDEMPOTENCY_KEY = "duplicate-idempotency-key"
    APPROVAL_CHAIN_DENIED = "approval-chain-denied"
    APPROVAL_CHAIN_UNAVAILABLE = "approval-chain-unavailable"
    TOOL_PIN_ALLOWED = "tool-pin-allowed"


@dataclass(frozen=True, slots=True)
class CommandSafetyProfile:
    """Runtime contract for CommandSafetyProfile."""

    profile_id: str
    surfaces: tuple[CommandSurface, ...]
    safe_prefixes: tuple[str, ...]
    approval_prefixes: tuple[str, ...] = ()
    blocked_patterns: tuple[str, ...] = ()
    allowed_cwd_roots: tuple[str, ...] = ()
    require_tool_pin: bool = True
    allow_without_human_approval: bool = False

    def __post_init__(self) -> None:
        if not self.profile_id.strip():
            raise CommandSafetyError("profile_id is required")
        object.__setattr__(self, "surfaces", tuple(CommandSurface(surface) for surface in self.surfaces))
        for field_name in ("safe_prefixes", "approval_prefixes", "blocked_patterns", "allowed_cwd_roots"):
            object.__setattr__(self, field_name, _clean_tuple(getattr(self, field_name)))

    def to_dict(self) -> dict[str, Any]:
        """Execute the to dict operation.

        Returns:
            dict[str, Any] value produced by to_dict().
        """
        payload = asdict(self)
        payload["id"] = self.profile_id
        payload["surfaces"] = [surface.value for surface in self.surfaces]
        return payload

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"CommandSafetyProfile(profile_id={self.profile_id!r}, surfaces={self.surfaces!r}, safe_prefixes={self.safe_prefixes!r})"


@dataclass(frozen=True, slots=True)
class CommandSafetyContext:
    """Runtime contract for CommandSafetyContext."""

    project_id: str
    run_id: str
    session_id: str
    surface_id: str
    surface: CommandSurface | str
    profile_id: str
    actor_id: str
    cwd: str
    command: str
    idempotency_key: str = ""
    approval_sources: tuple[str, ...] = ()
    readiness_signals: dict[str, Any] | None = None
    governance_available: bool = True
    governance_mode: str = "observe"
    pinned_surfaces: dict[str, Any] = field(default_factory=dict)
    observed_surface: Any | None = None
    capability_diff: Any | None = None
    tool_surface_approval: Any | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "surface", CommandSurface(self.surface))
        object.__setattr__(self, "approval_sources", _clean_tuple(self.approval_sources))
        for field_name in ("project_id", "run_id", "session_id", "surface_id", "profile_id", "actor_id", "command"):
            if not str(getattr(self, field_name)).strip():
                raise CommandSafetyError(f"{field_name} is required")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"CommandSafetyContext(project_id={self.project_id!r}, run_id={self.run_id!r}, session_id={self.session_id!r})"


@dataclass(frozen=True, slots=True)
class CommandClassification:
    """Runtime contract for CommandClassification."""

    normalized_command: str
    tokens: tuple[str, ...]
    safe_prefix_matched: str
    verdict: CommandSafetyVerdict
    reasons: tuple[CommandSafetyReason, ...]
    unsafe_fragments: tuple[str, ...] = ()
    requires_approval: bool = False
    hard_block: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "normalized_command": self.normalized_command,
            "tokens": list(self.tokens),
            "safe_prefix_matched": self.safe_prefix_matched,
            "verdict": self.verdict.value,
            "reasons": [r.value for r in self.reasons],
            "unsafe_fragments": list(self.unsafe_fragments),
            "requires_approval": self.requires_approval,
            "hard_block": self.hard_block,
        }

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"CommandClassification(normalized_command={self.normalized_command!r}, tokens={self.tokens!r}, safe_prefix_matched={self.safe_prefix_matched!r})"


@dataclass(frozen=True, slots=True)
class CwdHistoryStatus:
    """Runtime contract for CwdHistoryStatus."""

    status: str
    reasons: tuple[CommandSafetyReason, ...]
    cwd: str = ""
    history: tuple[dict[str, Any], ...] = ()
    revision: int = 0
    state_path: str = ""

    @property
    def allows_execution(self) -> bool:
        return self.status == "ready" and bool(self.cwd)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reasons": [r.value for r in self.reasons],
            "cwd": self.cwd,
            "history": list(self.history),
            "revision": self.revision,
            "state_path": self.state_path,
            "allows_execution": self.allows_execution,
        }

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"CwdHistoryStatus(status={self.status!r}, reasons={self.reasons!r}, cwd={self.cwd!r})"


@dataclass(frozen=True, slots=True)
class CommandSafetyDecision:
    """Runtime contract for CommandSafetyDecision."""

    decision_id: str
    schema_version: str
    project_id: str
    run_id: str
    session_id: str
    surface_id: str
    surface: str
    profile_id: str
    command_fingerprint: str
    normalized_command: str
    verdict: CommandSafetyVerdict
    allowed: bool
    human_approval_required: bool
    reasons: tuple[CommandSafetyReason, ...]
    classifier: CommandClassification
    approval_chain: dict[str, Any] | None
    tool_surface: dict[str, Any] | None
    cwd_state: dict[str, Any]
    receipt_ref: str
    receipt_payload: dict[str, Any]
    decided_at_utc: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision_id": self.decision_id,
            "schema_version": self.schema_version,
            "project_id": self.project_id,
            "run_id": self.run_id,
            "session_id": self.session_id,
            "surface_id": self.surface_id,
            "surface": self.surface,
            "profile_id": self.profile_id,
            "command_fingerprint": self.command_fingerprint,
            "normalized_command": self.normalized_command,
            "verdict": self.verdict.value,
            "allowed": self.allowed,
            "human_approval_required": self.human_approval_required,
            "reasons": [r.value for r in self.reasons],
            "classifier": self.classifier.to_dict(),
            "approval_chain": self.approval_chain,
            "tool_surface": self.tool_surface,
            "cwd_state": self.cwd_state,
            "receipt_ref": self.receipt_ref,
            "receipt_payload": self.receipt_payload,
            "decided_at_utc": self.decided_at_utc,
        }

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"CommandSafetyDecision(decision_id={self.decision_id!r}, schema_version={self.schema_version!r}, project_id={self.project_id!r})"


def _clean_tuple(values: tuple[str, ...] | list[str] | Any) -> tuple[str, ...]:
    if values is None:
        return ()
    if isinstance(values, (str, bytes)):
        return (str(values).strip(),) if str(values).strip() else ()
    return tuple(str(value).strip() for value in values if str(value).strip())
