"""Permission arbitration helpers for execution_context compatibility wrappers."""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import Any

from vetinari.types import AgentType

logger = logging.getLogger(__name__)


def enforce_agent_permissions_impl(
    agent_type: AgentType,
    permission: Any,
    agent_permission_map: Mapping[AgentType, frozenset[Any]],
    security_error_type: type[Exception],
) -> None:
    """Enforce per-agent permission membership.

    Args:
        agent_type: Agent type requesting access.
        permission: Permission requested by the agent.
        agent_permission_map: Mapping of agent types to allowed permissions.
        security_error_type: Exception type raised on denial.

    Raises:
        Exception: Uses ``security_error_type`` when the permission is denied.
    """
    allowed = agent_permission_map.get(agent_type)
    if allowed is None:
        logger.warning(
            "No permission map entry for agent type %s - denying %s by default",
            agent_type.value,
            permission.value,
        )
        raise security_error_type(
            f"Agent {agent_type.value} has no permission mapping - "
            f"cannot use {permission.value}. Add an entry to AGENT_PERMISSION_MAP.",
        )
    if permission not in allowed:
        logger.warning(
            "Permission denied: agent %s attempted %s (allowed: %s)",
            agent_type.value,
            permission.value,
            ", ".join(p.value for p in sorted(allowed, key=lambda p: p.value)),
        )
        try:
            from vetinari.audit import get_audit_logger

            get_audit_logger().log_permission_check(
                agent_type=agent_type.value,
                permission=permission.value,
                outcome="denied",
            )
        except Exception:
            logger.warning("Audit logging failed", exc_info=True)
        raise security_error_type(
            f"Agent {agent_type.value} is not allowed {permission.value}. "
            f"Allowed permissions: {', '.join(p.value for p in sorted(allowed, key=lambda p: p.value))}",
        )
    try:
        from vetinari.audit import get_audit_logger

        get_audit_logger().log_permission_check(
            agent_type=agent_type.value,
            permission=permission.value,
            outcome="allowed",
        )
    except Exception:
        logger.warning("Audit logging failed", exc_info=True)


def check_permission_unified_impl(
    agent_type: AgentType,
    permission: Any,
    agent_permission_map: Mapping[AgentType, frozenset[Any]],
    get_context_manager_fn: Callable[[], Any],
    *,
    action: str | None = None,
    target: str | None = None,
    context: dict[str, Any] | None = None,
) -> bool:
    """Apply the most-restrictive-wins permission decision.

    Args:
        agent_type: Agent type requesting access.
        permission: Permission requested by the agent.
        agent_permission_map: Mapping of agent types to allowed permissions.
        get_context_manager_fn: Callable returning the active context manager.
        action: Optional action verb forwarded to policy enforcement.
        target: Optional target resource forwarded to policy enforcement.
        context: Optional context forwarded to policy enforcement.

    Returns:
        True only when every applicable policy layer permits the request.
    """
    try:
        ctx_mgr = get_context_manager_fn()
        mode_allowed = ctx_mgr.check_permission(permission)
        if not mode_allowed:
            return False

        agent_allowed_set = agent_permission_map.get(agent_type)
        agent_allowed = agent_allowed_set is not None and permission in agent_allowed_set
        if not agent_allowed:
            return False

        if action is not None:
            from vetinari.safety.policy_enforcer import get_policy_enforcer

            decision = get_policy_enforcer().check_action(
                agent_type=agent_type,
                action=action,
                target=target or "",
                context=context or {},
            )
            if not decision.allowed:
                return False

        return True
    except Exception:
        logger.warning(
            "check_permission_unified failed for agent=%s permission=%s action=%s - failing closed",
            agent_type.value,
            permission.value,
            action,
        )
        return False


def enforce_permission_unified_impl(
    agent_type: AgentType,
    permission: Any,
    agent_permission_map: Mapping[AgentType, frozenset[Any]],
    get_context_manager_fn: Callable[[], Any],
    check_permission_unified_fn: Callable[[AgentType, Any], bool],
    security_error_type: type[Exception],
    operation_name: str,
) -> None:
    """Raise when unified permission arbitration denies an operation.

    Args:
        agent_type: Agent type requesting access.
        permission: Permission requested by the agent.
        agent_permission_map: Mapping of agent types to allowed permissions.
        get_context_manager_fn: Callable returning the active context manager.
        check_permission_unified_fn: Callable used for the combined permission check.
        security_error_type: Exception type raised on denial.
        operation_name: Human-readable operation name for errors.

    Raises:
        Exception: Uses ``security_error_type`` when the operation is denied.
    """
    if not check_permission_unified_fn(agent_type, permission):
        ctx_mgr = get_context_manager_fn()
        mode_ok = ctx_mgr.check_permission(permission)
        agent_set = agent_permission_map.get(agent_type)
        agent_ok = agent_set is not None and permission in agent_set

        denied_by = []
        if not mode_ok:
            denied_by.append(f"mode={ctx_mgr.current_mode.value}")
        if not agent_ok:
            denied_by.append(f"agent={agent_type.value}")

        raise security_error_type(
            f"'{operation_name}' requires {permission.value}, denied by: {', '.join(denied_by)}",
        )
