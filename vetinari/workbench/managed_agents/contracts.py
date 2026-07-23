"""Typed contracts for the AM Workbench managed-agent workspace."""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

SCHEMA_VERSION = 1

BLOCKER_TEMPLATE_UNAVAILABLE = "template_unavailable"
BLOCKER_TOOL_NOT_ALLOWED = "tool_not_allowed"
BLOCKER_MEMORY_SCOPE_NOT_ALLOWED = "memory_scope_not_allowed"
BLOCKER_MEMORY_POLICY_RECEIPT_REQUIRED = "memory_policy_receipt_required"
BLOCKER_AGENT_PAUSED = "agent_paused"
BLOCKER_AGENT_RETIRED = "agent_retired"
BLOCKER_STATE_UNREADABLE = "managed_agent_state_unreadable"
BLOCKER_PROJECT_SCOPE_UNSAFE = "project_scope_unsafe"
BLOCKER_DEPENDENCY_UNAVAILABLE = "dependency_unavailable"
BLOCKER_COST_CEILING_EXCEEDED = "cost_ceiling_exceeded"


class ManagedAgentWorkspaceError(ValueError):
    """Raised when a managed-agent contract is structurally invalid."""


class ManagedAgentKind(str, Enum):
    """Managed-agent subtypes supported by the workspace."""

    TASK = "task"
    CHAT = "chat"
    WATCHER = "watcher"
    AUTOMATION = "automation"


class ManagedAgentState(str, Enum):
    """Lifecycle state for one user-managed agent."""

    ACTIVE = "active"
    PAUSED = "paused"
    RETIRED = "retired"


class ManagedAgentDecisionStatus(str, Enum):
    """Fail-closed decision status for workspace operations."""

    ACCEPTED = "accepted"
    BLOCKED = "blocked"
    DEGRADED = "degraded"
    RECOVERY_NEEDED = "recovery_needed"


class _ManagedAgentModel(BaseModel):
    """Frozen Pydantic base for managed-agent contracts."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class ManagedAgentDependencyRefs(_ManagedAgentModel):
    """Cross-pack dependency references composed by a managed agent."""

    template_id: str
    mailbox_channel: str
    sandbox_profile: str
    route_ledger_ref: str
    watcher_policy_ref: str
    automation_recipe_refs: tuple[str, ...]
    conversation_ref: str
    promotion_targets: tuple[str, ...]
    memory_policy_ref: str
    monitoring_signal_ref: str
    resource_lease_ref: str
    trace_eval_ref: str

    @model_validator(mode="after")
    def _validate(self) -> ManagedAgentDependencyRefs:
        for field_name in (
            "template_id",
            "mailbox_channel",
            "sandbox_profile",
            "route_ledger_ref",
            "watcher_policy_ref",
            "conversation_ref",
            "memory_policy_ref",
            "monitoring_signal_ref",
            "resource_lease_ref",
            "trace_eval_ref",
        ):
            _require_text(getattr(self, field_name), field_name)
        _require_string_tuple(self.automation_recipe_refs, "automation_recipe_refs", allow_empty=True)
        _require_string_tuple(self.promotion_targets, "promotion_targets")
        return self

    def to_dict(self) -> dict[str, Any]:
        """Execute the to dict operation.

        Returns:
            dict[str, Any] value produced by to_dict().
        """
        payload = self.model_dump(mode="python")
        payload["automation_recipe_refs"] = list(self.automation_recipe_refs)
        payload["promotion_targets"] = list(self.promotion_targets)
        return payload

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"ManagedAgentDependencyRefs(template_id={self.template_id!r}, mailbox_channel={self.mailbox_channel!r}, sandbox_profile={self.sandbox_profile!r})"


class ManagedAgentInstallRequest(_ManagedAgentModel):
    """User request to install one managed agent from a reusable template."""

    agent_id: str
    project_id: str
    template_id: str
    display_name: str
    purpose: str
    kind: ManagedAgentKind
    requested_tools: tuple[str, ...]
    memory_scope: tuple[str, ...]
    persona_ref: str = ""
    conversation_branch_ref: str = ""
    policy_receipt_ref: str = ""
    cost_ceiling_ref: str = "resource-governor:default-prosumer"
    created_by: str = "user"

    @model_validator(mode="after")
    def _validate(self) -> ManagedAgentInstallRequest:
        for field_name in (
            "agent_id",
            "project_id",
            "template_id",
            "display_name",
            "purpose",
            "cost_ceiling_ref",
            "created_by",
        ):
            _require_text(getattr(self, field_name), field_name)
        if not isinstance(self.kind, ManagedAgentKind):
            raise ManagedAgentWorkspaceError("kind must be ManagedAgentKind")
        _require_string_tuple(self.requested_tools, "requested_tools")
        _require_string_tuple(self.memory_scope, "memory_scope")
        return self

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"ManagedAgentInstallRequest(agent_id={self.agent_id!r}, project_id={self.project_id!r}, template_id={self.template_id!r})"


class ManagedAgentRunRequest(_ManagedAgentModel):
    """Request to start a run for an installed managed agent."""

    agent_id: str
    run_id: str
    workspace_path: str
    requested_tools: tuple[str, ...]
    receipt_refs: tuple[str, ...]
    input_payload_ref: str = "managed-agent:input"
    expected_output_ref: str = "managed-agent:output"

    @model_validator(mode="after")
    def _validate(self) -> ManagedAgentRunRequest:
        for field_name in ("agent_id", "run_id", "workspace_path", "input_payload_ref", "expected_output_ref"):
            _require_text(getattr(self, field_name), field_name)
        _require_string_tuple(self.requested_tools, "requested_tools")
        _require_string_tuple(self.receipt_refs, "receipt_refs", allow_empty=True)
        return self

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"ManagedAgentRunRequest(agent_id={self.agent_id!r}, run_id={self.run_id!r}, workspace_path={self.workspace_path!r})"


class ManagedAgentRecord(_ManagedAgentModel):
    """Persisted user-visible managed-agent record."""

    schema_version: int
    agent_id: str
    project_id: str
    template_id: str
    display_name: str
    purpose: str
    kind: ManagedAgentKind
    state: ManagedAgentState
    requested_tools: tuple[str, ...]
    permissions: tuple[str, ...]
    memory_scope: tuple[str, ...]
    persona_ref: str
    conversation_branch_ref: str
    policy_receipt_refs: tuple[str, ...]
    cost_ceiling_ref: str
    dependencies: ManagedAgentDependencyRefs
    intervention: dict[str, Any]
    created_by: str
    created_at_utc: str
    updated_at_utc: str
    run_ids: tuple[str, ...] = Field(default_factory=tuple)

    @model_validator(mode="after")
    def _validate(self) -> ManagedAgentRecord:
        if self.schema_version != SCHEMA_VERSION:
            raise ManagedAgentWorkspaceError(f"schema_version must be {SCHEMA_VERSION}")
        for field_name in (
            "agent_id",
            "project_id",
            "template_id",
            "display_name",
            "purpose",
            "persona_ref",
            "conversation_branch_ref",
            "cost_ceiling_ref",
            "created_by",
            "created_at_utc",
            "updated_at_utc",
        ):
            _require_text(getattr(self, field_name), field_name)
        if not isinstance(self.kind, ManagedAgentKind):
            raise ManagedAgentWorkspaceError("kind must be ManagedAgentKind")
        if not isinstance(self.state, ManagedAgentState):
            raise ManagedAgentWorkspaceError("state must be ManagedAgentState")
        _require_string_tuple(self.requested_tools, "requested_tools")
        _require_string_tuple(self.permissions, "permissions")
        _require_string_tuple(self.memory_scope, "memory_scope")
        _require_string_tuple(self.policy_receipt_refs, "policy_receipt_refs", allow_empty=True)
        _require_string_tuple(self.run_ids, "run_ids", allow_empty=True)
        if not isinstance(self.dependencies, ManagedAgentDependencyRefs):
            raise ManagedAgentWorkspaceError("dependencies must be ManagedAgentDependencyRefs")
        if not isinstance(self.intervention, dict):
            raise ManagedAgentWorkspaceError("intervention must be a dict")
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "agent_id": self.agent_id,
            "project_id": self.project_id,
            "template_id": self.template_id,
            "display_name": self.display_name,
            "purpose": self.purpose,
            "kind": self.kind.value,
            "state": self.state.value,
            "requested_tools": list(self.requested_tools),
            "permissions": list(self.permissions),
            "memory_scope": list(self.memory_scope),
            "persona_ref": self.persona_ref,
            "conversation_branch_ref": self.conversation_branch_ref,
            "policy_receipt_refs": list(self.policy_receipt_refs),
            "cost_ceiling_ref": self.cost_ceiling_ref,
            "dependencies": self.dependencies.to_dict(),
            "intervention": dict(self.intervention),
            "created_by": self.created_by,
            "created_at_utc": self.created_at_utc,
            "updated_at_utc": self.updated_at_utc,
            "run_ids": list(self.run_ids),
        }

    @classmethod
    def from_mapping(cls, payload: dict[str, Any]) -> ManagedAgentRecord:
        """Execute the from mapping operation.

        Returns:
            ManagedAgentRecord value produced by from_mapping().
        """
        deps = ManagedAgentDependencyRefs(
            template_id=str(payload["dependencies"]["template_id"]),
            mailbox_channel=str(payload["dependencies"]["mailbox_channel"]),
            sandbox_profile=str(payload["dependencies"]["sandbox_profile"]),
            route_ledger_ref=str(payload["dependencies"]["route_ledger_ref"]),
            watcher_policy_ref=str(payload["dependencies"]["watcher_policy_ref"]),
            automation_recipe_refs=tuple(
                str(item) for item in payload["dependencies"].get("automation_recipe_refs", ())
            ),
            conversation_ref=str(payload["dependencies"]["conversation_ref"]),
            promotion_targets=tuple(str(item) for item in payload["dependencies"]["promotion_targets"]),
            memory_policy_ref=str(payload["dependencies"]["memory_policy_ref"]),
            monitoring_signal_ref=str(payload["dependencies"]["monitoring_signal_ref"]),
            resource_lease_ref=str(payload["dependencies"]["resource_lease_ref"]),
            trace_eval_ref=str(payload["dependencies"]["trace_eval_ref"]),
        )
        return cls(
            schema_version=int(payload["schema_version"]),
            agent_id=str(payload["agent_id"]),
            project_id=str(payload["project_id"]),
            template_id=str(payload["template_id"]),
            display_name=str(payload["display_name"]),
            purpose=str(payload["purpose"]),
            kind=ManagedAgentKind(str(payload["kind"])),
            state=ManagedAgentState(str(payload["state"])),
            requested_tools=tuple(str(item) for item in payload["requested_tools"]),
            permissions=tuple(str(item) for item in payload["permissions"]),
            memory_scope=tuple(str(item) for item in payload["memory_scope"]),
            persona_ref=str(payload["persona_ref"]),
            conversation_branch_ref=str(payload["conversation_branch_ref"]),
            policy_receipt_refs=tuple(str(item) for item in payload.get("policy_receipt_refs", ())),
            cost_ceiling_ref=str(payload["cost_ceiling_ref"]),
            dependencies=deps,
            intervention=dict(payload["intervention"]),
            created_by=str(payload["created_by"]),
            created_at_utc=str(payload["created_at_utc"]),
            updated_at_utc=str(payload["updated_at_utc"]),
            run_ids=tuple(str(item) for item in payload.get("run_ids", ())),
        )

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"ManagedAgentRecord(schema_version={self.schema_version!r}, agent_id={self.agent_id!r}, project_id={self.project_id!r})"


class ManagedAgentDecision(_ManagedAgentModel):
    """Result returned for every workspace operation."""

    status: ManagedAgentDecisionStatus
    agent_id: str
    accepted: bool
    blockers: tuple[str, ...]
    operator_action: str
    record: ManagedAgentRecord | None = None
    run_id: str = ""
    evidence_refs: tuple[str, ...] = Field(default_factory=tuple)

    @model_validator(mode="after")
    def _validate(self) -> ManagedAgentDecision:
        if not isinstance(self.status, ManagedAgentDecisionStatus):
            raise ManagedAgentWorkspaceError("status must be ManagedAgentDecisionStatus")
        _require_string_tuple(self.blockers, "blockers", allow_empty=True)
        _require_string_tuple(self.evidence_refs, "evidence_refs", allow_empty=True)
        _require_text(self.operator_action, "operator_action")
        if self.accepted and self.blockers:
            raise ManagedAgentWorkspaceError("accepted decision cannot include blockers")
        if not self.accepted and self.status is ManagedAgentDecisionStatus.ACCEPTED:
            raise ManagedAgentWorkspaceError("accepted status requires accepted=True")
        if self.record is not None and not isinstance(self.record, ManagedAgentRecord):
            raise ManagedAgentWorkspaceError("record must be ManagedAgentRecord or None")
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "agent_id": self.agent_id,
            "accepted": self.accepted,
            "blockers": list(self.blockers),
            "operator_action": self.operator_action,
            "record": self.record.to_dict() if self.record else None,
            "run_id": self.run_id,
            "evidence_refs": list(self.evidence_refs),
        }

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"ManagedAgentDecision(status={self.status!r}, agent_id={self.agent_id!r}, accepted={self.accepted!r})"


def _require_text(value: object, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ManagedAgentWorkspaceError(f"{field_name} must be non-empty")


def _require_string_tuple(values: tuple[str, ...], field_name: str, *, allow_empty: bool = False) -> None:
    if not isinstance(values, tuple) or (not values and not allow_empty):
        raise ManagedAgentWorkspaceError(f"{field_name} must be a non-empty tuple")
    if any(not isinstance(value, str) or not value.strip() for value in values):
        raise ManagedAgentWorkspaceError(f"{field_name} must contain non-empty strings")


__all__ = [
    "BLOCKER_AGENT_PAUSED",
    "BLOCKER_AGENT_RETIRED",
    "BLOCKER_DEPENDENCY_UNAVAILABLE",
    "BLOCKER_MEMORY_POLICY_RECEIPT_REQUIRED",
    "BLOCKER_MEMORY_SCOPE_NOT_ALLOWED",
    "BLOCKER_PROJECT_SCOPE_UNSAFE",
    "BLOCKER_STATE_UNREADABLE",
    "BLOCKER_TEMPLATE_UNAVAILABLE",
    "BLOCKER_TOOL_NOT_ALLOWED",
    "SCHEMA_VERSION",
    "ManagedAgentDecision",
    "ManagedAgentDecisionStatus",
    "ManagedAgentDependencyRefs",
    "ManagedAgentInstallRequest",
    "ManagedAgentKind",
    "ManagedAgentRecord",
    "ManagedAgentRunRequest",
    "ManagedAgentState",
    "ManagedAgentWorkspaceError",
]
