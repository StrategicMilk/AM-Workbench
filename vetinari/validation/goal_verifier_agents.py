"""Agent-backed checks for goal verification."""

from __future__ import annotations

import logging
from typing import Any

from vetinari.constants import TRUNCATE_CONTENT_ANALYSIS
from vetinari.guards import GateError
from vetinari.security.redaction import redact_text
from vetinari.types import AgentType

logger = logging.getLogger(__name__)


def llm_goal_evaluation(
    goal: str,
    final_output: str,
    required_features: list[str],
    things_to_avoid: list[str],
) -> dict[str, Any] | None:
    """Use the Inspector agent for goal compliance checks.

    Args:
        goal: Goal value consumed by llm_goal_evaluation().
        final_output: Final output value consumed by llm_goal_evaluation().
        required_features: Required features value consumed by llm_goal_evaluation().
        things_to_avoid: Things to avoid value consumed by llm_goal_evaluation().

    Returns:
        Value produced for the caller.

    Raises:
        Exception: Propagates unrecoverable Inspector task construction failures.
        GoalVerificationError: If the Inspector result has an unexpected shape.
    """
    try:
        from vetinari.agents import get_inspector_agent
        from vetinari.agents.contracts import AgentTask

        evaluator = get_inspector_agent()
        features_str = (
            "\n".join(f"- {feature}" for feature in required_features) if required_features else "None specified"
        )
        avoid_str = "\n".join(f"- {avoid}" for avoid in things_to_avoid) if things_to_avoid else "None specified"
        safe_goal = redact_text(goal)
        safe_features = redact_text(features_str)
        safe_avoid = redact_text(avoid_str)
        safe_output = redact_text(final_output[:3000])

        task = AgentTask(
            task_id="goal_verification",
            agent_type=AgentType.INSPECTOR,
            description="Verify deliverable against goal",
            prompt=f"""Verify this deliverable against the original goal.

GOAL: {safe_goal}

REQUIRED FEATURES:
{safe_features}

THINGS TO AVOID:
{safe_avoid}

DELIVERABLE (first 3000 chars):
{safe_output}

For each required feature, check if it's implemented. Return JSON:
{{
  "verdict": "pass|fail|partial",
  "quality_score": 0.0-1.0,
  "feature_checks": [
    {{"feature": "...", "implemented": true/false, "confidence": 0.0-1.0, "evidence": "..."}}
  ],
  "improvements": ["..."],
  "summary": "..."
}}""",
            context={},
        )
        result = evaluator.execute(task)
        if result.success and isinstance(result.output, dict):
            return result.output
    except Exception as exc:
        logger.warning("LLM evaluation in goal verifier failed: %s", exc)
    return None


def security_check(final_output: str, task_outputs: list[dict[str, Any]]) -> tuple[bool, list[dict[str, Any]], float]:
    """Run the Inspector agent security review for final output.

    Args:
        final_output: Final output value consumed by security_check().
        task_outputs: Task outputs value consumed by security_check().

    Returns:
        Value produced for the caller.

    Raises:
        Exception: Propagates unrecoverable Inspector task construction failures.
    """
    try:
        from vetinari.agents import get_inspector_agent
        from vetinari.agents.contracts import AgentTask

        auditor = get_inspector_agent()
        task = AgentTask(
            task_id="goal_security_check",
            agent_type=AgentType.INSPECTOR,
            description="Security review of final deliverable",
            prompt=redact_text(final_output[:TRUNCATE_CONTENT_ANALYSIS]),
            context={},
        )
        result = auditor.execute(task)
        if result.success and isinstance(result.output, dict):
            missing = [key for key in ("score", "findings") if key not in result.output]
            if missing:
                raise GateError(
                    "security_check",
                    f"result dict missing required keys: {', '.join(missing)}",
                )
            findings = result.output["findings"]
            score = result.output["score"] / 100.0
            critical = [finding for finding in findings if finding.get("severity") in ("critical", "high")]
            return len(critical) == 0, findings, score
    except GateError:
        raise
    except Exception as exc:
        logger.warning("Security check failed - failing closed: %s", exc)
        return False, [{"severity": "critical", "description": f"Security check error: {exc}"}], 0.0

    return False, [{"severity": "critical", "description": "Security check returned no result"}], 0.0


__all__ = ["llm_goal_evaluation", "security_check"]
