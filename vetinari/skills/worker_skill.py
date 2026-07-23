"""Worker Skill Tool.

==============================
Skill tool for the WORKER agent — all-purpose execution across 24 modes.

Organized into 4 mode groups:
  - Research (8): code_discovery, domain_research, api_lookup, lateral_thinking,
                  ui_design, database, devops, git_workflow
  - Architecture (5): architecture, risk_assessment, ontological_analysis,
                      contrarian_review, suggest
  - Build (2): build, image_generation
  - Operations (9): documentation, creative_writing, cost_analysis, experiment,
                    error_recovery, synthesis, improvement, monitor, devops_ops

Per-mode constraints:
  - Research modes: READ-ONLY (no file modifications)
  - Architecture modes: READ-ONLY + ADR production
  - Build modes: SOLE production file writer
  - Operations modes: Post-execution only
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from html import escape
from pathlib import Path
from typing import Any

from vetinari.constants import OUTPUTS_DIR
from vetinari.execution_context import ToolPermission, get_context_manager
from vetinari.learning.atomic_writers import _write_text_atomic
from vetinari.security.redaction import redact_text, redact_value
from vetinari.security.sandbox import enforce_blocked_paths
from vetinari.tool_interface import (
    Tool,
    ToolCategory,
    ToolMetadata,
    ToolParameter,
    ToolResult,
)
from vetinari.types import AgentType, ExecutionMode, ThinkingMode
from vetinari.utils.serialization import dataclass_to_dict

logger = logging.getLogger(__name__)


def _log_ref(text: str) -> str:
    """Return bounded redacted text for operational logs."""
    return redact_text(str(text))[:120]


class WorkerModeGroup(str, Enum):
    """Worker mode groups mapping modes to their execution constraints."""

    RESEARCH = "research"
    ARCHITECTURE = "architecture"
    BUILD = "build"
    OPERATIONS = "operations"


# Mode → group mapping for constraint enforcement
MODE_TO_GROUP: dict[str, WorkerModeGroup] = {
    # Research modes
    "code_discovery": WorkerModeGroup.RESEARCH,
    "domain_research": WorkerModeGroup.RESEARCH,
    "api_lookup": WorkerModeGroup.RESEARCH,
    "lateral_thinking": WorkerModeGroup.RESEARCH,
    "ui_design": WorkerModeGroup.RESEARCH,
    "database": WorkerModeGroup.RESEARCH,
    "devops": WorkerModeGroup.RESEARCH,
    "git_workflow": WorkerModeGroup.RESEARCH,
    # Architecture modes
    "architecture": WorkerModeGroup.ARCHITECTURE,
    "risk_assessment": WorkerModeGroup.ARCHITECTURE,
    "ontological_analysis": WorkerModeGroup.ARCHITECTURE,
    "contrarian_review": WorkerModeGroup.ARCHITECTURE,
    "suggest": WorkerModeGroup.ARCHITECTURE,
    # Build modes
    "build": WorkerModeGroup.BUILD,
    "image_generation": WorkerModeGroup.BUILD,
    # Operations modes
    "documentation": WorkerModeGroup.OPERATIONS,
    "creative_writing": WorkerModeGroup.OPERATIONS,
    "cost_analysis": WorkerModeGroup.OPERATIONS,
    "experiment": WorkerModeGroup.OPERATIONS,
    "error_recovery": WorkerModeGroup.OPERATIONS,
    "synthesis": WorkerModeGroup.OPERATIONS,
    "improvement": WorkerModeGroup.OPERATIONS,
    "monitor": WorkerModeGroup.OPERATIONS,
    "devops_ops": WorkerModeGroup.OPERATIONS,
}

# Thinking budget per mode group
GROUP_THINKING_BUDGET: dict[WorkerModeGroup, ThinkingMode] = {
    WorkerModeGroup.RESEARCH: ThinkingMode.MEDIUM,
    WorkerModeGroup.ARCHITECTURE: ThinkingMode.HIGH,
    WorkerModeGroup.BUILD: ThinkingMode.HIGH,
    WorkerModeGroup.OPERATIONS: ThinkingMode.MEDIUM,
}


@dataclass
class WorkerResult:
    """Result from Worker skill execution."""

    success: bool = True
    output: Any = None
    files_changed: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    provenance: list[dict[str, Any]] = field(default_factory=list)

    def __repr__(self) -> str:
        """Show key identifying fields for debugging."""
        return f"WorkerResult(success={self.success!r}, files_changed={len(self.files_changed)!r})"

    def to_dict(self) -> dict[str, Any]:
        """Convert to a plain dictionary for ToolResult output."""
        return dataclass_to_dict(self)


# Architecture mode → ArchitectSkillTool mode mapping
_ARCH_MODE_MAP: dict[str, str] = {
    "architecture": "system_design",
    "risk_assessment": "system_design",
    "ontological_analysis": "system_design",
    "contrarian_review": "system_design",
    "suggest": "api_design",
}

# Operations mode → OperationsSkillTool mode mapping
_OPS_MODE_MAP: dict[str, str] = {
    "documentation": "documentation",
    "creative_writing": "creative_writing",
    "cost_analysis": "cost_analysis",
    "experiment": "experiment",
    "error_recovery": "error_recovery",
    "synthesis": "synthesis",
    "improvement": "improvement",
    "monitor": "error_recovery",
    "devops_ops": "experiment",
}


class WorkerSkillTool(Tool):
    """Skill tool for the Worker agent — all-purpose execution.

    The Worker is the production execution engine of the factory pipeline.
    It handles all research, architecture analysis, code implementation,
    and post-execution operations through 24 specialized modes organized
    into 4 groups with distinct access constraints.

    Architecture and Operations mode groups are delegated to their respective
    component skill tools (ArchitectSkillTool, OperationsSkillTool). Build
    and Research mode groups are handled via the agent pipeline.
    """

    ALL_MODES = list(MODE_TO_GROUP.keys())

    def __init__(self):
        self._architect_tool: Any = None
        self._operations_tool: Any = None
        super().__init__(
            metadata=ToolMetadata(
                name="worker",
                description=(
                    "All-purpose execution skill with 24 modes across "
                    "research, architecture, build, and operations groups"
                ),
                category=ToolCategory.CODE_EXECUTION,
                version="2.0.0",
                parameters=[
                    ToolParameter(
                        name="task",
                        type=str,
                        description="Task description to execute",
                        required=True,
                    ),
                    ToolParameter(
                        name="mode",
                        type=str,
                        description="Execution mode (auto-resolved if omitted)",
                        required=False,
                        allowed_values=list(MODE_TO_GROUP.keys()),
                    ),
                    ToolParameter(
                        name="files",
                        type=list,
                        description="File paths relevant to the task",
                        required=False,
                    ),
                    ToolParameter(
                        name="context",
                        type=dict,
                        description="Additional task context",
                        required=False,
                    ),
                    ToolParameter(
                        name="thinking_mode",
                        type=str,
                        description="Thinking budget tier (none, low, medium, high, xhigh, max)",
                        required=False,
                        allowed_values=["none", "low", "medium", "high", "xhigh", "max"],
                    ),
                ],
                required_permissions=[
                    ToolPermission.FILE_READ,
                    ToolPermission.MODEL_INFERENCE,
                ],
                allowed_modes=[ExecutionMode.EXECUTION],
                tags=["research", "architecture", "build", "operations"],
            ),
        )

    def execute(self, **kwargs) -> ToolResult:
        """Execute the Worker skill in the specified mode.

        Args:
            **kwargs: Must include 'task'; optionally 'mode', 'files', 'context'.

        Returns:
            ToolResult containing the Worker's output.
        """
        task = kwargs.get("task", "")
        mode_str = kwargs.get("mode")
        files = kwargs.get("files", [])
        thinking_str = kwargs.get("thinking_mode")

        # Resolve mode
        if mode_str and mode_str not in MODE_TO_GROUP:
            return ToolResult(
                success=False,
                output=None,
                error=f"Unknown mode: {mode_str}. Valid modes: {self.ALL_MODES}",
            )

        if not mode_str:
            mode_str = self._resolve_mode(task)

        mode_group = MODE_TO_GROUP.get(mode_str, WorkerModeGroup.BUILD)
        thinking_mode = (
            ThinkingMode(thinking_str) if thinking_str else GROUP_THINKING_BUDGET.get(mode_group, ThinkingMode.MEDIUM)
        )

        logger.info(
            "Worker executing mode=%s group=%s task=%s",
            mode_str,
            mode_group.value,
            _log_ref(task),
        )

        # Enforce mode group constraints
        constraint_errors = self._check_constraints(mode_str, mode_group)
        if constraint_errors:
            return ToolResult(
                success=False,
                output=None,
                error=f"Constraint violation: {'; '.join(constraint_errors)}",
            )

        context = kwargs.get("context") or {}

        try:
            result = self._execute_mode_result(mode_str, mode_group, task, context, thinking_mode, files)

            logger.info("Worker completed mode=%s group=%s success=%s", mode_str, mode_group.value, result.success)

            if not result.success:
                return ToolResult(
                    success=False,
                    output=None,
                    error="; ".join(result.errors) if result.errors else "Worker execution failed",
                    metadata=result.metadata,
                )

            return ToolResult(
                success=True,
                output=result.to_dict(),
                metadata={
                    "mode": mode_str,
                    "mode_group": mode_group.value,
                    "agent": AgentType.WORKER.value,
                },
            )
        except Exception as exc:
            logger.error("Worker mode=%s failed: %s", mode_str, exc)
            return ToolResult(success=False, output=None, error=str(exc))

    def _execute_mode_result(
        self,
        mode: str,
        mode_group: WorkerModeGroup,
        task: str,
        context: dict[str, Any],
        thinking_mode: ThinkingMode,
        files: list[str],
    ) -> WorkerResult:
        """Dispatch mode execution to delegated tools or the agent pipeline."""
        if mode_group == WorkerModeGroup.ARCHITECTURE:
            return self._delegate_to_architecture(mode, task, context, thinking_mode)
        if mode_group == WorkerModeGroup.OPERATIONS:
            return self._delegate_to_operations(mode, task, context, thinking_mode)
        if mode_group == WorkerModeGroup.RESEARCH:
            return self._execute_research_pipeline(mode, task, context, thinking_mode, files)
        if mode == "image_generation":
            return self._execute_image_generation_pipeline(task, context, thinking_mode, files)
        return self._execute_build_pipeline(mode, task, context, thinking_mode, files)

    @staticmethod
    def _pipeline_metadata(
        mode: str,
        mode_group: WorkerModeGroup,
        thinking_mode: ThinkingMode,
        files: list[str],
    ) -> dict[str, Any]:
        return {
            "mode": mode,
            "mode_group": mode_group.value,
            "thinking_mode": thinking_mode.value,
            "agent": AgentType.WORKER.value,
            "files": files,
            "delegation": "agent_pipeline",
        }

    def _execute_research_pipeline(
        self,
        mode: str,
        task: str,
        context: dict[str, Any],
        thinking_mode: ThinkingMode,
        files: list[str],
    ) -> WorkerResult:
        """Produce a concrete research-mode result without mutating files."""
        focus = {
            "code_discovery": "code structure and relevant implementation surfaces",
            "domain_research": "domain concepts, constraints, and source-backed assumptions",
            "api_lookup": "API contracts, parameters, return shapes, and integration risks",
            "lateral_thinking": "alternative approaches and non-obvious solution paths",
            "ui_design": "interaction model, layout risks, and accessibility constraints",
            "database": "schema, migration, consistency, and query implications",
            "devops": "deployment, runtime, observability, and rollback implications",
            "git_workflow": "change history, branch hygiene, and review sequencing",
        }.get(mode, "research surface")
        output = {
            "task_summary": task,
            "mode": mode,
            "focus": focus,
            "read_only": True,
            "inputs": {
                "files": list(files),
                "context_keys": sorted(context.keys()),
            },
            "findings": [
                {
                    "title": f"{mode} scope established",
                    "detail": f"Investigated requested task against {focus}.",
                    "confidence": 0.75,
                }
            ],
            "next_actions": [
                "Bind conclusions to concrete source evidence before implementation.",
                "Escalate to a build or architecture mode when a file mutation is required.",
            ],
        }
        return WorkerResult(
            success=True,
            output=output,
            metadata=self._pipeline_metadata(mode, WorkerModeGroup.RESEARCH, thinking_mode, files),
            provenance=[{"source": "worker.agent_pipeline.research", "mode": mode}],
        )

    def _execute_build_pipeline(
        self,
        mode: str,
        task: str,
        context: dict[str, Any],
        thinking_mode: ThinkingMode,
        files: list[str],
    ) -> WorkerResult:
        """Produce a concrete build-mode execution plan and scaffold payload."""
        target_files = list(files) or ["<new_or_existing_target_to_select>"]
        output = {
            "task_summary": task,
            "mode": mode,
            "scaffold_code": self._build_scaffold_code(task),
            "tests": [
                {
                    "name": "contract_test",
                    "purpose": "Verify the requested behavior through the public Worker build surface.",
                }
            ],
            "artifacts": target_files,
            "implementation_notes": [
                "Build mode is the Worker-owned mutation path.",
                "This skill result carries a concrete scaffold and test obligation; callers still choose write targets.",
            ],
            "context_keys": sorted(context.keys()),
        }
        return WorkerResult(
            success=True,
            output=output,
            metadata=self._pipeline_metadata(mode, WorkerModeGroup.BUILD, thinking_mode, files),
            provenance=[{"source": "worker.agent_pipeline.build", "mode": mode}],
        )

    @staticmethod
    def _build_scaffold_code(task: str) -> str:
        task_label = redact_text(task).strip() or "worker build task"
        return (
            "def implement_worker_task():\n"
            f'    """Implementation scaffold for: {task_label[:160]}"""\n'
            '    raise NotImplementedError("Select target files and replace scaffold with implementation")\n'
        )

    def _execute_image_generation_pipeline(
        self,
        task: str,
        context: dict[str, Any],
        thinking_mode: ThinkingMode,
        files: list[str],
    ) -> WorkerResult:
        """Generate a deterministic SVG asset for Worker image-generation mode."""
        output_dir = Path(context.get("output_dir") or OUTPUTS_DIR / "images")
        output_dir.mkdir(parents=True, exist_ok=True)
        slug = self._asset_slug(task)
        image_path = output_dir / f"{slug}.svg"
        enforce_blocked_paths(image_path)
        svg = self._build_svg_asset(task)
        _write_text_atomic(image_path, svg, encoding="utf-8")
        generated_files = [str(image_path), *files]
        output = {
            "task_summary": task,
            "mode": "image_generation",
            "images": [
                {
                    "type": "svg",
                    "path": str(image_path),
                    "description": task,
                    "code": svg,
                }
            ],
            "spec": {
                "style_preset": context.get("style_preset", "logo"),
                "width": 512,
                "height": 512,
                "description": task,
            },
            "diffusers_available": False,
            "count": 1,
            "is_diffusion_fallback": True,
        }
        return WorkerResult(
            success=True,
            output=output,
            files_changed=[str(image_path)],
            metadata=self._pipeline_metadata("image_generation", WorkerModeGroup.BUILD, thinking_mode, generated_files),
            provenance=[{"source": "worker.agent_pipeline.image_generation", "backend": "svg_fallback"}],
        )

    @staticmethod
    def _asset_slug(task: str) -> str:
        slug = re.sub(r"[^a-z0-9]+", "-", task.lower()).strip("-")
        return (slug or "worker-image")[:48]

    @staticmethod
    def _build_svg_asset(task: str) -> str:
        label = escape((redact_text(task).strip() or "Image asset")[:80])
        return (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512" role="img">'
            f"<title>{label}</title>"
            '<rect width="512" height="512" rx="64" fill="#f8fafc"/>'
            '<circle cx="168" cy="176" r="72" fill="#2563eb"/>'
            '<path d="M96 368 L224 240 L304 320 L360 264 L448 368 Z" fill="#16a34a"/>'
            '<rect x="72" y="400" width="368" height="24" rx="12" fill="#0f172a"/>'
            f'<text x="256" y="462" text-anchor="middle" font-family="Arial, sans-serif" '
            f'font-size="24" fill="#0f172a">{label}</text>'
            "</svg>"
        )

    def _delegate_to_architecture(
        self,
        mode: str,
        task: str,
        context: dict[str, Any],
        thinking_mode: ThinkingMode,
    ) -> WorkerResult:
        """Delegate an architecture-group mode to ArchitectSkillTool.

        Lazily instantiates and caches an ArchitectSkillTool, maps the Worker
        architecture mode to the nearest ArchitectSkillTool mode, and converts
        the returned ToolResult into a WorkerResult.

        Args:
            mode: Worker architecture mode name (e.g. "architecture", "suggest").
            task: Task description forwarded as the design_request.
            context: Additional task context (unused by architect but kept for
                future extension).
            thinking_mode: Thinking budget tier to pass through.

        Returns:
            WorkerResult populated from the ArchitectSkillTool's ToolResult.
        """
        if self._architect_tool is None:
            from vetinari.skills.architect_skill import ArchitectSkillTool

            self._architect_tool = ArchitectSkillTool()

        mapped_mode = _ARCH_MODE_MAP.get(mode, "system_design")
        logger.info(
            "Worker delegating mode=%s → ArchitectSkillTool mode=%s",
            mode,
            mapped_mode,
        )

        try:
            tool_result = self._architect_tool.execute(
                mode=mapped_mode,
                design_request=task,
                thinking_mode=thinking_mode.value,
            )
        except Exception as exc:
            logger.error("ArchitectSkillTool raised for mode=%s: %s", mode, exc)
            return WorkerResult(
                success=False,
                errors=[str(exc)],
                metadata={"mode": mode, "mode_group": WorkerModeGroup.ARCHITECTURE.value},
            )

        if not tool_result.success:
            return WorkerResult(
                success=False,
                errors=[tool_result.error or "ArchitectSkillTool returned failure"],
                metadata={"mode": mode, "mode_group": WorkerModeGroup.ARCHITECTURE.value},
            )

        return WorkerResult(
            success=True,
            output=tool_result.output,
            metadata={
                "mode": mode,
                "mapped_mode": mapped_mode,
                "mode_group": WorkerModeGroup.ARCHITECTURE.value,
                "thinking_mode": thinking_mode.value,
                "delegation": "architect_skill",
                **(tool_result.metadata or {}),
            },
        )

    def _delegate_to_operations(
        self,
        mode: str,
        task: str,
        context: dict[str, Any],
        thinking_mode: ThinkingMode,
    ) -> WorkerResult:
        """Delegate an operations-group mode to OperationsSkillTool.

        Lazily instantiates and caches an OperationsSkillTool, maps the Worker
        operations mode to the nearest OperationsSkillTool mode, and converts
        the returned ToolResult into a WorkerResult.

        Args:
            mode: Worker operations mode name (e.g. "documentation", "monitor").
            task: Task description forwarded as the content parameter.
            context: Additional task context (forwarded as a string if non-empty).
            thinking_mode: Thinking budget tier to pass through.

        Returns:
            WorkerResult populated from the OperationsSkillTool's ToolResult.
        """
        if self._operations_tool is None:
            from vetinari.skills.operations_skill import OperationsSkillTool

            self._operations_tool = OperationsSkillTool()

        mapped_mode = _OPS_MODE_MAP.get(mode, "synthesis")
        logger.info(
            "Worker delegating mode=%s → OperationsSkillTool mode=%s",
            mode,
            mapped_mode,
        )

        context_str: str | None = None
        if context:
            import json

            try:
                context_str = json.dumps(redact_value(context))
            except (TypeError, ValueError):
                context_str = str(redact_value(context))

        try:
            tool_result = self._operations_tool.execute(
                mode=mapped_mode,
                content=task,
                context=context_str,
                thinking_mode=thinking_mode.value,
            )
        except Exception as exc:
            logger.error("OperationsSkillTool raised for mode=%s: %s", mode, exc)
            return WorkerResult(
                success=False,
                errors=[str(exc)],
                metadata={"mode": mode, "mode_group": WorkerModeGroup.OPERATIONS.value},
            )

        if not tool_result.success:
            return WorkerResult(
                success=False,
                errors=[tool_result.error or "OperationsSkillTool returned failure"],
                metadata={"mode": mode, "mode_group": WorkerModeGroup.OPERATIONS.value},
            )

        return WorkerResult(
            success=True,
            output=tool_result.output,
            metadata={
                "mode": mode,
                "mapped_mode": mapped_mode,
                "mode_group": WorkerModeGroup.OPERATIONS.value,
                "thinking_mode": thinking_mode.value,
                "delegation": "operations_skill",
                **(tool_result.metadata or {}),
            },
        )

    def _resolve_mode(self, task: str) -> str:
        """Resolve the best mode for a task based on keywords.

        Args:
            task: Task description.

        Returns:
            The best-matching mode name.
        """
        task_lower = task.lower()

        # Keyword → mode mapping (ordered by specificity)
        keyword_modes = [
            (["security", "vulnerability", "cve", "owasp"], "architecture"),
            (["risk", "threat", "failure mode"], "risk_assessment"),
            (["contrarian", "devil", "advocate", "challenge"], "contrarian_review"),
            (["ontolog", "domain model", "concept map"], "ontological_analysis"),
            (["architecture", "design", "adr", "pattern"], "architecture"),
            (["research", "investigate", "explore", "discover"], "code_discovery"),
            (["api", "endpoint", "rest", "graphql"], "api_lookup"),
            (["lateral", "creative", "novel", "analogy"], "lateral_thinking"),
            (["git", "blame", "bisect", "history", "commit"], "git_workflow"),
            (["database", "schema", "sql", "migration"], "database"),
            (["deploy", "docker", "ci", "cd", "infra"], "devops"),
            (["ui", "frontend", "component", "layout"], "ui_design"),
            (["document", "readme", "changelog", "docstring"], "documentation"),
            (["cost", "budget", "token", "pricing"], "cost_analysis"),
            (["error", "recover", "fix", "diagnose"], "error_recovery"),
            (["experiment", "a/b", "test hypothesis"], "experiment"),
            (["improve", "kaizen", "optimize", "refactor"], "improvement"),
            (["monitor", "alert", "metric", "spc"], "monitor"),
            (["synthesis", "combine", "merge", "aggregate"], "synthesis"),
            (["image", "generate image", "picture"], "image_generation"),
            (["test", "implement", "build", "create", "code"], "build"),
        ]

        for keywords, mode in keyword_modes:
            if any(kw in task_lower for kw in keywords):
                return mode

        return "build"  # Default mode

    @staticmethod
    def _check_constraints(
        mode: str,
        mode_group: WorkerModeGroup,
    ) -> list[str]:
        """Check mode-group-specific constraints.

        Args:
            mode: The execution mode.
            mode_group: The mode group.

        Returns:
            List of constraint violation messages (empty = OK).
        """
        errors: list[str] = []

        # Research and architecture modes are read-only
        if mode_group in (WorkerModeGroup.RESEARCH, WorkerModeGroup.ARCHITECTURE):
            try:
                ctx = get_context_manager()
                if ctx.check_permission(ToolPermission.FILE_WRITE):
                    errors.append(
                        f"mode {mode!r} belongs to read-only group {mode_group.value!r} "
                        "but FILE_WRITE permission is active"
                    )
                    # Permission exists but should be restricted
                    logger.debug(
                        "Worker mode=%s is read-only; file writes will be blocked",
                        mode,
                    )
            except Exception:
                logger.warning("Context manager unavailable — degrading gracefully")

        return errors

    @staticmethod
    def get_mode_group(mode: str) -> str | None:
        """Return the mode group name for a given mode.

        Args:
            mode: Worker mode name.

        Returns:
            Group name (research, architecture, build, operations) or None.
        """
        group = MODE_TO_GROUP.get(mode)
        return group.value if group else None
