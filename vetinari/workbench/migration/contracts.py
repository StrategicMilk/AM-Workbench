"""Typed contracts for safe Workbench migration imports."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any


class MigrationSourceKind(str, Enum):
    """Runtime contract for MigrationSourceKind."""

    WORKBENCH_EXPORT = "workbench_export"
    CODEX_CONFIG = "codex_config"
    CLAUDE_CONFIG = "claude_config"
    PROVIDER_MODEL_SETTING = "provider_model_setting"
    SKILL = "skill"
    MEMORY = "memory"
    AUTOMATION = "automation"
    TOOL_SETTING = "tool_setting"
    WORKSPACE_PACK = "workspace_pack"
    EXTERNAL_ASSISTANT_DATA = "external_assistant_data"


class MigrationRisk(str, Enum):
    """Runtime contract for MigrationRisk."""

    LOW = "low"
    CONFLICT = "conflict"
    RISKY_TOOL = "risky_tool"
    SENSITIVE_CREDENTIAL = "secret"
    UNAVAILABLE = "unavailable"
    CORRUPT = "corrupt"


class MigrationApplyStatus(str, Enum):
    """Runtime contract for MigrationApplyStatus."""

    APPLIED = "applied"
    BLOCKED = "blocked"
    IDEMPOTENT = "idempotent"


class MigrationBlockReason(str, Enum):
    """Runtime contract for MigrationBlockReason."""

    CREDENTIAL_SELECTION_REQUIRED = "secret_selection_required"
    RISKY_TOOL_EXPLICIT_SELECTION_REQUIRED = "risky_tool_explicit_selection_required"
    CONFLICT_SELECTION_REQUIRED = "conflict_selection_required"
    BACKUP_CONFIRMATION_REQUIRED = "backup_confirmation_required"
    STALE_PLAN = "stale_plan"
    UNKNOWN_ITEM = "unknown_item"
    ALREADY_APPLIED = "already_applied"
    CORRUPT_STATE = "corrupt_state"
    UNAVAILABLE_SOURCE = "unavailable_source"


@dataclass(frozen=True, slots=True)
class MigrationSourceSpec:
    """Runtime contract for MigrationSourceSpec."""

    source_id: str
    label: str
    kind: MigrationSourceKind
    paths: tuple[str, ...]
    risky_tool: bool = False

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"MigrationSourceSpec(source_id={self.source_id!r}, label={self.label!r}, kind={self.kind!r})"


@dataclass(frozen=True, slots=True)
class MigrationFinding:
    """Runtime contract for MigrationFinding."""

    item_id: str
    source_id: str
    label: str
    kind: MigrationSourceKind
    path: str
    destination_path: str
    risk: MigrationRisk
    default_selected: bool
    blocked_reason: MigrationBlockReason | None
    conflict_key: str | None
    redacted_preview: str

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"MigrationFinding(item_id={self.item_id!r}, source_id={self.source_id!r}, label={self.label!r})"


@dataclass(frozen=True, slots=True)
class MigrationConflict:
    """Runtime contract for MigrationConflict."""

    conflict_key: str
    destination_path: str
    candidate_item_ids: tuple[str, ...]
    reason: str

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"MigrationConflict(conflict_key={self.conflict_key!r}, destination_path={self.destination_path!r}, candidate_item_ids={self.candidate_item_ids!r})"


@dataclass(frozen=True, slots=True)
class MigrationPlan:
    """Runtime contract for MigrationPlan."""

    proposal_id: str
    dry_run: bool
    findings: tuple[MigrationFinding, ...]
    conflicts: tuple[MigrationConflict, ...]
    blocked_reasons: tuple[MigrationBlockReason, ...]
    writes_planned: bool
    generated_at_utc: str

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"MigrationPlan(proposal_id={self.proposal_id!r}, dry_run={self.dry_run!r}, findings={self.findings!r})"


@dataclass(frozen=True, slots=True)
class MigrationApplyRequest:
    """Runtime contract for MigrationApplyRequest."""

    proposal_id: str
    selected_item_ids: tuple[str, ...] = ()
    include_secret_item_ids: tuple[str, ...] = ()
    conflict_selections: dict[str, str] | None = None

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"MigrationApplyRequest(proposal_id={self.proposal_id!r}, selected_item_ids={self.selected_item_ids!r}, include_secret_item_ids={self.include_secret_item_ids!r})"


@dataclass(frozen=True, slots=True)
class MigrationApplyResult:
    """Runtime contract for MigrationApplyResult."""

    status: MigrationApplyStatus
    proposal_id: str
    applied_item_ids: tuple[str, ...]
    blocked_reasons: tuple[MigrationBlockReason, ...]
    backup_path: str | None
    report_path: str | None
    idempotency_key: str

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"MigrationApplyResult(status={self.status!r}, proposal_id={self.proposal_id!r}, applied_item_ids={self.applied_item_ids!r})"


def migration_json_safe(value: Any) -> Any:
    """Convert migration dataclasses and enums into JSON-safe primitives.

    Returns:
        Any value produced by migration_json_safe().
    """
    if isinstance(value, Enum):
        return value.value
    if hasattr(value, "__dataclass_fields__") and not isinstance(value, type):
        return {key: migration_json_safe(item) for key, item in asdict(value).items()}
    if isinstance(value, dict):
        return {str(key): migration_json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [migration_json_safe(item) for item in value]
    return value


__all__ = [
    "MigrationApplyRequest",
    "MigrationApplyResult",
    "MigrationApplyStatus",
    "MigrationBlockReason",
    "MigrationConflict",
    "MigrationFinding",
    "MigrationPlan",
    "MigrationRisk",
    "MigrationSourceKind",
    "MigrationSourceSpec",
    "migration_json_safe",
]
