"""Permission decision flow for the autonomy governor."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from vetinari.autonomy.governor_levels import (
    _confidence_to_band,
    _confidence_to_level,
    _level_to_decision,
    _min_level,
)
from vetinari.autonomy.governor_models import PermissionResult
from vetinari.exceptions import ScopeViolationError
from vetinari.types import AutonomyLevel, DomainCareLevel, PermissionDecision

logger = logging.getLogger(__name__)


class _GovernorPermissionMixin:
    """Permission routing and confidence-based autonomy level evaluation."""

    if TYPE_CHECKING:
        from vetinari.autonomy.governor_models import ActionPolicy
        from vetinari.types import AutonomyMode

        _policies: dict[str, ActionPolicy]
        _autonomy_mode: AutonomyMode
        _domain_care_levels: dict[str, DomainCareLevel]

        def get_policy(self, action_type: str) -> ActionPolicy: ...

    def request_permission(
        self,
        action_type: str,
        details: dict[str, Any] | None = None,
    ) -> PermissionDecision:
        """Check whether an action is permitted under the current autonomy policy.

        Args:
            action_type: The action type identifier.
            details: Optional metadata about the specific action instance.

        Returns:
            APPROVE if the action can proceed autonomously, DENY if blocked,
            or DEFER if human approval is required.
        """
        # LLM06 (Excessive Agency) mitigation: if the caller supplies a
        # granted_scope in details, enforce that the requested action stays
        # within that scope before proceeding to policy evaluation.
        if details:
            granted_scope = details.get("granted_scope")
            if granted_scope:
                enforce_scope_boundary(action_type, granted_scope)

        policy = self.get_policy(action_type)
        level = policy.level
        decision = self._level_to_decision(level)

        if details and policy.max_change_pct < 100.0:
            change_pct = details.get("change_pct", 0.0)
            if change_pct > policy.max_change_pct:
                logger.info(
                    "Action %s change_pct=%.1f exceeds max=%.1f; deferring to human",
                    action_type,
                    change_pct,
                    policy.max_change_pct,
                )
                decision = PermissionDecision.DEFER

        logger.info(
            "Permission request: action=%s level=%s decision=%s",
            action_type,
            level.value,
            decision.value,
        )
        return decision

    def request_permission_full(
        self,
        action_type: str,
        details: dict[str, Any] | None = None,
        confidence: float = 0.0,
        domain: str | None = None,
    ) -> PermissionResult:
        """Check permission and enqueue in the approval queue when deferred.

        Args:
            action_type: The action type identifier.
            details: Optional metadata about the specific action instance.
            confidence: Agent confidence score for this action.
            domain: Optional domain name for per-domain care level override.

        Returns:
            PermissionResult with the decision, queue action ID when deferred,
            and the policy level that produced the decision.
        """
        policy = self.get_policy(action_type)
        level = policy.level

        if domain is not None:
            care = self._domain_care_levels.get(domain)
            if care == DomainCareLevel.REVIEW:
                level = AutonomyLevel.L1_SUGGEST

        if confidence > 0.0 and level != AutonomyLevel.L1_SUGGEST:
            confidence_level = self._confidence_to_level(confidence)
            level = self._min_level(level, confidence_level)

        if details and policy.max_change_pct < 100.0:
            change_pct = details.get("change_pct", 0.0)
            if change_pct > policy.max_change_pct:
                logger.info(
                    "Action %s change_pct=%.1f exceeds max=%.1f; deferring to human",
                    action_type,
                    change_pct,
                    policy.max_change_pct,
                )
                level = AutonomyLevel.L1_SUGGEST

        decision = self._level_to_decision(level)
        action_id: str | None = None

        from vetinari.autonomy.approval_queue import get_approval_queue

        queue = get_approval_queue()

        if decision == PermissionDecision.DEFER:
            action_id = queue.enqueue(action_type, details=details, confidence=confidence)
            logger.info(
                "Action %s deferred; enqueued as %s (confidence=%.2f)",
                action_type,
                action_id,
                confidence,
            )

        queue.log_decision(
            action_type=action_type,
            autonomy_level=level,
            decision=decision,
            confidence=confidence,
            details=details,
        )

        logger.info(
            "Permission request (full): action=%s level=%s decision=%s action_id=%s",
            action_type,
            level.value,
            decision.value,
            action_id or "n/a",
        )
        return PermissionResult(
            decision=decision,
            action_type=action_type,
            action_id=action_id,
            level=level,
            policy=policy,
        )

    @staticmethod
    def _level_to_decision(level: AutonomyLevel) -> PermissionDecision:
        """Map an autonomy level to a permission decision."""
        return _level_to_decision(level)

    @staticmethod
    def _confidence_to_band(confidence: float) -> str:
        """Map a confidence score to a named risk band."""
        return _confidence_to_band(confidence)

    def _confidence_to_level(self, confidence: float) -> AutonomyLevel:
        """Map confidence to an autonomy level under the active mode."""
        return _confidence_to_level(confidence, self._autonomy_mode)

    @staticmethod
    def _min_level(first: AutonomyLevel, second: AutonomyLevel) -> AutonomyLevel:
        """Return the lower, more conservative autonomy level."""
        return _min_level(first, second)


def enforce_scope_boundary(action: str, granted_scope: str) -> None:
    """Enforce that an agent action does not exceed its granted autonomy scope.

    Compares the requested action against the granted scope string. If the action
    starts with a prefix that is not covered by the granted scope, a
    ``ScopeViolationError`` is raised so callers fail closed rather than
    silently permitting out-of-scope actions. This implements the LLM06
    (Excessive Agency) mitigation from OWASP LLM Top 10 2025.

    Args:
        action: The action identifier being requested (e.g.
            ``"file:write:/etc/passwd"`` or ``"network:outbound:untrusted"``).
        granted_scope: The scope token granted to the agent for this session
            (e.g. ``"file:read"`` or ``"network:outbound:trusted"``).

    Raises:
        ScopeViolationError: If ``action`` is not covered by ``granted_scope``.
    """
    if not action.startswith(granted_scope):
        raise ScopeViolationError(
            f"action {action!r} exceeds granted scope {granted_scope!r}; "
            "agent must request explicit permission before performing out-of-scope actions"
        )
