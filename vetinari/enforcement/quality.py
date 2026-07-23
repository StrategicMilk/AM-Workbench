"""Quality gate enforcement for Vetinari agents.

Validates that an agent's output quality score meets the minimum threshold
configured in its ``AgentSpec.quality_gate_score``.
"""

from __future__ import annotations

import logging

from vetinari.agents.contracts import get_agent_spec
from vetinari.exceptions import JurisdictionViolation, QualityGateFailed
from vetinari.types import AgentType

logger = logging.getLogger(__name__)


class QualityGateEnforcer:
    """Validates agent output quality scores against the threshold in AgentSpec."""

    def validate(self, agent_type: AgentType, quality_score: float) -> None:
        """Validate that quality_score meets the agent's minimum threshold.

        Args:
            agent_type: Agent type whose specification provides the threshold.
            quality_score: Produced or assigned quality score, in the range 0.0-1.0.

        Raises:
            JurisdictionViolation: If the agent has no registered AgentSpec.
            QualityGateFailed: If quality_score is below spec.quality_gate_score.
        """
        spec = get_agent_spec(agent_type)
        if spec is None:
            raise JurisdictionViolation(
                f"Agent {agent_type.value!r} has no AgentSpec; quality gate is unknown.",
                agent_type=agent_type.value,
                file_path="quality_gate",
                jurisdiction=(),
            )

        threshold = spec.quality_gate_score
        if quality_score < threshold:
            raise QualityGateFailed(
                f"Agent {agent_type.value!r} quality score {quality_score:.3f} is below "
                f"the required threshold of {threshold:.3f}. "
                "Improve output quality or lower quality_gate_score in AgentSpec.",
                agent_type=agent_type.value,
                quality_score=quality_score,
                threshold=threshold,
            )

        logger.debug("Quality gate passed for %s: %.3f >= %.3f", agent_type.value, quality_score, threshold)
