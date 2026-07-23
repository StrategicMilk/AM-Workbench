"""Pre-execution policy enforcement layer.

Evaluates every agent action against safety policies before execution.
Checks: irreversibility, resource budgets, stakeholder impact, file jurisdiction.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from vetinari.types import AgentType
from vetinari.utils.serialization import dataclass_to_dict

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# File jurisdiction map — which agent type owns which directory prefixes
# ---------------------------------------------------------------------------

# Maps agent type (lowercase) to the set of path prefixes it may write to.
# WORKER is the only agent permitted to write production source files.
# FOREMAN owns planning artifacts; INSPECTOR owns review artifacts.
# Keys are lowercased for case-insensitive lookup in _check_jurisdiction.
_JURISDICTION: dict[str, list[str]] = {
    AgentType.WORKER.value.lower(): ["vetinari/", "src/", "lib/", "docs/"],
    AgentType.FOREMAN.value.lower(): [".omc/plans/", ".omc/state/", ".omc/decisions/"],
    AgentType.INSPECTOR.value.lower(): [".omc/reviews/", "reports/", ".omc/research/"],
}

# Actions that are considered irreversible / destructive
_DESTRUCTIVE_ACTIONS: frozenset[str] = frozenset(
    {
        "delete",
        "drop",
        "truncate",
        "remove",
        "purge",
        "overwrite",
        "wipe",
        "destroy",
        "rm",
        "unlink",
    },
)

# Default resource budgets
_DEFAULT_MAX_TOKENS: int = 100_000
_DEFAULT_MAX_TIME_SECONDS: float = 600.0
_DEFAULT_MAX_DELEGATION_DEPTH: int = 3


# ---------------------------------------------------------------------------
# PolicyDecision
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    """Result of a policy check for an agent action.

    Attributes:
        allowed: Whether the action is permitted.
        reason: Human-readable explanation of the decision.
        risk_level: One of ``"low"``, ``"medium"``, or ``"high"``.
    """

    allowed: bool
    reason: str
    risk_level: str = "low"  # "low" | "medium" | "high"

    def to_dict(self) -> dict[str, Any]:
        """Converts the policy decision fields to a JSON-serializable dict."""
        return dataclass_to_dict(self)


# ---------------------------------------------------------------------------
# PolicyEnforcer
# ---------------------------------------------------------------------------


class PolicyEnforcer:
    """Singleton policy gate evaluated before every agent action.

    Use ``get_policy_enforcer()`` to obtain the shared instance.

    Example::

        enforcer = get_policy_enforcer()
        decision = enforcer.check_action(
            agent_type="builder",
            action="write",
            target="vetinari/agents/foo.py",
            context={"delegation_depth": 1},
        )
        if not decision.allowed:
            raise PermissionError(decision.reason)
    """

    _instance: PolicyEnforcer | None = None
    _class_lock = threading.Lock()

    def __new__(cls) -> PolicyEnforcer:
        if cls._instance is None:
            with cls._class_lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._setup()
        return cls._instance

    def _setup(self) -> None:
        self._lock = threading.Lock()
        self._policies: list[tuple[str, Callable[[str, str, str, dict[str, Any]], PolicyDecision | None]]] = []
        self._total_checks: int = 0
        self._total_denied: int = 0
        self._total_flagged_irreversible: int = 0
        # Register built-in policies in priority order
        self.register_policy("jurisdiction", self._check_jurisdiction)
        self.register_policy("delegation_depth", self._check_delegation_depth)
        self.register_policy("irreversibility", self._check_irreversibility)
        self.register_policy("resource_budget", self._check_resource_budget)

    # ------------------------------------------------------------------
    # Policy registration
    # ------------------------------------------------------------------

    def register_policy(
        self,
        name: str,
        check_fn: Callable[[str, str, str, dict[str, Any]], PolicyDecision | None],
    ) -> None:
        """Register a named policy check function.

        The ``check_fn`` receives ``(agent_type, action, target, context)``
        and should return a ``PolicyDecision`` to short-circuit evaluation,
        or ``None`` to pass through to the next policy.

        Args:
            name: Human-readable policy name (used in logs and stats).
            check_fn: Callable matching the signature described above.
        """
        with self._lock:
            self._policies.append((name, check_fn))
        logger.debug("Registered policy %r", name)

    # ------------------------------------------------------------------
    # Main check
    # ------------------------------------------------------------------

    def check_action(
        self,
        agent_type: str | AgentType,
        action: str,
        target: str,
        context: dict[str, Any],
    ) -> PolicyDecision:
        """Evaluate all registered policies for the given action.

        Policies are evaluated in registration order. The first policy that
        returns a ``PolicyDecision`` wins; remaining policies are skipped.
        If all policies return ``None`` the action is allowed.

        Args:
            agent_type: Agent type as ``AgentType`` enum or its ``.value`` string.
            action: The action being requested (e.g. ``"write"``, ``"delete"``).
            target: The resource path or identifier being acted on.
            context: Arbitrary context dict (may include ``delegation_depth``,
                ``tokens_used``, ``elapsed_seconds``, etc.).

        Returns:
            ``PolicyDecision`` indicating whether the action is permitted.
        """
        with self._lock:
            policies_snapshot = list(self._policies)
            self._total_checks += 1

        action_lower = action.lower()
        # Normalize agent_type: accept both AgentType enum and string
        if isinstance(agent_type, AgentType):
            agent_lower = agent_type.value.lower()
        else:
            agent_lower = agent_type.lower()

        for name, check_fn in policies_snapshot:
            try:
                result = check_fn(agent_lower, action_lower, target, context)
            except Exception as exc:
                # Fail closed: a broken policy check blocks the action
                logger.warning(
                    "Policy %r raised unexpectedly: %s — failing closed (action denied)",
                    name,
                    exc,
                )
                with self._lock:
                    self._total_denied += 1
                return PolicyDecision(
                    allowed=False,
                    reason=f"Policy {name!r} error: {exc} — action denied (fail-closed)",
                    risk_level="high",
                )

            if result is not None:
                if not result.allowed:
                    with self._lock:
                        self._total_denied += 1
                    logger.info(
                        "Action denied by policy %r: agent=%r action=%r target=%r reason=%r",
                        name,
                        agent_type,
                        action,
                        target,
                        result.reason,
                    )
                return result

        return PolicyDecision(allowed=True, reason="all policies passed", risk_level="low")

    # ------------------------------------------------------------------
    # Built-in policy implementations
    # ------------------------------------------------------------------

    @staticmethod
    def _check_jurisdiction(
        agent_type: str,
        action: str,
        target: str,
        context: dict[str, Any],
    ) -> PolicyDecision | None:
        """Block mutating actions outside an agent's permitted path prefixes.

        Covers both write-style actions (create, edit, …) and destructive
        deletes so that jurisdiction cannot be bypassed by using the delete
        verb instead of write.

        Unknown agent types are denied by default (fail-closed) rather than
        falling through to other policies, because an unrecognised principal
        must never be silently permitted to act on the filesystem.
        """
        # Gate all mutating verbs — write-style and destructive deletes alike.
        # Read-only or unknown verbs pass through; the irreversibility policy
        # handles destructive-action context checks independently.
        mutating_actions = frozenset({
            "write",
            "create",
            "edit",
            "patch",
            "modify",
            "update",
            "delete",
            "remove",
            "unlink",
            "rm",
        })
        if action not in mutating_actions:
            return None

        allowed_prefixes = _JURISDICTION.get(agent_type)
        if allowed_prefixes is None:
            # Unknown principal — fail closed. An unrecognised agent type has
            # no declared jurisdiction and must not silently inherit any access.
            logger.warning(
                "Unknown agent type %r in _check_jurisdiction; all mutating actions denied; "
                "register the agent in _JURISDICTION to grant write access",
                agent_type,
            )
            return PolicyDecision(
                allowed=False,
                reason=(
                    f"Unknown agent type {agent_type!r} has no jurisdiction entry — "
                    "all mutating actions denied until the agent is registered in _JURISDICTION"
                ),
                risk_level="high",
            )

        # Normalise path separators for comparison
        normalised_target = target.replace("\\", "/")
        for prefix in allowed_prefixes:
            normalised_prefix = prefix.replace("\\", "/")
            # Require a path-component boundary so "vetinari/" cannot match
            # "vetinari_evil/private.py".
            if normalised_target == normalised_prefix or normalised_target.startswith(
                normalised_prefix.rstrip("/") + "/"
            ):
                return None  # within jurisdiction

        return PolicyDecision(
            allowed=False,
            reason=(
                f"Agent {agent_type!r} is not permitted to write to {target!r}. Allowed prefixes: {allowed_prefixes}"
            ),
            risk_level="high",
        )

    @staticmethod
    def _check_delegation_depth(
        agent_type: str,
        action: str,
        target: str,
        context: dict[str, Any],
    ) -> PolicyDecision | None:
        """Block actions that exceed the maximum delegation depth."""
        depth = context.get("delegation_depth", 0)
        max_depth = context.get("max_delegation_depth", _DEFAULT_MAX_DELEGATION_DEPTH)

        if not isinstance(depth, (int, float)):
            return None

        if depth > max_depth:
            return PolicyDecision(
                allowed=False,
                reason=(f"Delegation depth {depth} exceeds maximum of {max_depth}"),
                risk_level="high",
            )
        return None

    def _check_irreversibility(
        self,
        agent_type: str,
        action: str,
        target: str,
        context: dict[str, Any],
    ) -> PolicyDecision | None:
        """Flag (but allow) destructive actions; block if context disallows them."""
        if action not in _DESTRUCTIVE_ACTIONS:
            return None

        with self._lock:
            self._total_flagged_irreversible += 1

        allow_destructive = context.get("allow_destructive", False)
        if not allow_destructive:
            return PolicyDecision(
                allowed=False,
                reason=(
                    f"Action {action!r} is irreversible/destructive. Set context['allow_destructive']=True to permit."
                ),
                risk_level="high",
            )

        # Allowed but flagged at medium risk
        return PolicyDecision(
            allowed=True,
            reason=f"Destructive action {action!r} permitted by context",
            risk_level="medium",
        )

    @staticmethod
    def _check_resource_budget(
        agent_type: str,
        action: str,
        target: str,
        context: dict[str, Any],
    ) -> PolicyDecision | None:
        """Block actions when resource budgets are exhausted."""
        tokens_used = context.get("tokens_used", 0)
        max_tokens = context.get("max_tokens", _DEFAULT_MAX_TOKENS)
        elapsed = context.get("elapsed_seconds", 0.0)
        max_time = context.get("max_time_seconds", _DEFAULT_MAX_TIME_SECONDS)

        if isinstance(tokens_used, (int, float)) and isinstance(max_tokens, (int, float)) and tokens_used > max_tokens:
            return PolicyDecision(
                allowed=False,
                reason=(f"Token budget exhausted: {tokens_used} > {max_tokens}"),
                risk_level="medium",
            )

        if isinstance(elapsed, (int, float)) and isinstance(max_time, (int, float)) and elapsed > max_time:
            return PolicyDecision(
                allowed=False,
                reason=(f"Time budget exhausted: {elapsed:.1f}s > {max_time:.1f}s"),
                risk_level="medium",
            )

        return None

    # ------------------------------------------------------------------
    # Convenience: enforce (raise on deny)
    # ------------------------------------------------------------------

    def enforce_all(
        self,
        agent_type: str | AgentType,
        action: str,
        target: str,
        context: dict[str, Any] | None = None,
    ) -> PolicyDecision:
        """Run ``check_action`` and raise ``SecurityError`` if denied.

        Args:
            agent_type: Agent type as ``AgentType`` enum or string.
            action: The action being requested.
            target: Resource path or identifier.
            context: Optional context dict.

        Returns:
            The ``PolicyDecision`` (always ``allowed=True`` — denied throws).

        Raises:
            vetinari.exceptions.SecurityError: If any policy denies the action.
        """
        from vetinari.exceptions import SecurityError

        if context is None:
            context = {}
        decision = self.check_action(agent_type, action, target, context)
        if not decision.allowed:
            raise SecurityError(
                f"Policy denied: {decision.reason} (agent={agent_type}, action={action!r}, target={target!r})",
            )
        return decision

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------

    def get_stats(self) -> dict[str, Any]:
        """Return policy enforcement statistics.

        Returns:
            Dict with keys ``registered_policies``, ``total_checks``,
            ``total_denied``, and ``total_flagged_irreversible``.
        """
        with self._lock:
            return {
                "registered_policies": len(self._policies),
                "total_checks": self._total_checks,
                "total_denied": self._total_denied,
                "total_flagged_irreversible": self._total_flagged_irreversible,
            }


# ---------------------------------------------------------------------------
# Singleton accessor + reset helper
# ---------------------------------------------------------------------------


def get_policy_enforcer() -> PolicyEnforcer:
    """Return the shared ``PolicyEnforcer`` singleton."""
    return PolicyEnforcer()


def reset_policy_enforcer() -> None:
    """Destroy the singleton — intended for use in tests."""
    with PolicyEnforcer._class_lock:
        PolicyEnforcer._instance = None
