"""Operations Skill Tool — internal component of WorkerSkillTool.

Operations-focused modes providing documentation, synthesis, cost analysis,
and improvement capabilities. This module is an *internal component* of
WorkerSkillTool (the primary Worker skill tool in the 3-agent factory
pipeline, ADR-0061). WorkerSkillTool delegates operations mode group
calls here.

Direct usage is supported for backwards compatibility but all new code
should go through ``WorkerSkillTool(mode="documentation", ...)``.

Modes:
  - documentation: Documentation generation (Overview, Usage, API, Config)
  - creative_writing: Creative content generation
  - cost_analysis: Cost estimation and optimization
  - experiment: Experiment design (Hypothesis, Methodology, Success Criteria)
  - error_recovery: Error RCA and fix steps
  - synthesis: Multi-source information synthesis
  - image_generation: Image generation requests
  - improvement: PDCA-based improvement identification
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from enum import Enum
from html import escape
from typing import Any

from vetinari.execution_context import ToolPermission
from vetinari.security.redaction import redact_text
from vetinari.tool_interface import (
    Tool,
    ToolCategory,
    ToolMetadata,
    ToolParameter,
    ToolResult,
)
from vetinari.types import (
    ExecutionMode,
    ThinkingMode,  # canonical enum from types.py
)
from vetinari.utils.serialization import dataclass_to_dict

logger = logging.getLogger(__name__)


def _log_ref(text: str) -> str:
    """Return bounded redacted text for operational logs."""
    return redact_text(str(text))[:120]


class OperationsMode(str, Enum):
    """Modes of the unified operations skill."""

    DOCUMENTATION = "documentation"
    CREATIVE_WRITING = "creative_writing"
    COST_ANALYSIS = "cost_analysis"
    EXPERIMENT = "experiment"
    ERROR_RECOVERY = "error_recovery"
    SYNTHESIS = "synthesis"
    IMAGE_GENERATION = "image_generation"
    IMPROVEMENT = "improvement"


class OutputFormat(str, Enum):
    """Output format options."""

    MARKDOWN = "markdown"
    HTML = "html"
    PLAIN = "plain"
    JSON = "json"


@dataclass(frozen=True, slots=True)
class OperationsRequest:
    """Request structure for operations."""

    mode: OperationsMode
    content: str
    context: str | None = None
    output_format: OutputFormat = OutputFormat.MARKDOWN
    thinking_mode: ThinkingMode = ThinkingMode.MEDIUM

    def __repr__(self) -> str:
        """Show key identifying fields for debugging."""
        return f"OperationsRequest(mode={self.mode!r}, output_format={self.output_format!r})"

    def to_dict(self) -> dict[str, Any]:
        """Convert to a plain dictionary for JSON output; enum fields become their string values."""
        return dataclass_to_dict(self)


@dataclass(frozen=True, slots=True)
class Section:
    """A section of generated content."""

    title: str
    content: str
    order: int = 0

    def to_dict(self) -> dict[str, Any]:
        """Convert to a plain dictionary for JSON output."""
        return dataclass_to_dict(self)


@dataclass
class OperationsResult:
    """Result of an operations task."""

    success: bool
    content: str | None = None
    content_type: str | None = None
    output_format: OutputFormat = OutputFormat.MARKDOWN
    sections: list[Section] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def __repr__(self) -> str:
        """Show key identifying fields for debugging."""
        return (
            f"OperationsResult(success={self.success!r},"
            f" content_type={self.content_type!r},"
            f" output_format={self.output_format!r})"
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize this OperationsResult to a plain dictionary suitable for JSON output.

        Returns:
            Dictionary containing the result status, generated content,
            sections, metadata, and any warnings.
        """
        return {
            "success": self.success,
            "content": self.content,
            "type": self.content_type,
            "format": self.output_format.value,
            "sections": [s.to_dict() for s in self.sections],
            "metadata": self.metadata,
            "warnings": self.warnings,
        }


class OperationsSkillTool(Tool):
    """Unified tool for the OPERATIONS consolidated agent.

    Replaces: SynthesizerSkillTool, DocumentationSkill, CostPlannerSkill,
              ExperimentationManagerSkill, ImprovementSkill,
              ErrorRecoverySkill, ImageGeneratorSkill.

    Provides documentation, synthesis, cost analysis, experimentation,
    error recovery, and improvement capabilities.
    """

    def __init__(self) -> None:
        metadata = ToolMetadata(
            name="operations",
            description=(
                "Documentation, synthesis, cost analysis, experiments, error recovery, "
                "improvement, and creative writing. Use for operational and support tasks."
            ),
            category=ToolCategory.SEARCH_ANALYSIS,
            version="1.1.0",
            author="Vetinari",
            parameters=[
                ToolParameter(
                    name="mode",
                    type=str,
                    description="Operations mode to use",
                    required=True,
                    allowed_values=[m.value for m in OperationsMode],
                ),
                ToolParameter(
                    name="content",
                    type=str,
                    description="Input content to process",
                    required=True,
                ),
                ToolParameter(
                    name="context",
                    type=str,
                    description="Additional context",
                    required=False,
                ),
                ToolParameter(
                    name="output_format",
                    type=str,
                    description="Output format (markdown/html/plain/json)",
                    required=False,
                    default="markdown",
                    allowed_values=[f.value for f in OutputFormat],
                ),
                ToolParameter(
                    name="thinking_mode",
                    type=str,
                    description="Operations depth (none/low/medium/high/xhigh/max)",
                    required=False,
                    default="medium",
                    allowed_values=[m.value for m in ThinkingMode],
                ),
            ],
            required_permissions=[
                ToolPermission.FILE_READ,
                ToolPermission.FILE_WRITE,
                ToolPermission.MODEL_INFERENCE,
            ],
            allowed_modes=[ExecutionMode.EXECUTION, ExecutionMode.PLANNING],
            tags=[
                "documentation",
                "creative",
                "operations",
                "cost",
                "recovery",
                "synthesis",
                "improvement",
            ],
        )
        super().__init__(metadata)

    def execute(self, **kwargs: Any) -> ToolResult:
        """Execute an operations task.

        Returns:
            The ToolResult result.
        """
        try:
            mode_str = kwargs.get("mode")
            content = kwargs.get("content")
            context = kwargs.get("context")
            format_str = kwargs.get("output_format", "markdown")
            thinking_str = kwargs.get("thinking_mode", "medium")

            if not content:
                return ToolResult(success=False, output=None, error="content parameter is required")

            try:
                mode = OperationsMode(mode_str)
            except ValueError:
                logger.warning("Invalid OperationsMode %r in tool call — returning error to caller", mode_str)
                return ToolResult(success=False, output=None, error=f"Invalid mode: {mode_str}")

            try:
                output_format = OutputFormat(format_str)
            except ValueError:
                logger.warning("Invalid output_format %r in tool call; returning error to caller", format_str)
                return ToolResult(success=False, output=None, error=f"Invalid output_format: {format_str!r}")

            try:
                thinking_mode = ThinkingMode(thinking_str)
            except ValueError:
                logger.warning("Invalid thinking_mode %r in tool call; returning error to caller", thinking_str)
                return ToolResult(success=False, output=None, error=f"Invalid thinking_mode: {thinking_str!r}")

            request = OperationsRequest(
                mode=mode,
                content=content,
                context=context,
                output_format=output_format,
                thinking_mode=thinking_mode,
            )

            result = self._run_mode(request)
            result.content = self._render_content(result, output_format)

            return ToolResult(
                success=result.success,
                output=result.to_dict(),
                error=None if result.success else "Operations task failed",
                metadata={
                    "mode": mode.value,
                    "output_format": output_format.value,
                    "sections_count": len(result.sections),
                },
            )
        except Exception as e:
            logger.error("Operations tool failed: %s", e, exc_info=True)
            return ToolResult(success=False, output=None, error=str(e))

    def _run_mode(self, request: OperationsRequest) -> OperationsResult:
        """Route to the appropriate operations mode."""
        dispatch = {
            OperationsMode.DOCUMENTATION: self._documentation,
            OperationsMode.CREATIVE_WRITING: self._creative_writing,
            OperationsMode.COST_ANALYSIS: self._cost_analysis,
            OperationsMode.EXPERIMENT: self._experiment,
            OperationsMode.ERROR_RECOVERY: self._error_recovery,
            OperationsMode.SYNTHESIS: self._synthesis,
            OperationsMode.IMAGE_GENERATION: self._image_generation,
            OperationsMode.IMPROVEMENT: self._improvement,
        }
        handler = dispatch.get(request.mode)
        if handler is None:
            return OperationsResult(success=False, content=f"Unknown mode: {request.mode.value}")
        return handler(request)

    @staticmethod
    def _render_content(result: OperationsResult, output_format: OutputFormat) -> str | None:
        """Render sectioned output into the requested serialization format."""
        if result.content is None or not result.sections:
            return result.content
        if output_format == OutputFormat.MARKDOWN:
            return result.content
        if output_format == OutputFormat.HTML:
            return "\n".join(
                f"<section><h2>{escape(s.title)}</h2><p>{escape(s.content)}</p></section>" for s in result.sections
            )
        if output_format == OutputFormat.PLAIN:
            return "\n\n".join(f"{s.title}\n{s.content}" for s in result.sections)
        return json.dumps(
            {
                "type": result.content_type,
                "sections": [s.to_dict() for s in result.sections],
                "metadata": result.metadata,
            },
            indent=2,
        )

    @staticmethod
    def _documentation(request: OperationsRequest) -> OperationsResult:
        """Generate documentation."""
        logger.info("Generating documentation: %s", _log_ref(request.content))
        sections = [
            Section(title="Overview", content=f"Documentation for: {request.content}", order=1),
            Section(title="Usage", content="Usage examples and getting started guide.", order=2),
            Section(title="API Reference", content="Detailed API documentation.", order=3),
            Section(title="Configuration", content="Configuration options and defaults.", order=4),
        ]
        if request.thinking_mode in (ThinkingMode.HIGH, ThinkingMode.XHIGH):
            sections.extend((
                Section(title="Troubleshooting", content="Common issues and solutions.", order=5),
                Section(title="Migration Guide", content="Steps for upgrading.", order=6),
            ))

        return OperationsResult(
            success=True,
            content="\n\n".join(f"## {s.title}\n\n{s.content}" for s in sections),
            content_type="documentation",
            output_format=request.output_format,
            sections=sections,
            metadata={"word_count": sum(len(s.content.split()) for s in sections)},
        )

    @staticmethod
    def _creative_writing(request: OperationsRequest) -> OperationsResult:
        """Generate creative writing content."""
        logger.info("Creative writing: %s", _log_ref(request.content))
        return OperationsResult(
            success=True,
            content=f"Creative content for: {request.content}",
            content_type="creative_writing",
            output_format=request.output_format,
            sections=[Section(title="Draft", content=request.content, order=1)],
            metadata={"word_count": len(request.content.split())},
        )

    @staticmethod
    def _cost_analysis(request: OperationsRequest) -> OperationsResult:
        """Perform cost analysis."""
        logger.info("Cost analysis: %s", _log_ref(request.content))
        sections = [
            Section(title="Current Cost", content="Analysis of current resource costs.", order=1),
            Section(title="Projected Cost", content="Projected costs based on growth trends.", order=2),
            Section(title="Savings Opportunities", content="Identified cost reduction opportunities.", order=3),
            Section(title="Recommendations", content="Prioritized cost optimization actions.", order=4),
        ]
        return OperationsResult(
            success=True,
            content="\n\n".join(f"## {s.title}\n\n{s.content}" for s in sections),
            content_type="cost_analysis",
            output_format=request.output_format,
            sections=sections,
            metadata={"estimated_cost": 0.0},
        )

    @staticmethod
    def _experiment(request: OperationsRequest) -> OperationsResult:
        """Design and run experiments."""
        logger.info("Experiment: %s", _log_ref(request.content))
        sections = [
            Section(title="Hypothesis", content=f"Hypothesis for: {request.content}", order=1),
            Section(title="Methodology", content="Experimental methodology and controls.", order=2),
            Section(title="Success Criteria", content="Measurable success criteria.", order=3),
            Section(title="Rollback Plan", content="Steps to revert if experiment fails.", order=4),
        ]
        return OperationsResult(
            success=True,
            content="\n\n".join(f"## {s.title}\n\n{s.content}" for s in sections),
            content_type="experiment",
            output_format=request.output_format,
            sections=sections,
            metadata={"experiment_results": {"status": "designed", "phase": "pre-execution"}},
        )

    @staticmethod
    def _error_recovery(request: OperationsRequest) -> OperationsResult:
        """Create error recovery plan."""
        logger.info("Error recovery: %s", _log_ref(request.content))
        sections = [
            Section(title="Root Cause Analysis", content=f"Analysis of: {request.content}", order=1),
            Section(title="Fix Steps", content="Step-by-step recovery procedure.", order=2),
            Section(title="Verification", content="How to verify the fix is complete.", order=3),
            Section(title="Prevention", content="Measures to prevent recurrence.", order=4),
        ]
        return OperationsResult(
            success=True,
            content="\n\n".join(f"## {s.title}\n\n{s.content}" for s in sections),
            content_type="error_recovery",
            output_format=request.output_format,
            sections=sections,
            warnings=["Review fix steps before applying to production"],
        )

    @staticmethod
    def _synthesis(request: OperationsRequest) -> OperationsResult:
        """Synthesize information from multiple sources."""
        logger.info("Synthesis: %s", _log_ref(request.content))
        sections = [
            Section(title="Summary", content=f"Synthesis of: {request.content}", order=1),
            Section(title="Key Insights", content="Primary insights from source material.", order=2),
            Section(title="Conflicts", content="Any conflicting information across sources.", order=3),
            Section(title="Conclusion", content="Consolidated conclusion.", order=4),
        ]
        return OperationsResult(
            success=True,
            content="\n\n".join(f"## {s.title}\n\n{s.content}" for s in sections),
            content_type="synthesis",
            output_format=request.output_format,
            sections=sections,
            metadata={"source_count": 0},
        )

    @staticmethod
    def _image_generation(request: OperationsRequest) -> OperationsResult:
        """Handle image generation by calling the local diffusion engine.

        Args:
            request: The operations request with prompt in content field.

        Returns:
            OperationsResult with success=True and image path, or success=False
            with an error description if the engine is unavailable or fails.
        """
        logger.info("Image generation: %s", _log_ref(request.content))
        from vetinari.image.diffusion_engine import DiffusionEngine

        engine = DiffusionEngine()
        if not engine.is_available():
            return OperationsResult(
                success=False,
                content="Image generation unavailable: diffusers/torch/Pillow not installed",
                content_type="image_generation",
                output_format=request.output_format,
                metadata={"status": "engine_unavailable"},
            )
        if not engine.has_models():
            return OperationsResult(
                success=False,
                content="Image generation unavailable: no local models found",
                content_type="image_generation",
                output_format=request.output_format,
                metadata={"status": "no_models"},
            )
        result = engine.generate(prompt=request.content)
        if result.get("success"):
            return OperationsResult(
                success=True,
                content=result.get("path", ""),
                content_type="image_generation",
                output_format=request.output_format,
                metadata={
                    "status": "generated",
                    "path": result.get("path", ""),
                    "filename": result.get("filename", ""),
                },
            )
        return OperationsResult(
            success=False,
            content=result.get("error", "Generation failed"),
            content_type="image_generation",
            output_format=request.output_format,
            metadata={"status": "generation_failed", "error": result.get("error", "")},
        )

    @staticmethod
    def _improvement(request: OperationsRequest) -> OperationsResult:
        """Identify improvement opportunities."""
        logger.info("Improvement analysis: %s", _log_ref(request.content))
        sections = [
            Section(title="Current State", content=f"Analysis of: {request.content}", order=1),
            Section(title="Tech Debt", content="Identified technical debt items.", order=2),
            Section(title="Quick Wins", content="Low-effort, high-impact improvements.", order=3),
            Section(title="Strategic Improvements", content="Long-term improvement roadmap.", order=4),
        ]
        return OperationsResult(
            success=True,
            content="\n\n".join(f"## {s.title}\n\n{s.content}" for s in sections),
            content_type="improvement",
            output_format=request.output_format,
            sections=sections,
        )
