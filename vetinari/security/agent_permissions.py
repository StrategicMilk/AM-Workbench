"""Agent Permission System — action-level authorization for agent operations.

Defines the canonical set of actions agents may attempt, maps agent types
to permission policies, and provides dual-permission checking (both the
requesting agent and its target must authorize an action).

Permission checks MUST fail closed: any exception during evaluation results
in DENIED, never ALLOWED.  This follows the fail-closed security principle.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from vetinari.types import AgentType

logger = logging.getLogger(__name__)


class AgentAction(Enum):
    """All actions an agent may attempt within the Vetinari system.

    Each action represents a distinct authorization boundary.  New actions
    must be added here before any code checks for them.
    """

    # File system operations
    READ_FILE = "read_file"
    WRITE_FILE = "write_file"
    DELETE_FILE = "delete_file"
    CREATE_DIR = "create_dir"

    # Code execution
    EXECUTE_CODE = "execute_code"
    EXECUTE_SHELL = "execute_shell"
    INSTALL_PACKAGE = "install_package"

    # Agent delegation
    DELEGATE_TO_AGENT = "delegate_to_agent"
    SPAWN_SUBAGENT = "spawn_subagent"
    CANCEL_TASK = "cancel_task"

    # Network / external
    WEB_SEARCH = "web_search"
    HTTP_REQUEST = "http_request"
    SEND_NOTIFICATION = "send_notification"

    # Memory / storage
    WRITE_MEMORY = "write_memory"
    READ_MEMORY = "read_memory"

    # Plan management
    MODIFY_PLAN = "modify_plan"
    APPROVE_PLAN = "approve_plan"
    REJECT_PLAN = "reject_plan"


@dataclass(frozen=True, slots=True)
class AgentPermissionPolicy:
    """Immutable permission policy for a single agent type.

    Args:
        agent_type: The AgentType this policy applies to.
        allowed_actions: Frozenset of actions this agent may perform.
        denied_actions: Frozenset of actions explicitly denied (overrides allowed).
        description: Human-readable summary of the policy's intent.
    """

    agent_type: AgentType
    allowed_actions: frozenset[AgentAction]
    denied_actions: frozenset[AgentAction] = field(default_factory=frozenset)
    description: str = ""

    def __repr__(self) -> str:
        return (
            f"PermissionPolicy(agent_type={self.agent_type.value!r}, "
            f"allowed={len(self.allowed_actions)!r}, "
            f"denied={len(self.denied_actions)!r})"
        )

    def allows(self, action: AgentAction) -> bool:
        """Check whether this policy permits *action*.

        Explicit deny overrides explicit allow.

        Args:
            action: The action to check.

        Returns:
            True if the action is permitted under this policy.
        """
        if action in self.denied_actions:
            return False
        return action in self.allowed_actions


# -- Default policies per agent type ----------------------------------------
# FOREMAN: orchestration only — no direct code execution or file writes
# WORKER: full execution rights (sandboxed via CodeSandbox)
# INSPECTOR: read-only + memory write for quality reports

AGENT_PERMISSION_POLICIES: dict[AgentType, AgentPermissionPolicy] = {
    AgentType.FOREMAN: AgentPermissionPolicy(
        agent_type=AgentType.FOREMAN,
        allowed_actions=frozenset({
            AgentAction.READ_FILE,
            AgentAction.READ_MEMORY,
            AgentAction.WRITE_MEMORY,
            AgentAction.DELEGATE_TO_AGENT,
            AgentAction.SPAWN_SUBAGENT,
            AgentAction.CANCEL_TASK,
            AgentAction.MODIFY_PLAN,
            AgentAction.APPROVE_PLAN,
            AgentAction.REJECT_PLAN,
            AgentAction.WEB_SEARCH,
        }),
        denied_actions=frozenset({
            AgentAction.EXECUTE_SHELL,
            AgentAction.DELETE_FILE,
            AgentAction.INSTALL_PACKAGE,
        }),
        description="Orchestration-only: plans, delegates, reads — no direct execution",
    ),
    AgentType.WORKER: AgentPermissionPolicy(
        agent_type=AgentType.WORKER,
        allowed_actions=frozenset(AgentAction),  # all actions
        denied_actions=frozenset({
            AgentAction.APPROVE_PLAN,
            AgentAction.REJECT_PLAN,
            AgentAction.CANCEL_TASK,
        }),
        description="Full execution rights — sandboxed environment enforces isolation",
    ),
    AgentType.INSPECTOR: AgentPermissionPolicy(
        agent_type=AgentType.INSPECTOR,
        allowed_actions=frozenset({
            AgentAction.READ_FILE,
            AgentAction.READ_MEMORY,
            AgentAction.WRITE_MEMORY,
            AgentAction.WEB_SEARCH,
            AgentAction.EXECUTE_CODE,  # read-only test runs
            AgentAction.APPROVE_PLAN,
            AgentAction.REJECT_PLAN,
        }),
        denied_actions=frozenset({
            AgentAction.WRITE_FILE,
            AgentAction.DELETE_FILE,
            AgentAction.EXECUTE_SHELL,
            AgentAction.INSTALL_PACKAGE,
            AgentAction.SPAWN_SUBAGENT,
            AgentAction.DELEGATE_TO_AGENT,
            AgentAction.MODIFY_PLAN,
        }),
        description="Read-only auditor: inspects, reports, approves/rejects, and runs read-only code checks — no file modification or shell execution",
    ),
}


# TODO(#5508): FSA-5508 real wiring in vetinari/agents/consolidated/worker_agent.py Task 1.4
class AgentPermissions:
    """Authorization checker for agent actions.

    Wraps the policy table and provides helper methods for checking
    whether an agent may perform an action, optionally against a second
    agent (for delegation checks).

    Checks fail CLOSED: any exception returns False (denied) so that
    broken authorization logic never inadvertently grants access.
    """

    def __init__(self, policies: dict[AgentType, AgentPermissionPolicy] | None = None) -> None:
        """Initialise with an optional custom policy set.

        Args:
            policies: Custom policy dict.  Defaults to AGENT_PERMISSION_POLICIES.
        """
        self._policies = policies or AGENT_PERMISSION_POLICIES

    def is_allowed(self, agent_type: AgentType, action: AgentAction) -> bool:
        """Check whether *agent_type* may perform *action*.

        Args:
            agent_type: The agent requesting authorization.
            action: The action being requested.

        Returns:
            True if the action is permitted; False if denied or policy missing.
        """
        try:
            policy = self._policies.get(agent_type)
            if policy is None:
                logger.warning(
                    "[AgentPermissions] No policy for agent_type=%s — denying %s",
                    agent_type.value,
                    action.value,
                )
                return False
            result = policy.allows(action)
            if not result:
                logger.debug("[AgentPermissions] DENIED %s -> %s", agent_type.value, action.value)
            return result
        except Exception as exc:
            # Fail closed — any exception is treated as denial
            logger.error(
                "[AgentPermissions] Exception during permission check (fail closed): %s",
                exc,
                exc_info=True,
            )
            return False

    def check_dual_permission(
        self,
        requesting_agent: AgentType,
        target_agent: AgentType,
        action: AgentAction,
    ) -> bool:
        """Check that BOTH the requesting and target agents permit *action*.

        AND semantics: both policies must allow the action.  Used for
        delegation (e.g. Foreman spawning a Worker sub-task must have both
        the Foreman's DELEGATE_TO_AGENT and Worker's SPAWN_SUBAGENT allowed).

        Args:
            requesting_agent: The agent initiating the action.
            target_agent: The agent being acted upon or delegated to.
            action: The action being checked.

        Returns:
            True only if both agents' policies permit the action.
        """
        try:
            requesting_ok = self.is_allowed(requesting_agent, action)
            target_ok = self.is_allowed(target_agent, action)
            result = requesting_ok and target_ok
            if not result:
                logger.debug(
                    "[AgentPermissions] Dual-permission DENIED: %s->%s action=%s (requesting=%s, target=%s)",
                    requesting_agent.value,
                    target_agent.value,
                    action.value,
                    requesting_ok,
                    target_ok,
                )
            return result
        except Exception as exc:
            logger.error(
                "[AgentPermissions] Exception in dual permission check (fail closed): %s",
                exc,
                exc_info=True,
            )
            return False

    def get_allowed_actions(self, agent_type: AgentType) -> frozenset[AgentAction]:
        """Return the full set of allowed actions for *agent_type*.

        Args:
            agent_type: The agent type to query.

        Returns:
            Frozenset of permitted AgentAction values, or empty frozenset
            if no policy exists.
        """
        try:
            policy = self._policies.get(agent_type)
            if policy is None:
                return frozenset()
            return policy.allowed_actions - policy.denied_actions
        except Exception as exc:
            logger.error("[AgentPermissions] Error reading allowed actions: %s", exc)
            return frozenset()

    def to_dict(self) -> dict[str, Any]:
        """Serialize all policies to a JSON-safe dictionary.

        Returns:
            Dictionary mapping agent type values to their policy details.
        """
        result: dict[str, Any] = {}
        for agent_type, policy in self._policies.items():
            result[agent_type.value] = {
                "allowed_actions": [a.value for a in policy.allowed_actions],
                "denied_actions": [a.value for a in policy.denied_actions],
                "description": policy.description,
            }
        return result
