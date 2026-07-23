"""Contracts for Workbench workflow-builder ergonomics."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from vetinari.api.responses import json_safe as _json_safe


class WorkflowBuilderError(ValueError):
    """Raised when a workflow draft cannot be trusted."""

    def __init__(self, reason: str, message: str | None = None) -> None:
        super().__init__(f"{reason}: {message}" if message else reason)
        self.reason = reason


class WorkbenchWorkflowStepKind(str, Enum):
    """Runtime contract for WorkbenchWorkflowStepKind."""

    PROMPT = "prompt"
    TOOL = "tool"
    APPROVAL = "approval"
    CHANNEL_DELIVERY = "channel_delivery"
    ASSET_REPLAY = "asset_replay"
    WAIT = "wait"


class WorkflowSafetyMode(str, Enum):
    """Runtime contract for WorkflowSafetyMode."""

    SIMULATION_ONLY = "simulation_only"
    APPROVAL_REQUIRED = "approval_required"
    READ_ONLY_CONSOLE = "read_only_console"


@dataclass(frozen=True, slots=True)
class WorkbenchWorkflowStep:
    """Runtime contract for WorkbenchWorkflowStep."""

    step_id: str
    kind: WorkbenchWorkflowStepKind | str
    label: str
    config: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_id(self.step_id, "step_id")
        object.__setattr__(self, "kind", _coerce_enum(WorkbenchWorkflowStepKind, self.kind, "step-kind-unknown"))
        _require_text(self.label, "label")
        if not isinstance(self.config, dict):
            raise WorkflowBuilderError("step-config-invalid", self.step_id)

    def to_dict(self) -> dict[str, Any]:
        return _json_safe(self)

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"WorkbenchWorkflowStep(step_id={self.step_id!r}, kind={self.kind!r}, label={self.label!r})"


@dataclass(frozen=True, slots=True)
class WorkflowEdge:
    """Runtime contract for WorkflowEdge."""

    source: str
    target: str

    def __post_init__(self) -> None:
        _require_id(self.source, "source")
        _require_id(self.target, "target")
        if self.source == self.target:
            raise WorkflowBuilderError("edge-self-loop", self.source)

    def to_dict(self) -> dict[str, Any]:
        return _json_safe(self)


@dataclass(frozen=True, slots=True)
class WorkflowGraph:
    """Runtime contract for WorkflowGraph."""

    graph_id: str
    name: str
    steps: tuple[WorkbenchWorkflowStep, ...]
    edges: tuple[WorkflowEdge, ...] = ()
    safety_mode: WorkflowSafetyMode | str = WorkflowSafetyMode.SIMULATION_ONLY
    metadata: dict[str, Any] = field(default_factory=dict)
    schema_version: int = 1

    def __post_init__(self) -> None:
        _require_id(self.graph_id, "graph_id")
        _require_text(self.name, "name")
        object.__setattr__(
            self, "safety_mode", _coerce_enum(WorkflowSafetyMode, self.safety_mode, "safety-mode-unknown")
        )
        if self.schema_version != 1:
            raise WorkflowBuilderError("schema-version-unsupported", str(self.schema_version))
        if not self.steps:
            raise WorkflowBuilderError("workflow-steps-missing")
        if len({step.step_id for step in self.steps}) != len(self.steps):
            raise WorkflowBuilderError("workflow-step-duplicate")
        if not isinstance(self.metadata, dict):
            raise WorkflowBuilderError("workflow-metadata-invalid")

    def to_dict(self) -> dict[str, Any]:
        return _json_safe(self)

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"WorkflowGraph(graph_id={self.graph_id!r}, name={self.name!r}, steps={self.steps!r})"


@dataclass(frozen=True, slots=True)
class WorkflowValidationResult:
    """Runtime contract for WorkflowValidationResult."""

    passed: bool
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    reachable_steps: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return _json_safe(self)

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"WorkflowValidationResult(passed={self.passed!r}, errors={self.errors!r}, warnings={self.warnings!r})"


@dataclass(frozen=True, slots=True)
class WorkflowPreview:
    """Runtime contract for WorkflowPreview."""

    graph_id: str
    ordered_steps: tuple[dict[str, Any], ...]
    approval_points: tuple[dict[str, Any], ...]
    channel_deliveries: tuple[dict[str, Any], ...]
    runtime_mode: WorkflowSafetyMode | str
    executable: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "runtime_mode", _coerce_enum(WorkflowSafetyMode, self.runtime_mode, "safety-mode-unknown")
        )
        if self.executable:
            raise WorkflowBuilderError("preview-must-not-execute", self.graph_id)

    def to_dict(self) -> dict[str, Any]:
        return _json_safe(self)

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"WorkflowPreview(graph_id={self.graph_id!r}, ordered_steps={self.ordered_steps!r}, approval_points={self.approval_points!r})"


@dataclass(frozen=True, slots=True)
class WorkflowRuntimeSettings:
    """Runtime contract for WorkflowRuntimeSettings."""

    project_id: str
    max_parallel_steps: int
    safety_mode: WorkflowSafetyMode | str
    channel_preview_only: bool = True
    persistent_threads: bool = False

    def __post_init__(self) -> None:
        _require_id(self.project_id, "project_id")
        if self.max_parallel_steps < 1:
            raise WorkflowBuilderError("parallelism-invalid")
        object.__setattr__(
            self, "safety_mode", _coerce_enum(WorkflowSafetyMode, self.safety_mode, "safety-mode-unknown")
        )
        if not self.channel_preview_only:
            raise WorkflowBuilderError("channel-preview-only-required")

    def to_dict(self) -> dict[str, Any]:
        return _json_safe(self)

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"WorkflowRuntimeSettings(project_id={self.project_id!r}, max_parallel_steps={self.max_parallel_steps!r}, safety_mode={self.safety_mode!r})"


@dataclass(frozen=True, slots=True)
class WorkflowConsoleSnapshot:
    """Runtime contract for WorkflowConsoleSnapshot."""

    project_id: str
    saved_graph_count: int
    active_graph_id: str | None
    runtime_settings: WorkflowRuntimeSettings
    recent_events: tuple[dict[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return _json_safe(self)

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"WorkflowConsoleSnapshot(project_id={self.project_id!r}, saved_graph_count={self.saved_graph_count!r}, active_graph_id={self.active_graph_id!r})"


def workflow_graph_from_dict(raw: dict[str, Any]) -> WorkflowGraph:
    """Build a workflow graph from an explicit versioned payload.

    Args:
        raw: Raw graph payload received from persistence or an API boundary.

    Returns:
        Validated workflow graph.

    Raises:
        WorkflowBuilderError: If the schema version is missing, malformed, or
            the graph contract rejects any field.
    """
    if "schema_version" not in raw:
        raise WorkflowBuilderError("schema-version-missing")
    try:
        schema_version = int(raw["schema_version"])
    except (TypeError, ValueError) as exc:
        raise WorkflowBuilderError("schema-version-invalid", str(raw.get("schema_version"))) from exc
    migrated = _migrate_workflow_graph_payload(raw, schema_version)
    return WorkflowGraph(
        graph_id=str(migrated["graph_id"]),
        name=str(migrated["name"]),
        steps=tuple(WorkbenchWorkflowStep(**item) for item in migrated["steps"]),
        edges=tuple(WorkflowEdge(**item) for item in migrated.get("edges", ())),
        safety_mode=str(migrated.get("safety_mode", WorkflowSafetyMode.SIMULATION_ONLY.value)),
        metadata=dict(migrated.get("metadata", {})),
        schema_version=int(migrated["schema_version"]),
    )


def _migrate_workflow_graph_payload(raw: dict[str, Any], schema_version: int) -> dict[str, Any]:
    if schema_version == 1:
        return dict(raw)
    if schema_version != 2:
        raise WorkflowBuilderError("schema-version-unsupported", str(schema_version))

    migrated = dict(raw)
    if "steps" not in migrated and "nodes" in migrated:
        migrated["steps"] = migrated.pop("nodes")
    if "edges" not in migrated and "connections" in migrated:
        migrated["edges"] = migrated.pop("connections")
    runtime = migrated.pop("runtime", None)
    if isinstance(runtime, dict) and "safety_mode" not in migrated:
        migrated["safety_mode"] = runtime.get("safety_mode", WorkflowSafetyMode.SIMULATION_ONLY.value)
    metadata = dict(migrated.get("metadata", {}))
    metadata.setdefault("migrated_from_schema_version", schema_version)
    migrated["metadata"] = metadata
    migrated["schema_version"] = 1
    return migrated


def runtime_settings_from_dict(raw: dict[str, Any]) -> WorkflowRuntimeSettings:
    """Build fail-closed runtime settings from a persisted or API payload.

    Returns:
        WorkflowRuntimeSettings value produced by runtime_settings_from_dict().

    Raises:
        WorkflowBuilderError: If required fields are missing or invalid.
    """
    if "project_id" not in raw:
        raise WorkflowBuilderError("runtime-project-id-missing")
    if "max_parallel_steps" not in raw:
        raise WorkflowBuilderError("runtime-parallelism-missing")
    try:
        max_parallel_steps = int(raw["max_parallel_steps"])
    except (TypeError, ValueError) as exc:
        raise WorkflowBuilderError("runtime-parallelism-invalid", str(raw.get("max_parallel_steps"))) from exc
    return WorkflowRuntimeSettings(
        project_id=str(raw["project_id"]),
        max_parallel_steps=max_parallel_steps,
        safety_mode=str(raw.get("safety_mode", WorkflowSafetyMode.SIMULATION_ONLY.value)),
        channel_preview_only=bool(raw.get("channel_preview_only", True)),
        persistent_threads=bool(raw.get("persistent_threads")),
    )


def _coerce_enum(enum_type: type[Enum], value: Enum | str, reason: str) -> Enum:
    if isinstance(value, enum_type):
        return value
    raw_value = value.value if isinstance(value, Enum) else value
    try:
        return enum_type(str(raw_value))
    except ValueError as exc:
        raise WorkflowBuilderError(reason, str(value)) from exc


def _require_text(value: object, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise WorkflowBuilderError("text-required", field_name)


def _require_id(value: object, field_name: str) -> None:
    _require_text(value, field_name)
    text = str(value)
    if "/" in text or "\\" in text or ".." in text:
        raise WorkflowBuilderError("id-invalid", field_name)


__all__ = [
    "WorkbenchWorkflowStep",
    "WorkbenchWorkflowStepKind",
    "WorkflowBuilderError",
    "WorkflowConsoleSnapshot",
    "WorkflowEdge",
    "WorkflowGraph",
    "WorkflowPreview",
    "WorkflowRuntimeSettings",
    "WorkflowSafetyMode",
    "WorkflowValidationResult",
    "runtime_settings_from_dict",
    "workflow_graph_from_dict",
]
