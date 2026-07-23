"""Quality Skill Tool — internal component of InspectorSkillTool.

Provides quality review modes with comprehensive heuristic pattern scanning.
This module is an *internal component* of InspectorSkillTool (the primary
Inspector skill tool in the 3-agent factory pipeline, ADR-0061).
InspectorSkillTool delegates supplementary analysis calls here.

Direct usage is supported for backwards compatibility but all new code
should go through ``InspectorSkillTool(mode="code_review", ...)``.

Modes:
  - code_review: Code quality, design patterns, maintainability
  - security_audit: Vulnerability detection with 45+ heuristic patterns
  - test_generation: pytest-aware test generation
  - simplification: Code simplification and refactoring
  - performance_review: Performance analysis
  - best_practices: Project-specific VET rule checking

This module defines a security pattern scanner. The patterns listed in
SECURITY_PATTERNS are vulnerability signatures to DETECT in user code,
not patterns this module itself uses.
"""

from __future__ import annotations

import logging
from typing import Any

from vetinari.execution_context import ToolPermission
from vetinari.skills.quality_skill_contracts import (
    SECURITY_PATTERNS,
    QualityIssue,
    QualityMode,
    QualityResult,
)
from vetinari.skills.quality_skill_modes import _QualitySkillModeMixin
from vetinari.skills.quality_skill_patterns import OWASP_TOP_10
from vetinari.tool_interface import (
    Tool,
    ToolCategory,
    ToolMetadata,
    ToolParameter,
    ToolResult,
)
from vetinari.types import ExecutionMode, QualityGrade, SeverityLevel, ThinkingMode  # canonical enums

logger = logging.getLogger(__name__)


class QualitySkillTool(_QualitySkillModeMixin, Tool):
    """Unified tool for the QUALITY consolidated agent.

    Replaces: EvaluatorSkillTool, SecurityAuditorSkill, TestAutomationSkill.

    Provides code review, security auditing, test generation, performance
    review, and simplification through a standardized Tool interface.
    """

    def __init__(self) -> None:
        metadata = ToolMetadata(
            name="quality",
            description=(
                "Code review, security audit, test generation, performance review, "
                "and simplification. Use for any code quality assessment."
            ),
            category=ToolCategory.SEARCH_ANALYSIS,
            version="1.1.0",
            author="Vetinari",
            parameters=[
                ToolParameter(
                    name="mode",
                    type=str,
                    description="Quality mode to use",
                    required=True,
                    allowed_values=[m.value for m in QualityMode],
                ),
                ToolParameter(
                    name="code",
                    type=str,
                    description="Code to review, audit, or generate tests for",
                    required=True,
                ),
                ToolParameter(
                    name="context",
                    type=str,
                    description="File path, PR description, or context",
                    required=False,
                ),
                ToolParameter(
                    name="thinking_mode",
                    type=str,
                    description="Review depth (none/low/medium/high/xhigh/max)",
                    required=False,
                    default="medium",
                    allowed_values=[m.value for m in ThinkingMode],
                ),
                ToolParameter(
                    name="focus_areas",
                    type=list,
                    description="Specific areas to prioritize",
                    required=False,
                ),
                ToolParameter(
                    name="severity_threshold",
                    type=str,
                    description="Minimum severity to report",
                    required=False,
                    default="low",
                    allowed_values=[s.value for s in SeverityLevel],
                ),
            ],
            required_permissions=[
                ToolPermission.FILE_READ,
                ToolPermission.MODEL_INFERENCE,
            ],
            allowed_modes=[ExecutionMode.EXECUTION, ExecutionMode.PLANNING],
            tags=["quality", "review", "security", "testing", "audit", "performance"],
        )
        super().__init__(metadata)

    def execute(self, **kwargs: Any) -> ToolResult:
        """Execute a quality operation in the requested mode.

        Returns:
            ToolResult whose output is the QualityResult dict; metadata
            includes mode, thinking_mode, issues_found count, and quality_grade.
        """
        try:
            mode_str = kwargs.get("mode")
            code = kwargs.get("code")
            context = kwargs.get("context")
            thinking_mode_str = kwargs.get("thinking_mode", "medium")
            focus_areas = kwargs.get("focus_areas", [])
            severity_threshold_str = kwargs.get("severity_threshold", "low")

            if not code:
                return ToolResult(success=False, output=None, error="code parameter is required")

            try:
                mode = QualityMode(mode_str)
            except ValueError:
                logger.warning("Invalid QualityMode %r in tool call — returning error to caller", mode_str)
                return ToolResult(success=False, output=None, error=f"Invalid mode: {mode_str}")

            try:
                thinking_mode = ThinkingMode(thinking_mode_str)
            except ValueError:
                logger.warning(
                    "Invalid ThinkingMode %r in quality tool call — returning error to caller",
                    thinking_mode_str,
                )
                return ToolResult(success=False, output=None, error=f"Invalid thinking_mode: {thinking_mode_str}")

            result = self._run_mode(mode, code, context, thinking_mode, focus_areas)

            severity_order = list(SeverityLevel)
            try:
                threshold = SeverityLevel(severity_threshold_str)
                threshold_idx = severity_order.index(threshold)
                result.issues = [i for i in result.issues if severity_order.index(i.severity) <= threshold_idx]
            except ValueError:
                logger.warning("Invalid severity threshold value: %s", severity_threshold_str, exc_info=True)

            return ToolResult(
                success=result.success,
                output=result.to_dict(),
                error=None if result.success else "Quality assessment failed",
                metadata={
                    "mode": mode.value,
                    "thinking_mode": thinking_mode.value,
                    "issues_found": len(result.issues),
                    "quality_grade": result.grade.value if result.grade else None,
                },
            )
        except Exception as e:
            logger.error("Quality tool failed: %s", e, exc_info=True)
            return ToolResult(success=False, output=None, error=str(e))


__all__ = [
    "OWASP_TOP_10",
    "SECURITY_PATTERNS",
    "QualityGrade",
    "QualityIssue",
    "QualityMode",
    "QualityResult",
    "QualitySkillTool",
    "SeverityLevel",
    "ThinkingMode",
]
