"""Context manager method helpers for execution_context wrappers."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from vetinari.exceptions import SecurityError

if TYPE_CHECKING:
    from vetinari.execution_context import ToolPermission

logger = logging.getLogger(__name__)


class ContextManagerPermissionMixin:
    """Permission enforcement implementation for ContextManager."""

    if TYPE_CHECKING:
        check_permission: Any
        current_mode: Any
        requires_confirmation: Any

    def enforce_permission(self, permission: ToolPermission, operation_name: str = "operation") -> None:
        """Enforce a permission, raising SecurityError if not allowed or if confirmation is required.

        Fails closed in headless/automated contexts: operations marked as requiring
        confirmation are blocked because there is no interactive prompt available.
        Callers that have obtained explicit user consent should switch to EXECUTION mode
        before invoking the operation.

        Args:
            permission: The ToolPermission to enforce.
            operation_name: Human-readable operation name used in error messages.

        Raises:
            SecurityError: If the permission is denied in the current mode, or if
                the current policy requires confirmation for this permission (since
                automated agents cannot solicit interactive confirmation).
        """
        if not self.check_permission(permission):
            try:
                from vetinari.audit import get_audit_logger

                get_audit_logger().log_permission_check(
                    agent_type=operation_name,
                    permission=permission.value,
                    outcome="denied",
                )
            except Exception:
                logger.warning("Audit logging failed for denied permission %s", permission.value, exc_info=True)
            raise SecurityError(
                f"'{operation_name}' operation requires {permission.value} permission, "
                f"which is not allowed in {self.current_mode.value} mode",
            )

        # Fail-closed for confirmation-required operations: automated agents have no
        # mechanism to solicit user confirmation, so treat it as a denial.
        if self.requires_confirmation(permission):
            try:
                from vetinari.audit import get_audit_logger

                get_audit_logger().log_permission_check(
                    agent_type=operation_name,
                    permission=permission.value,
                    outcome="denied",
                )
            except Exception:
                logger.warning(
                    "Audit logging failed for confirmation-blocked permission %s",
                    permission.value,
                    exc_info=True,
                )
            raise SecurityError(
                f"'{operation_name}' operation requires {permission.value} confirmation "
                f"before proceeding — switch to an execution context with explicit approval",
            )

        try:
            from vetinari.audit import get_audit_logger

            get_audit_logger().log_permission_check(
                agent_type=operation_name,
                permission=permission.value,
                outcome="allowed",
            )
        except Exception:
            logger.warning("Audit logging failed for allowed permission %s", permission.value, exc_info=True)
