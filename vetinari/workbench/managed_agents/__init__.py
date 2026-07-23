"""Managed-agent workspace public contract surface."""

from __future__ import annotations

from vetinari.workbench.managed_agents.context_modes import (
    ManagedAgentContextDecision,
    ManagedAgentContextMode,
    ManagedAgentContextRequest,
    evaluate_managed_agent_context,
)
from vetinari.workbench.managed_agents.contracts import (
    BLOCKER_AGENT_PAUSED,
    BLOCKER_AGENT_RETIRED,
    BLOCKER_COST_CEILING_EXCEEDED,
    BLOCKER_DEPENDENCY_UNAVAILABLE,
    BLOCKER_MEMORY_POLICY_RECEIPT_REQUIRED,
    BLOCKER_MEMORY_SCOPE_NOT_ALLOWED,
    BLOCKER_PROJECT_SCOPE_UNSAFE,
    BLOCKER_STATE_UNREADABLE,
    BLOCKER_TEMPLATE_UNAVAILABLE,
    BLOCKER_TOOL_NOT_ALLOWED,
    ManagedAgentDecision,
    ManagedAgentDecisionStatus,
    ManagedAgentDependencyRefs,
    ManagedAgentInstallRequest,
    ManagedAgentKind,
    ManagedAgentRecord,
    ManagedAgentRunRequest,
    ManagedAgentState,
    ManagedAgentWorkspaceError,
)
from vetinari.workbench.managed_agents.runtime import (
    DEFAULT_MANAGED_AGENT_CONFIG_PATH,
    DEFAULT_MANAGED_AGENT_STATE_PATH,
    ManagedAgentWorkspaceRuntime,
)

__all__ = [
    "BLOCKER_AGENT_PAUSED",
    "BLOCKER_AGENT_RETIRED",
    "BLOCKER_COST_CEILING_EXCEEDED",
    "BLOCKER_DEPENDENCY_UNAVAILABLE",
    "BLOCKER_MEMORY_POLICY_RECEIPT_REQUIRED",
    "BLOCKER_MEMORY_SCOPE_NOT_ALLOWED",
    "BLOCKER_PROJECT_SCOPE_UNSAFE",
    "BLOCKER_STATE_UNREADABLE",
    "BLOCKER_TEMPLATE_UNAVAILABLE",
    "BLOCKER_TOOL_NOT_ALLOWED",
    "DEFAULT_MANAGED_AGENT_CONFIG_PATH",
    "DEFAULT_MANAGED_AGENT_STATE_PATH",
    "ManagedAgentContextDecision",
    "ManagedAgentContextMode",
    "ManagedAgentContextRequest",
    "ManagedAgentDecision",
    "ManagedAgentDecisionStatus",
    "ManagedAgentDependencyRefs",
    "ManagedAgentInstallRequest",
    "ManagedAgentKind",
    "ManagedAgentRecord",
    "ManagedAgentRunRequest",
    "ManagedAgentState",
    "ManagedAgentWorkspaceError",
    "ManagedAgentWorkspaceRuntime",
    "evaluate_managed_agent_context",
]
