"""Output schema validation for the AgentGraph.

Validates agent outputs against their SkillSpec output_schema after a
successful execution, providing lightweight structural checks (required
keys, value types) and optional Pydantic validation when available.

This runs as part of step 3 of the execution flow after a task succeeds
but before the maker-checker quality gate.
"""

from __future__ import annotations

import logging
from typing import Any

from vetinari.types import AgentType

logger = logging.getLogger(__name__)


# Maps JSON Schema type names to Python types for structural validation.
_TYPE_MAP: dict[str, type | tuple[type, ...]] = {
    "string": str,
    "number": (int, float),
    "integer": int,
    "boolean": bool,
    "array": list,
    "object": dict,
}


class GraphValidationEngine:
    """Output schema validation methods for AgentGraph.

    Validates agent outputs against SkillSpec schemas after successful
    execution. Mixed into AgentGraph alongside the planner, executor,
    and recovery mixins.

    Attributes expected on ``self``:
        (none beyond standard AgentGraph state)
    """

    def _validate_output_schema(self, agent_type: AgentType, output: Any) -> list[str]:
        """Validate agent output against the SkillSpec output_schema.

        Performs a lightweight structural check — verifies required keys are
        present and value types match, without a full JSON Schema library
        dependency. Optionally runs Pydantic validation if a schema model
        is registered for the agent's mode.

        Args:
            agent_type: The AgentType whose SkillSpec schema to validate against.
            output: The output value produced by the agent.

        Returns:
            List of validation issue strings. Empty list means output is valid.
        """
        spec = self.get_skill_spec(agent_type)
        if spec is None:
            return [f"Missing SkillSpec for agent type: {agent_type.value}"]
        if not spec.output_schema:
            return [f"Missing output schema for agent type: {agent_type.value}"]

        schema = spec.output_schema
        if not isinstance(output, dict):
            issues = [f"Output for {agent_type.value} must be an object, got {type(output).__name__}"]
            required = schema.get("required", [])
            issues.extend(f"Missing required output field: '{key}'" for key in required)
            return issues

        required = schema.get("required", [])
        properties = schema.get("properties", {})

        issues = [f"Missing required output field: '{key}'" for key in required if key not in output]

        for key, prop_schema in properties.items():
            if key not in output:
                continue
            expected_type = prop_schema.get("type")
            if expected_type and expected_type in _TYPE_MAP:
                py_type = _TYPE_MAP[expected_type]
                if not isinstance(output[key], py_type):
                    issues.append(f"Field '{key}' expected type {expected_type}, got {type(output[key]).__name__}")

        if issues:
            logger.debug(
                "[AgentGraph] Output schema issues for %s: %s",
                agent_type.value,
                "; ".join(issues),
            )

        # Secondary: try Pydantic schema validation if available
        try:
            from vetinari.schemas import get_schema_for_mode

            mode = spec.name if spec else ""
            pydantic_schema = get_schema_for_mode(mode)
            if pydantic_schema and isinstance(output, dict):
                try:
                    pydantic_schema(**output)
                except Exception as val_err:
                    issues.append(f"Pydantic validation: {val_err}")
        except Exception as exc:
            issues.append(f"Pydantic schema validation unavailable: {exc}")

        return issues
