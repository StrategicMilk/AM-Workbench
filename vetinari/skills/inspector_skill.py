"""Inspector Skill Tool.

==============================
Skill tool for the INSPECTOR agent - independent quality gate.

Covers 4 modes:
  - code_review: 5-pass review (correctness, style, security, perf, maintainability)
  - security_audit: OWASP Top 10, CWE mapping, secrets detection
  - test_generation: Coverage analysis, gap-filling test generation
  - simplification: Dead code detection, complexity reduction

The Inspector is the final gate in the factory pipeline. Its decisions
cannot be overridden by any other agent - only humans can bypass the gate.

Standards enforced (from skill_registry):
  - STD-INS-001: 5-dimension review coverage
  - STD-INS-002: OWASP Top 10 + CWE mapping
  - STD-INS-003: Credential/secrets scanning
  - STD-INS-004: Happy/edge/error test coverage
  - STD-INS-005: Severity + actionable descriptions
  - STD-INS-006: Read-only constraint
  - STD-INS-007: Objective gate criteria
  - STD-INS-008: Human-only gate override
"""

from __future__ import annotations

from typing import Any

from vetinari.execution_context import ToolPermission
from vetinari.skills.inspector_skill_execution import _InspectorExecutionMixin
from vetinari.skills.inspector_skill_modes import _InspectorModeHandlersMixin
from vetinari.skills.inspector_skill_quality import _InspectorQualityMixin
from vetinari.skills.inspector_skill_types import (
    _FUNC_DEF_RE,
    InspectorMode,
    InspectorResult,
    ReviewIssue,
    _inspector_result_to_signal,
)
from vetinari.tool_interface import (
    Tool,
    ToolCategory,
    ToolMetadata,
    ToolParameter,
)
from vetinari.types import ExecutionMode

__all__ = [
    "_FUNC_DEF_RE",
    "InspectorMode",
    "InspectorResult",
    "InspectorSkillTool",
    "ReviewIssue",
    "_inspector_result_to_signal",
]

for _public_symbol in (InspectorMode, InspectorResult, ReviewIssue, _inspector_result_to_signal):
    _public_symbol.__module__ = __name__
del _public_symbol


class InspectorSkillTool(_InspectorExecutionMixin, _InspectorModeHandlersMixin, _InspectorQualityMixin, Tool):
    """Skill tool for the Inspector agent - independent quality gate.

    The Inspector is the last stage of the factory pipeline. It performs
    read-only review of Worker output and issues pass/fail gate decisions
    that cannot be overridden by any other agent. Only humans can bypass
    the Inspector's gate.

    QualitySkillTool is used as a supplementary checker: Inspector heuristics
    run first and remain the primary gate; Quality findings are merged in
    afterwards as additive signal only.
    """

    def __init__(self) -> None:
        """Initialize InspectorSkillTool metadata and lazy helper state."""
        self._quality_tool: Any = None  # lazily populated by _get_quality_tool()
        super().__init__(
            metadata=ToolMetadata(
                name="inspector",
                description=(
                    "Independent quality gate - code review, security audit, test generation, and code simplification"
                ),
                category=ToolCategory.SEARCH_ANALYSIS,
                version="2.0.0",
                parameters=[
                    ToolParameter(
                        name="code",
                        type=str,
                        description="Code or content to review",
                        required=True,
                    ),
                    ToolParameter(
                        name="mode",
                        type=str,
                        description="Review mode",
                        required=False,
                        default="code_review",
                        allowed_values=[m.value for m in InspectorMode],
                    ),
                    ToolParameter(
                        name="context",
                        type=dict,
                        description="Review context (PR description, self_check results)",
                        required=False,
                    ),
                    ToolParameter(
                        name="focus_areas",
                        type=list,
                        description="Specific areas to focus the review on",
                        required=False,
                    ),
                    ToolParameter(
                        name="thinking_mode",
                        type=str,
                        description="Thinking budget tier",
                        required=False,
                        allowed_values=["none", "low", "medium", "high", "xhigh", "max"],
                    ),
                ],
                required_permissions=[
                    ToolPermission.FILE_READ,
                    ToolPermission.MODEL_INFERENCE,
                ],
                allowed_modes=[ExecutionMode.EXECUTION],
                tags=["quality", "security", "review", "gate"],
            ),
        )
