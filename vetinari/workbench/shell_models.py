"""Workbench shell records."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any, Literal

ShellStatus = Literal["ok", "empty", "degraded"]
RiskLevel = Literal["low", "medium", "high", "blocked"]


def _jsonify(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(key): _jsonify(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonify(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class ShellNavigationItem:
    """One first-class shell destination."""

    view: str
    label: str
    object_kind: str
    count: int
    active: bool
    why: str

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"ShellNavigationItem(view={self.view!r}, label={self.label!r}, object_kind={self.object_kind!r})"


@dataclass(frozen=True, slots=True)
class ShellCommand:
    """Keyboard-first command palette action with policy context."""

    command_id: str
    label: str
    view: str
    object_kind: str
    object_id: str | None
    shortcut: str
    enabled: bool
    requires_approval: bool
    why: str
    blocked_reason: str | None = None

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"ShellCommand(command_id={self.command_id!r}, label={self.label!r}, view={self.view!r})"


@dataclass(frozen=True, slots=True)
class ShellObjectSummary:
    """Compact Workbench object row shown in drawers and sidebars."""

    object_id: str
    object_kind: str
    title: str
    status: str
    view: str
    provenance_state: str
    risk_level: RiskLevel
    updated_at_utc: str
    why: str

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return (
            f"ShellObjectSummary(object_id={self.object_id!r}, object_kind={self.object_kind!r}, title={self.title!r})"
        )


@dataclass(frozen=True, slots=True)
class ShellQueueSummary:
    """Visible queue state for the desktop shell."""

    active_count: int
    queued_count: int
    blocked_count: int
    lane_pressure: str
    why: str

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"ShellQueueSummary(active_count={self.active_count!r}, queued_count={self.queued_count!r}, blocked_count={self.blocked_count!r})"


@dataclass(frozen=True, slots=True)
class ShellTimelineEvent:
    """One persistent timeline event."""

    event_id: str
    object_kind: str
    object_id: str
    label: str
    occurred_at_utc: str
    severity: str
    why: str

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"ShellTimelineEvent(event_id={self.event_id!r}, object_kind={self.object_kind!r}, object_id={self.object_id!r})"


@dataclass(frozen=True, slots=True)
class ShellSplitComparison:
    """Current split-compare state."""

    left_object_id: str | None
    right_object_id: str | None
    basis: str
    degraded: bool
    degraded_reason: str | None

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"ShellSplitComparison(left_object_id={self.left_object_id!r}, right_object_id={self.right_object_id!r}, basis={self.basis!r})"


@dataclass(frozen=True, slots=True)
class ShellRiskControl:
    """Fail-closed safety context for operator actions."""

    risk_level: RiskLevel
    cost_context: str
    provenance_context: str
    policy_context: str
    can_execute: bool
    approval_required: bool
    why: str
    missing: tuple[str, ...]

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"ShellRiskControl(risk_level={self.risk_level!r}, cost_context={self.cost_context!r}, provenance_context={self.provenance_context!r})"


@dataclass(frozen=True, slots=True)
class WorkbenchShellSnapshot:
    """API payload consumed by the Svelte desktop shell."""

    project_id: str
    generated_at_utc: str
    status: ShellStatus
    degraded: bool
    degraded_reason: str | None
    navigation: tuple[ShellNavigationItem, ...]
    commands: tuple[ShellCommand, ...]
    objects: tuple[ShellObjectSummary, ...]
    queue: ShellQueueSummary
    timeline: tuple[ShellTimelineEvent, ...]
    split_comparison: ShellSplitComparison
    risk_control: ShellRiskControl
    next_actions: tuple[ShellCommand, ...]

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable dictionary."""
        return _jsonify(asdict(self))

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"WorkbenchShellSnapshot(project_id={self.project_id!r}, generated_at_utc={self.generated_at_utc!r}, status={self.status!r})"
