"""Agent capability enforcement for Vetinari agents.

Validates that a required capability is present in the agent's capability
list as defined in ``AgentSpec.capabilities``.
"""

from __future__ import annotations

import logging

from vetinari.agents.contracts import get_agent_spec
from vetinari.exceptions import CapabilityNotAvailable
from vetinari.types import AgentType

logger = logging.getLogger(__name__)


class AgentCapabilityEnforcer:
    """Validates required capabilities against the agent's capability list in AgentSpec.

    Reads ``AgentSpec.capabilities`` for the given agent type and raises
    ``CapabilityNotAvailable`` when the requested capability is absent.

    Example:
        >>> enforcer = AgentCapabilityEnforcer()
        >>> enforcer.validate(AgentType.WORKER, "code_scaffolding")  # passes
        >>> enforcer.validate(AgentType.WORKER, "ontological_analysis")  # raises
    """

    def validate(self, agent_type: AgentType, required_capability: str) -> None:
        """Validate that the agent exposes the required capability.

        Args:
            agent_type: The agent type whose specification provides the
                capabilities list.
            required_capability: The capability string that must be present in
                ``AgentSpec.capabilities``.

        Raises:
            CapabilityNotAvailable: If required_capability is not in
                spec.capabilities.
        """
        spec = get_agent_spec(agent_type)
        if spec is None:
            # Unknown agent types are denied, not skipped — fail closed per security policy.
            raise CapabilityNotAvailable(
                f"Agent {agent_type.value!r} has no registered AgentSpec — "
                "capability validation cannot proceed; request denied.",
                agent_type=agent_type.value,
                required_capability=required_capability,
                available_capabilities=[],
            )

        capabilities = spec.capabilities
        if required_capability not in capabilities:
            raise CapabilityNotAvailable(
                f"Agent {agent_type.value!r} does not support capability "
                f"{required_capability!r}. "
                f"Available capabilities: {capabilities}. "
                "Route the request to an agent that provides this capability.",
                agent_type=agent_type.value,
                required_capability=required_capability,
                available_capabilities=capabilities,
            )

        from vetinari.envs import tier_for_capability

        tier_for_capability(required_capability)

        logger.debug(
            "Capability check passed for %s: %r is available",
            agent_type.value,
            required_capability,
        )
