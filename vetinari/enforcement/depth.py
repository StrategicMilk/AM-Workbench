"""Delegation depth enforcement for Vetinari agents.

Validates that an agent's delegation depth does not exceed the maximum
configured in its ``AgentSpec.max_delegation_depth``.
"""

from __future__ import annotations

import logging

from vetinari.agents.contracts import get_agent_spec
from vetinari.exceptions import DelegationDepthExceeded
from vetinari.guards import GateError
from vetinari.reliability_guards import require_not_none
from vetinari.types import AgentType

logger = logging.getLogger(__name__)


class DelegationDepthValidator:
    """Validates agent delegation depth against the limit in AgentSpec.

    Reads ``AgentSpec.max_delegation_depth`` for the given agent type and
    raises ``DelegationDepthExceeded`` if the current depth exceeds it.

    Example:
        >>> validator = DelegationDepthValidator()
        >>> validator.validate(AgentType.WORKER, current_depth=1)  # passes
        >>> validator.validate(AgentType.WORKER, current_depth=5)  # raises
    """

    def validate(self, agent_type: AgentType, current_depth: int) -> None:
        """Validate that current_depth does not exceed the agent's max delegation depth.

        Args:
            agent_type: The agent type whose specification provides the depth limit.
            current_depth: The delegation depth at which the agent is being called.

        Raises:
            DelegationDepthExceeded: If current_depth > spec.max_delegation_depth.
        """
        if not isinstance(agent_type, AgentType):
            raise GateError(
                "delegation_depth",
                f"unknown agent type: {agent_type!r} - add to AgentType enum before use",
            )
        spec = require_not_none(get_agent_spec(agent_type), label=f"AgentSpec[{agent_type!r}]")

        max_depth = spec.max_delegation_depth
        if current_depth > max_depth:
            raise DelegationDepthExceeded(
                f"Agent {agent_type.value!r} reached delegation depth {current_depth}, "
                f"which exceeds its maximum of {max_depth}. "
                "Reduce task nesting or increase max_delegation_depth in AgentSpec.",
                agent_type=agent_type.value,
                current_depth=current_depth,
                max_depth=max_depth,
            )

        logger.debug(
            "Depth check passed for %s: %d/%d",
            agent_type.value,
            current_depth,
            max_depth,
        )
