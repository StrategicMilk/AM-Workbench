"""JSON-safe Workbench status health contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal


class WorkbenchHealthState(StrEnum):
    """Distinct health states surfaced by the Workbench status console."""

    CONFIGURED = "configured"
    DEGRADED = "degraded"
    BROKEN = "broken"
    BUSY = "busy"
    STALE = "stale"
    APPROVAL_REQUIRED = "approval_required"


class WorkbenchHealthDomain(StrEnum):
    """Status domains that the health console must represent."""

    PROVIDERS = "providers"
    MODELS = "models"
    CHANNELS = "channels"
    MEMORY = "memory"
    TOOLS = "tools"
    MCP = "mcp"
    AGENT_SAFETY = "agent_safety"
    CAPABILITY_PACKS = "capability_packs"
    SCHEDULER_RESOURCES = "scheduler_resources"
    ACTIVE_RUNS = "active_runs"
    QUEUES = "queues"
    LOGS_ERRORS = "logs_errors"
    UPDATES = "updates"
    SETTINGS = "settings"
    SUPPORT_BUNDLE = "support_bundle"


class WorkbenchStatusSeverity(StrEnum):
    """Operator-facing severity for one health result."""

    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    BLOCKING = "blocking"


@dataclass(frozen=True, slots=True)
class WorkbenchStatusProbeResult:
    """Result returned by status probe callables."""

    status: Literal["ok", "degraded", "critical"]
    message: str
    value: float | None = None


ProbeResult = WorkbenchStatusProbeResult


@dataclass(frozen=True, slots=True)
class WorkbenchHealthResult:
    """One status row for a Workbench health domain."""

    domain: WorkbenchHealthDomain
    key: str
    state: WorkbenchHealthState
    severity: WorkbenchStatusSeverity
    summary: str
    evidence_refs: tuple[str, ...]
    checked_at_utc: str
    settings_target: str | None = None
    fix_action: str | None = None
    assistant_visible: bool = True
    ui_visible: bool = True
    stale_after_utc: str | None = None
    stale_reason: str | None = None
    informational: bool = False

    def __post_init__(self) -> None:
        if not self.key.strip():
            raise ValueError("health result key is required")
        if not self.summary.strip():
            raise ValueError("health result summary is required")
        if not self.evidence_refs:
            raise ValueError("health result evidence_refs must be non-empty")
        if not self.informational and not (self.settings_target or self.fix_action):
            raise ValueError("actionable health results require settings_target or fix_action")
        if self.state is WorkbenchHealthState.STALE and not (self.stale_after_utc or self.stale_reason):
            raise ValueError("stale health results require stale_after_utc or stale_reason")

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe result payload."""
        return {
            "domain": self.domain.value,
            "key": self.key,
            "state": self.state.value,
            "severity": self.severity.value,
            "summary": self.summary,
            "evidence_refs": list(self.evidence_refs),
            "checked_at_utc": self.checked_at_utc,
            "settings_target": self.settings_target,
            "fix_action": self.fix_action,
            "assistant_visible": self.assistant_visible,
            "ui_visible": self.ui_visible,
            "stale_after_utc": self.stale_after_utc,
            "stale_reason": self.stale_reason,
            "informational": self.informational,
        }

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"WorkbenchHealthResult(domain={self.domain!r}, key={self.key!r}, state={self.state!r})"


@dataclass(frozen=True, slots=True)
class WorkbenchStatusConfig:
    """Static status configuration loaded from YAML."""

    schema_version: str
    required_domains: tuple[WorkbenchHealthDomain, ...]
    settings_targets: dict[WorkbenchHealthDomain, str]
    fix_actions: dict[WorkbenchHealthDomain, str]

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe config summary without environment secrets."""
        return {
            "schema_version": self.schema_version,
            "required_domains": [domain.value for domain in self.required_domains],
            "settings_targets": {domain.value: target for domain, target in self.settings_targets.items()},
            "fix_actions": {domain.value: action for domain, action in self.fix_actions.items()},
        }

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"WorkbenchStatusConfig(schema_version={self.schema_version!r}, required_domains={self.required_domains!r}, settings_targets={self.settings_targets!r})"


@dataclass(frozen=True, slots=True)
class WorkbenchStatusSnapshot:
    """Aggregate Workbench status snapshot for UI and assistant read paths."""

    project_id: str
    overall_state: WorkbenchHealthState
    generated_at_utc: str
    results: tuple[WorkbenchHealthResult, ...]
    state_counts: dict[WorkbenchHealthState, int] = field(default_factory=dict)
    config: WorkbenchStatusConfig | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a schema-ready JSON-safe snapshot."""
        return {
            "project_id": self.project_id,
            "overall_state": self.overall_state.value,
            "generated_at_utc": self.generated_at_utc,
            "state_counts": {state.value: int(self.state_counts.get(state, 0)) for state in WorkbenchHealthState},
            "results": [result.to_dict() for result in self.results],
            "config": self.config.to_dict() if self.config else None,
        }

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"WorkbenchStatusSnapshot(project_id={self.project_id!r}, overall_state={self.overall_state!r}, generated_at_utc={self.generated_at_utc!r})"


__all__ = [
    "ProbeResult",
    "WorkbenchHealthDomain",
    "WorkbenchHealthResult",
    "WorkbenchHealthState",
    "WorkbenchStatusConfig",
    "WorkbenchStatusSeverity",
    "WorkbenchStatusSnapshot",
]
