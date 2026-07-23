"""Execution Context Module for Vetinari.

Inspired by OpenCode's agent-based approach, this module implements:
- ExecutionMode (Planning/Read-only vs Execution/Write)
- Context-aware safety checks
- Tool permission enforcement
- Pre/post-execution hooks

This allows Vetinari to operate in different modes with varying levels of access,
similar to OpenCode's 'plan' vs 'build' agents.
"""

from __future__ import annotations

import contextvars
import logging
import threading
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from vetinari.exceptions import SecurityError
from vetinari.execution_context_manager import ContextManagerPermissionMixin
from vetinari.execution_context_permissions import (
    check_permission_unified_impl,
    enforce_agent_permissions_impl,
    enforce_permission_unified_impl,
)
from vetinari.security.fail_closed import sanitize_untrusted_text
from vetinari.types import AgentType, ExecutionMode  # canonical source

logger = logging.getLogger(__name__)


class ToolPermission(Enum):
    """Tool permissions that can be restricted based on execution mode."""

    # File operations
    FILE_READ = "file_read"
    FILE_WRITE = "file_write"
    FILE_DELETE = "file_delete"

    # Execution operations
    BASH_EXECUTE = "bash_execute"
    PYTHON_EXECUTE = "python_execute"
    CODE_EXECUTION = "code_execution"

    # Model operations
    MODEL_INFERENCE = "model_inference"
    MODEL_DISCOVERY = "model_discovery"

    # Web/network operations
    WEB_ACCESS = "web_access"
    NETWORK_REQUEST = "network_request"
    DATABASE_WRITE = "database_write"
    MEMORY_READ = "memory_read"
    MEMORY_WRITE = "memory_write"

    # Planning operations
    PLANNING = "planning"

    # Git operations
    GIT_READ = "git_read"  # Read-only git ops: status, log, diff, current_branch
    GIT_COMMIT = "git_commit"  # Write ops: commit, add, branch, checkout, stash, tag
    GIT_PUSH = "git_push"  # Remote write op: push (irreversible without force)


@dataclass
class PermissionPolicy:
    """Defines which permissions are allowed in a given execution mode."""

    mode: ExecutionMode
    allowed_permissions: set[ToolPermission]
    require_confirmation: set[ToolPermission] = field(default_factory=set)
    deny_patterns: list[str] = field(default_factory=list)  # Regex patterns to deny

    def __repr__(self) -> str:
        return f"PermissionPolicy(mode={self.mode!r}, allowed={len(self.allowed_permissions)})"

    def has_permission(self, permission: ToolPermission) -> bool:
        """Check if a permission is granted."""
        return permission in self.allowed_permissions

    def requires_confirmation(self, permission: ToolPermission) -> bool:
        """Check if a permission requires user confirmation."""
        return permission in self.require_confirmation


# Default permission policies for each mode
DEFAULT_POLICIES = {
    ExecutionMode.PLANNING: PermissionPolicy(
        mode=ExecutionMode.PLANNING,
        allowed_permissions={
            ToolPermission.FILE_READ,
            ToolPermission.GIT_READ,  # Read-only git ops are safe during planning
            ToolPermission.MODEL_INFERENCE,
            ToolPermission.MODEL_DISCOVERY,
            ToolPermission.NETWORK_REQUEST,  # Read-only requests
            ToolPermission.WEB_ACCESS,  # Web search for research during planning
            ToolPermission.MEMORY_READ,
            ToolPermission.PLANNING,  # Plan generation tools
        },
        require_confirmation={
            ToolPermission.BASH_EXECUTE,
            ToolPermission.PYTHON_EXECUTE,
        },
        deny_patterns=[r"^rm\s", r"^mv\s", r"^del\s"],  # Deny destructive commands
    ),
    ExecutionMode.EXECUTION: PermissionPolicy(
        mode=ExecutionMode.EXECUTION,
        allowed_permissions={
            ToolPermission.FILE_READ,
            ToolPermission.FILE_WRITE,
            ToolPermission.FILE_DELETE,
            ToolPermission.BASH_EXECUTE,
            ToolPermission.PYTHON_EXECUTE,
            ToolPermission.MODEL_INFERENCE,
            ToolPermission.MODEL_DISCOVERY,
            ToolPermission.NETWORK_REQUEST,
            ToolPermission.DATABASE_WRITE,
            ToolPermission.MEMORY_READ,
            ToolPermission.MEMORY_WRITE,
            ToolPermission.GIT_READ,  # Read-only git is always allowed in execution
            ToolPermission.GIT_COMMIT,
        },
        require_confirmation={
            ToolPermission.GIT_PUSH,
            ToolPermission.FILE_DELETE,
        },
    ),
    ExecutionMode.SANDBOX: PermissionPolicy(
        mode=ExecutionMode.SANDBOX,
        allowed_permissions={
            ToolPermission.FILE_READ,
            ToolPermission.GIT_READ,  # Inspecting repo state is safe in sandbox
            ToolPermission.PYTHON_EXECUTE,
            ToolPermission.MODEL_INFERENCE,
        },
        require_confirmation={
            ToolPermission.BASH_EXECUTE,
        },
        deny_patterns=[r"^import\s+os", r"^import\s+subprocess"],
    ),
}


@dataclass
class ExecutionContext:
    """Represents the current execution context.

    Tracks mode, permissions, active tasks, and enables safety checks.
    """

    mode: ExecutionMode = ExecutionMode.PLANNING
    policy: PermissionPolicy | None = None
    active_task_id: str | None = None
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    # Hooks
    pre_execution_hooks: list[Callable[[str, dict[str, Any]], bool]] = field(default_factory=list)
    post_execution_hooks: list[Callable[[str, dict[str, Any], Any], None]] = field(default_factory=list)

    # Audit trail
    executed_operations: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        """Initialize policy if not provided."""
        if self.policy is None:
            self.policy = DEFAULT_POLICIES.get(self.mode)

    def can_execute(self, permission: ToolPermission) -> bool:
        """Check if the current context allows execution of a tool.

        Args:
            permission: The ToolPermission to check

        Returns:
            True if execution is allowed, False otherwise
        """
        if not self.policy:
            logger.warning("No policy defined for mode %s", self.mode)
            return False
        return self.policy.has_permission(permission)

    def requires_confirmation(self, permission: ToolPermission) -> bool:
        """Check if an operation requires user confirmation before proceeding.

        Args:
            permission: The ToolPermission to check.

        Returns:
            True if the current policy requires confirmation for this permission,
            or if no policy is defined (fail-safe). False if the permission is
            unconditionally allowed.
        """
        if not self.policy:
            return True
        return self.policy.requires_confirmation(permission)

    def add_pre_execution_hook(self, hook: Callable[[str, dict[str, Any]], bool]) -> None:
        """Register a hook to run before execution.

        Hook signature: (operation_name: str, operation_params: Dict) -> bool
        Should return True to proceed, False to block.

        Raises:
            SecurityError: If ``hook`` is not callable.
        """
        if not callable(hook):
            raise SecurityError("pre-execution hook must be callable")
        self.pre_execution_hooks.append(hook)

    def add_post_execution_hook(self, hook: Callable[[str, dict[str, Any], Any], None]) -> None:
        """Register a hook to run after execution.

        Hook signature: (operation_name: str, operation_params: Dict, result: Any) -> None

        Raises:
            SecurityError: If ``hook`` is not callable.
        """
        if not callable(hook):
            raise SecurityError("post-execution hook must be callable")
        self.post_execution_hooks.append(hook)

    def record_operation(self, operation_name: str, params: dict[str, Any], result: Any) -> None:
        """Record an executed operation for audit trail.

        Args:
            operation_name: The operation name.
            params: The params.
            result: The result.
        """
        operation_name = sanitize_untrusted_text(operation_name, max_length=160)
        safe_params = _safe_audit_payload(params)
        self.executed_operations.append({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "operation": operation_name,
            "params": safe_params,
            "result": _safe_audit_payload(result),
        })

    def get_audit_trail(self) -> list[dict[str, Any]]:
        """Return a snapshot copy of all operations recorded since context creation.

        Returns:
            List of operation records, each containing timestamp, operation name, params, and result.
        """
        return self.executed_operations.copy()

    def __repr__(self) -> str:
        return f"ExecutionContext(mode={self.mode!r}, active_task_id={self.active_task_id!r})"


# Module-level ContextVar so each asyncio task (coroutine) gets its own stack.
# threading.local() only isolates threads; ContextVar isolates both threads AND
# coroutines, preventing concurrent async requests from bleeding execution mode
# or task attribution into each other.
#
# Who writes: ContextManager._get_stack(), temporary_mode() via token-based set/reset
# Who reads:  ContextManager._get_stack(), current_context(), current_mode()
# Lifecycle:  created once at module import; each coroutine/thread gets its own copy
# Lock:       not needed — ContextVar guarantees copy-on-write semantics per task
_context_stack_var: contextvars.ContextVar[list[ExecutionContext]] = contextvars.ContextVar("vetinari_context_stack")


class ContextManager(ContextManagerPermissionMixin):
    """Manages execution contexts for Vetinari.

    Provides context-switching, safety checks, and enforcement of permissions.

    Isolation: each asyncio task (coroutine) and each thread gets its own
    independent context stack via ``contextvars.ContextVar``.  Concurrent async
    requests cannot bleed execution mode or task attribution into each other.
    """

    def __init__(
        self,
        stack_var: contextvars.ContextVar[list[ExecutionContext]] | None = None,
    ) -> None:
        """Create a context manager with isolated stack storage by default."""
        self._stack_var = stack_var or contextvars.ContextVar(
            f"vetinari_context_stack_{id(self)}",
        )

    def _get_stack(self) -> list[ExecutionContext]:
        """Return the context stack for the current task, creating a default on first access."""
        try:
            return self._stack_var.get()
        except LookupError:
            # First access in this coroutine/thread — seed with a PLANNING context.
            logger.warning("ExecutionContext stack not found in current thread — initializing default PLANNING context")
            default = ExecutionContext(mode=ExecutionMode.PLANNING)
            stack = [default]
            self._stack_var.set(stack)
            return stack

    @property
    def current_context(self) -> ExecutionContext:
        """Return the top-most context on this thread's stack."""
        stack = self._get_stack()
        return stack[-1]

    @property
    def current_mode(self) -> ExecutionMode:
        """Return the execution mode of the current context on this thread."""
        return self.current_context.mode

    def switch_mode(self, mode: ExecutionMode, task_id: str | None = None) -> ExecutionContext:
        """Switch to a different execution mode on this thread's context stack.

        Args:
            mode: The ExecutionMode to switch to.
            task_id: Optional task ID associated with this context.

        Returns:
            The new ExecutionContext pushed onto this thread's stack.
        """
        context = ExecutionContext(
            mode=mode,
            active_task_id=task_id,
            policy=DEFAULT_POLICIES.get(mode),
        )
        self._get_stack().append(context)
        logger.info("Switched to mode %s%s", mode.value, (f" for task {task_id}") if task_id else "")
        return context

    def pop_context(self) -> ExecutionContext | None:
        """Pop the current context and return to the previous one on this thread's stack.

        Returns:
            The popped ExecutionContext, or None if only the default context remains.
        """
        stack = self._get_stack()
        if len(stack) > 1:
            context = stack.pop()
            logger.info("Popped context, returned to %s", self.current_mode.value)
            return context
        return None

    def check_permission(self, permission: ToolPermission) -> bool:
        """Check if the current context allows a permission.

        Args:
            permission: The ToolPermission to check

        Returns:
            True if allowed, False otherwise
        """
        return self.current_context.can_execute(permission)

    def requires_confirmation(self, permission: ToolPermission) -> bool:
        """Check if an operation requires user confirmation."""
        return self.current_context.requires_confirmation(permission)

    @contextmanager
    def temporary_mode(self, mode: ExecutionMode, task_id: str | None = None) -> Generator[None, None, None]:
        """Context manager for temporarily switching to a different execution mode.

        Installs a fresh context stack for the duration of the block so that
        concurrent async tasks cannot observe each other's mode transitions.
        The previous stack is atomically restored via ``ContextVar.reset(token)``
        regardless of how the block exits.

        Usage:
            with context_manager.temporary_mode(ExecutionMode.EXECUTION):
                # Execute operations with EXECUTION permissions
                ...

        Args:
            mode: The ExecutionMode to activate inside the block.
            task_id: Optional task ID to associate with the temporary context.

        Yields:
            None (callers access current_context via get_context_manager().current_context).
        """
        new_context = ExecutionContext(
            mode=mode,
            active_task_id=task_id,
            policy=DEFAULT_POLICIES.get(mode),
        )
        # Build a new stack that inherits the current stack's contents then adds
        # the new context — this preserves the nesting invariant.
        current_stack = self._get_stack()
        new_stack = [*list(current_stack), new_context]
        token = self._stack_var.set(new_stack)
        logger.info(
            "Entered temporary mode %s%s",
            mode.value,
            (f" for task {task_id}") if task_id else "",
        )
        try:
            yield
        finally:
            self._stack_var.reset(token)
            logger.info("Exited temporary mode %s, restored previous context", mode.value)

    def get_status(self) -> dict[str, Any]:
        """Get the current status of the context manager.

        Returns:
            Dictionary with keys: mode, task_id, started_at, operations_count,
            and permissions (list of allowed permission values for the current context).
        """
        ctx = self.current_context
        return {
            "mode": ctx.mode.value,
            "task_id": ctx.active_task_id,
            "started_at": ctx.started_at.isoformat(),
            "operations_count": len(ctx.executed_operations),
            "permissions": [p.value for p in ctx.policy.allowed_permissions] if ctx.policy else [],
        }


# Global context manager instance
_context_manager: ContextManager | None = None
_context_manager_lock = threading.Lock()


def get_context_manager() -> ContextManager:
    """Get or create the global context manager instance.

    Returns:
        The singleton ContextManager, initialised in PLANNING mode on first call.
    """
    global _context_manager
    if _context_manager is None:
        with _context_manager_lock:
            if _context_manager is None:
                _context_manager = ContextManager(stack_var=_context_stack_var)
    return _context_manager


def _safe_audit_payload(value: Any) -> Any:
    if isinstance(value, str):
        return sanitize_untrusted_text(value, max_length=4_000) if value.strip() else ""
    if isinstance(value, dict):
        return {
            sanitize_untrusted_text(str(key), max_length=160): _safe_audit_payload(item) for key, item in value.items()
        }
    if isinstance(value, list):
        return [_safe_audit_payload(item) for item in value]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return sanitize_untrusted_text(str(value), max_length=4_000)


def current_mode() -> ExecutionMode:
    """Convenience accessor for the active execution mode on the global context stack."""
    return get_context_manager().current_mode


def current_context() -> ExecutionContext:
    """Convenience accessor for the active ExecutionContext on the global context stack."""
    return get_context_manager().current_context


# ── Per-agent permission model (US-040) ──────────────────────────────────────

# Per-agent permission mapping — defines what each agent type is allowed to do.
# Each entry is a frozenset to prevent accidental mutation at runtime.
AGENT_PERMISSION_MAP: dict[AgentType, frozenset[ToolPermission]] = {
    AgentType.FOREMAN: frozenset({
        ToolPermission.FILE_READ,
        ToolPermission.MODEL_INFERENCE,
        ToolPermission.MODEL_DISCOVERY,
    }),
    AgentType.WORKER: frozenset({
        # Union of all former execution agent permissions
        ToolPermission.FILE_READ,
        ToolPermission.FILE_WRITE,
        ToolPermission.MODEL_INFERENCE,
        ToolPermission.MODEL_DISCOVERY,
        ToolPermission.NETWORK_REQUEST,
        ToolPermission.BASH_EXECUTE,
        ToolPermission.PYTHON_EXECUTE,
        ToolPermission.MEMORY_WRITE,
    }),
    AgentType.INSPECTOR: frozenset({
        ToolPermission.FILE_READ,
        ToolPermission.MODEL_INFERENCE,
        ToolPermission.BASH_EXECUTE,
        ToolPermission.PYTHON_EXECUTE,
    }),
}


def enforce_agent_permissions(agent_type: AgentType, permission: ToolPermission) -> None:
    """Check if an agent type is allowed to use a specific permission.

    Args:
        agent_type: The type of agent requesting the permission.
        permission: The permission being requested.

    Raises:
        PermissionError: If the agent type is not allowed the requested permission.
    """
    enforce_agent_permissions_impl(agent_type, permission, AGENT_PERMISSION_MAP, SecurityError)


# ── Unified permission arbitration (most-restrictive-wins) ────────────────


def check_permission_unified(
    agent_type: AgentType,
    permission: ToolPermission,
    *,
    action: str | None = None,
    target: str | None = None,
    context: dict[str, Any] | None = None,
) -> bool:
    """Check if a permission is granted by BOTH the execution-mode policy AND the per-agent map.

    Most-restrictive-wins: all three layers (mode policy, agent permission map, and
    when ``action`` is supplied, the ``PolicyEnforcer`` jurisdiction/irreversibility
    rules) must agree. Any exception inside a check causes an immediate ``False``
    (fail-closed behaviour).

    Args:
        agent_type: The type of agent requesting the permission.
        permission: The permission being requested.
        action: Optional action verb (e.g. ``"write"``, ``"delete"``). When
            provided the ``PolicyEnforcer`` is also consulted.
        target: Optional resource path acted on. Defaults to ``""`` when
            ``action`` is given but ``target`` is omitted.
        context: Optional context dict forwarded to ``PolicyEnforcer.check_action``.

    Returns:
        ``True`` only if every applicable layer permits the request.
    """
    return check_permission_unified_impl(
        agent_type,
        permission,
        AGENT_PERMISSION_MAP,
        get_context_manager,
        action=action,
        target=target,
        context=context,
    )


def enforce_permission_unified(
    agent_type: AgentType,
    permission: ToolPermission,
    operation_name: str = "operation",
) -> None:
    """Enforce a permission using both mode and agent policies (most-restrictive-wins).

    Args:
        agent_type: The type of agent requesting the permission.
        permission: The permission being requested.
        operation_name: Human-readable operation name for error messages.

    Raises:
        SecurityError: If either the mode policy or agent map denies the permission.
    """
    enforce_permission_unified_impl(
        agent_type,
        permission,
        AGENT_PERMISSION_MAP,
        get_context_manager,
        check_permission_unified,
        SecurityError,
        operation_name,
    )
