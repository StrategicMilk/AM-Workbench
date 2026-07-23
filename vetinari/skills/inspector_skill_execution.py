"""Execution helper mixin for InspectorSkillTool."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from vetinari.agents.contracts import OutcomeSignal
from vetinari.skills.inspector_skill_types import InspectorMode, InspectorResult, _inspector_result_to_signal
from vetinari.tool_interface import ToolResult
from vetinari.types import AgentType

logger = logging.getLogger("vetinari.skills.inspector_skill")


class _InspectorExecutionMixin:
    """Provide public execution flow for InspectorSkillTool."""

    if TYPE_CHECKING:
        _code_review: Any
        _security_audit: Any
        _simplification: Any
        _test_generation: Any

    def execute(self, **kwargs: Any) -> ToolResult:
        """Execute the Inspector skill in the specified mode.

        Args:
            **kwargs: Must include 'code'; optionally 'mode', 'context', 'focus_areas'.

        Returns:
            ToolResult containing the Inspector's review result.
        """
        code = kwargs.get("code", "")
        mode_str = kwargs.get("mode", "code_review")
        context = kwargs.get("context", {})
        focus_areas = kwargs.get("focus_areas", [])

        try:
            mode = InspectorMode(mode_str)
        except ValueError:
            logger.warning("Invalid InspectorMode %r in tool call - returning error to caller", mode_str)
            return ToolResult(
                success=False,
                output=None,
                error=f"Unknown mode: {mode_str}. Valid modes: {[m.value for m in InspectorMode]}",
            )

        logger.info("Inspector executing mode=%s code_length=%d", mode.value, len(code))

        # Check self_check_passed from context (Phase 5.24 integration)
        self_check_passed = context.get("self_check_passed")
        self_check_issues = context.get("self_check_issues", [])
        if self_check_passed is False:
            logger.info(
                "Inspector: self_check failed with %d issues - applying deeper review",
                len(self_check_issues),
            )

        try:
            result = self._execute_mode(mode, code, context, focus_areas)
            # Incorporate self_check results into gate decision
            if self_check_passed is not None:
                result.self_check_passed = self_check_passed
                if not self_check_passed and result.passed:
                    # Self-check failed but review passed - add advisory
                    result.suggestions.append(
                        "Agent self-check flagged issues that were not caught in review: "
                        + "; ".join(self_check_issues[:3]),
                    )

            logger.info(
                "Inspector completed mode=%s passed=%s grade=%s score=%.2f",
                mode.value,
                result.passed,
                result.grade,
                result.score,
            )

            # Build OutcomeSignal from heuristic result so callers get a
            # provenance-bearing verdict rather than a bare pass/fail dict.
            outcome = _inspector_result_to_signal(result, mode.value)

            return _build_tool_result(mode, result, outcome)
        except Exception as exc:
            logger.error("Inspector mode=%s failed: %s", mode.value, exc)
            return ToolResult(success=False, output=None, error=str(exc))

    def _execute_mode(
        self,
        mode: InspectorMode,
        code: str,
        context: dict[str, Any],
        focus_areas: list[str],
    ) -> InspectorResult:
        """Route to the appropriate mode handler.

        Args:
            mode: The review mode.
            code: Code or content to review.
            context: Review context.
            focus_areas: Specific areas to focus on.

        Returns:
            InspectorResult from the mode handler.
        """
        handler = {
            InspectorMode.CODE_REVIEW: self._code_review,
            InspectorMode.SECURITY_AUDIT: self._security_audit,
            InspectorMode.TEST_GENERATION: self._test_generation,
            InspectorMode.SIMPLIFICATION: self._simplification,
        }[mode]
        return handler(code, context, focus_areas)


def _build_tool_result(mode: InspectorMode, result: InspectorResult, outcome: OutcomeSignal) -> ToolResult:
    """Build the ToolResult wrapper for an InspectorResult.

    Args:
        mode: Executed Inspector mode.
        result: Inspector mode result.
        outcome: Provenance-bearing outcome signal.

    Returns:
        ToolResult with the public InspectorSkillTool metadata shape.
    """
    return ToolResult(
        success=True,
        output=result.to_dict(),
        metadata={
            "mode": mode.value,
            "agent": AgentType.INSPECTOR.value,
            "passed": result.passed,
            "grade": result.grade,
            "outcome_signal": {
                "passed": outcome.passed,
                "score": outcome.score,
                "basis": outcome.basis.value,
                "issues": list(outcome.issues),
            },
        },
    )
