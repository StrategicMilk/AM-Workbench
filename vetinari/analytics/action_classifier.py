"""Three-tier action classification for human-in-the-loop governance.

Classifies all agent actions into three tiers:
- Allow: Safe actions that proceed without human involvement (read-only queries,
  status checks, low-risk tool calls)
- RequireApproval: Destructive or sensitive actions that pause for human review
  (production changes, compliance-sensitive operations)
- Deny: Actions outside the agent's authorized scope, blocked with a rejection
  rationale

This is a governance component in the analytics pipeline. It sits between the
agent that proposes an action and the executor that carries it out, ensuring
humans remain in the loop for consequential decisions.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Any

from vetinari.types import ActionTier

logger = logging.getLogger(__name__)


# Actions that are always safe: read-only, non-destructive, no side effects.
# Any action in this set proceeds without human review.
ALLOW_ACTIONS: frozenset[str] = frozenset({
    "code_review",
    "documentation_read",
    "get_logs",
    "get_metrics",
    "health_check",
    "list_files",
    "query",
    "read_file",
    "research",
    "search",
    "status_check",
})

# Actions that are never permitted regardless of context or overrides.
# Attempting these triggers an immediate DENY with a rationale.
DENY_ACTIONS: frozenset[str] = frozenset({
    "access_secrets",
    "bypass_guardrails",
    "delete_production_data",
    "disable_safety",
    "modify_credentials",
    "modify_permissions",
})

# Actions that require Foreman review specifically because they cross
# organizational or compliance boundaries beyond ordinary approval.
FOREMAN_REVIEW_ACTIONS: frozenset[str] = frozenset({
    "deploy_to_production",
    "modify_billing",
    "modify_compliance_config",
    "publish_external",
})


@dataclass(frozen=True, slots=True)
class ActionClassification:
    """The outcome of classifying a single agent action.

    Attributes:
        tier: The governance tier assigned to this action.
        action_type: The action string that was classified.
        rationale: Human-readable explanation of why this tier was assigned.
        requires_foreman_review: Whether the Foreman agent must also sign off
            before the action is executed, in addition to any human approval.
    """

    tier: ActionTier
    action_type: str
    rationale: str
    requires_foreman_review: bool = False

    def __repr__(self) -> str:
        foreman = " [foreman-review]" if self.requires_foreman_review else ""
        return f"ActionClassification(tier={self.tier.value!r}, action={self.action_type!r}{foreman})"


class ActionClassifier:
    """Classifies agent actions into the three governance tiers.

    Uses three fixed sets — ALLOW_ACTIONS, DENY_ACTIONS, and
    FOREMAN_REVIEW_ACTIONS — plus a caller-supplied override map to decide
    whether an action should proceed automatically, wait for human approval,
    or be denied outright.

    Thread-safe: all state is read-only after construction. Overrides are
    passed per-call, not stored on the instance.
    """

    def classify(
        self,
        action_type: str,
        agent_type: str,
        context: dict[str, Any] | None = None,
    ) -> ActionClassification:
        """Classify an action using the default rule sets.

        Args:
            action_type: The action the agent wants to perform (e.g.
                ``"read_file"``, ``"delete_production_data"``).
            agent_type: The agent type string requesting the action (e.g.
                ``"WORKER"``). Used for logging and rationale messages.
            context: Optional dict of contextual metadata (project_id,
                environment, etc.) available to enrich the rationale. Not
                used for tier routing in the default implementation but
                forwarded to callers who inspect the returned rationale.

        Returns:
            An :class:`ActionClassification` describing the tier, action, and
            rationale for the assignment.
        """
        return self._classify_internal(action_type, agent_type, overrides=None, context=context)

    def classify_with_override(
        self,
        action_type: str,
        agent_type: str,
        overrides: dict[str, ActionTier] | None,
        context: dict[str, Any] | None = None,
    ) -> ActionClassification:
        """Classify an action, allowing per-context tier overrides.

        Overrides let callers promote or demote specific actions for a
        particular execution context (e.g. promoting ``"deploy_to_production"``
        to ALLOW inside a controlled CI environment).

        DENY_ACTIONS cannot be overridden: the deny set is absolute.

        Args:
            action_type: The action the agent wants to perform.
            agent_type: The agent type string requesting the action.
            overrides: Mapping of ``action_type -> ActionTier`` that takes
                precedence over the default classification rules.  Pass
                ``None`` or an empty dict to skip overrides.
            context: Optional dict of contextual metadata.

        Returns:
            An :class:`ActionClassification` for the (possibly overridden)
            tier assignment.
        """
        return self._classify_internal(action_type, agent_type, overrides=overrides, context=context)

    # -- internal ----------------------------------------------------------------

    @staticmethod
    def _deny_classification(action_type: str, agent_type: str) -> ActionClassification:
        """Build the absolute-deny classification for prohibited actions."""
        logger.warning(
            "Action %r denied for agent %s — action is in the absolute deny list",
            action_type,
            agent_type,
        )
        return ActionClassification(
            tier=ActionTier.DENY,
            action_type=action_type,
            rationale=(
                f"Action '{action_type}' is in the absolute deny list and may never "
                "be executed regardless of context or overrides. It falls outside "
                "all authorized agent scopes."
            ),
            requires_foreman_review=False,
        )

    @staticmethod
    def _override_classification(
        action_type: str,
        agent_type: str,
        normalised: str,
        overrides: dict[str, ActionTier] | None,
    ) -> ActionClassification | None:
        """Return a caller override classification when one applies."""
        if not overrides:
            return None

        override_tier = overrides.get(normalised) or overrides.get(action_type)
        if override_tier is None:
            return None

        requires_foreman = normalised in FOREMAN_REVIEW_ACTIONS
        if requires_foreman and override_tier is ActionTier.ALLOW:
            logger.warning(
                "Action %r for agent %s attempted unsafe override to ALLOW; preserving Foreman review",
                action_type,
                agent_type,
            )
            return ActionClassification(
                tier=ActionTier.REQUIRE_APPROVAL,
                action_type=action_type,
                rationale=(
                    f"Action '{action_type}' requires Foreman review and cannot be downgraded "
                    "to ALLOW by the caller-supplied override map."
                ),
                requires_foreman_review=True,
            )

        logger.info(
            "Action %r for agent %s overridden to tier %s",
            action_type,
            agent_type,
            override_tier.value,
        )
        return ActionClassification(
            tier=override_tier,
            action_type=action_type,
            rationale=(
                f"Action '{action_type}' was overridden to tier "
                f"'{override_tier.value}' by the caller-supplied override map."
            ),
            requires_foreman_review=requires_foreman,
        )

    @staticmethod
    def _allow_classification(action_type: str, agent_type: str) -> ActionClassification:
        """Build the allow-list classification for safe read-only actions."""
        logger.debug(
            "Action %r for agent %s classified as ALLOW (read-only allow list)",
            action_type,
            agent_type,
        )
        return ActionClassification(
            tier=ActionTier.ALLOW,
            action_type=action_type,
            rationale=f"Action '{action_type}' is a read-only or safe operation that does not require human review.",
            requires_foreman_review=False,
        )

    @staticmethod
    def _foreman_review_classification(action_type: str, agent_type: str) -> ActionClassification:
        """Build the classification for actions that need Foreman review."""
        logger.info(
            "Action %r for agent %s classified as REQUIRE_APPROVAL with Foreman review",
            action_type,
            agent_type,
        )
        return ActionClassification(
            tier=ActionTier.REQUIRE_APPROVAL,
            action_type=action_type,
            rationale=(
                f"Action '{action_type}' crosses organizational or compliance "
                "boundaries and requires both human approval and Foreman review "
                "before execution."
            ),
            requires_foreman_review=True,
        )

    @staticmethod
    def _default_classification(action_type: str, agent_type: str) -> ActionClassification:
        """Build the fail-closed classification for unknown actions."""
        logger.info(
            "Action %r for agent %s classified as REQUIRE_APPROVAL (default for unknown action)",
            action_type,
            agent_type,
        )
        return ActionClassification(
            tier=ActionTier.REQUIRE_APPROVAL,
            action_type=action_type,
            rationale=(
                f"Action '{action_type}' is not in the allow or deny lists. "
                "Unknown actions default to REQUIRE_APPROVAL to ensure a human "
                "reviews the intent before execution proceeds."
            ),
            requires_foreman_review=False,
        )

    def _classify_internal(
        self,
        action_type: str,
        agent_type: str,
        overrides: dict[str, ActionTier] | None,
        context: dict[str, Any] | None,
    ) -> ActionClassification:
        """Route an action through deny-set → override-map → allow/foreman/default.

        The evaluation order is:
        1. DENY_ACTIONS (absolute, cannot be overridden)
        2. Caller overrides
        3. ALLOW_ACTIONS
        4. FOREMAN_REVIEW_ACTIONS (REQUIRE_APPROVAL + foreman flag)
        5. Default: REQUIRE_APPROVAL

        Args:
            action_type: The action string to classify.
            agent_type: Agent type string for logging context.
            overrides: Optional per-context tier override map.
            context: Optional metadata dict (not used for routing, carried
                through for callers who inspect the returned rationale).

        Returns:
            Fully populated :class:`ActionClassification`.
        """
        normalised = action_type.strip().lower()

        if normalised in DENY_ACTIONS:
            return self._deny_classification(action_type, agent_type)

        override = self._override_classification(action_type, agent_type, normalised, overrides)
        if override is not None:
            return override

        if normalised in ALLOW_ACTIONS:
            return self._allow_classification(action_type, agent_type)

        if normalised in FOREMAN_REVIEW_ACTIONS:
            return self._foreman_review_classification(action_type, agent_type)

        return self._default_classification(action_type, agent_type)


# -- Singleton -----------------------------------------------------------------
# Protects initialisation against concurrent first-access from multiple threads.
_classifier: ActionClassifier | None = None
_classifier_lock = threading.Lock()


def get_action_classifier() -> ActionClassifier:
    """Return the global :class:`ActionClassifier` singleton.

    Uses double-checked locking so concurrent callers never construct two
    instances. The classifier holds no mutable state, so a single shared
    instance is safe across threads.

    Returns:
        The module-level :class:`ActionClassifier` instance.
    """
    global _classifier
    if _classifier is None:
        with _classifier_lock:
            if _classifier is None:
                _classifier = ActionClassifier()
    return _classifier


def reset_action_classifier() -> None:
    """Replace the singleton with a fresh instance.

    Intended for test teardown only. Production code should call
    :func:`get_action_classifier` and never reset the instance.
    """
    global _classifier
    with _classifier_lock:
        _classifier = None
