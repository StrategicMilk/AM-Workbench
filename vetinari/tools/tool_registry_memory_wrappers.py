"""Memory, model, and planning wrappers for tool registry integration."""

from __future__ import annotations

import logging

from vetinari.execution_context import ToolPermission
from vetinari.security.fail_closed import assert_closed_schema, sanitize_untrusted_text
from vetinari.tool_interface import (
    Tool,
    ToolCategory,
    ToolMetadata,
    ToolParameter,
    ToolResult,
)
from vetinari.types import ExecutionMode

logger = logging.getLogger(__name__)


def _redact_text(text: str) -> str:
    return f"<redacted:{len(text)} chars>"


def _safe_optional_text(value: object, *, max_length: int, default: str = "") -> str:
    text = str(value if value is not None else default).strip()
    return sanitize_untrusted_text(text, max_length=max_length) if text else default


def _safe_tags(value: object) -> list[str]:
    if value in (None, ""):
        return []
    if not isinstance(value, list):
        raise ValueError("memory tags must be a list")
    return [sanitize_untrusted_text(str(item), max_length=80) for item in value]


def _permission(name: str, fallback: str) -> object:
    return getattr(ToolPermission, name, fallback)


class MemoryRecallToolWrapper(Tool):
    """Wrapper for memory recall."""

    def __init__(self):
        metadata = ToolMetadata(
            name="recall_memory",
            description="Recall information from memory",
            category=ToolCategory.SEARCH_ANALYSIS,
            version="1.0.0",
            parameters=[
                ToolParameter(
                    name="query",
                    type=str,
                    description="Query to search memory",
                    required=False,
                    default="",
                ),
                ToolParameter(
                    name="memory_type",
                    type=str,
                    description="Type of memory to search",
                    required=False,
                    default=None,
                ),
                ToolParameter(
                    name="limit",
                    type=int,
                    description="Maximum results",
                    required=False,
                    default=5,
                ),
            ],
            required_permissions=[_permission("MEMORY_READ", "memory_read")],
            allowed_modes=[ExecutionMode.EXECUTION, ExecutionMode.PLANNING],
            tags=["memory", "recall", "context"],
        )
        super().__init__(metadata)

        self._memory = None

    def _get_memory(self):
        if self._memory is None:
            from vetinari.memory import get_unified_memory_store

            self._memory = get_unified_memory_store()
        return self._memory

    def execute(self, **kwargs) -> ToolResult:
        """Execute.

        Returns:
            The ToolResult result.

        Raises:
            ValueError: If constraints are present but not a mapping.
        """
        assert_closed_schema(kwargs, allowed_keys={"query", "memory_type", "limit"})
        query = _safe_optional_text(kwargs.get("query", ""), max_length=2_000)
        memory_type = _safe_optional_text(kwargs.get("memory_type"), max_length=80, default="")
        limit = kwargs.get("limit", 5)

        try:
            memory = self._get_memory()
            search_kwargs: dict = {"query": query, "limit": limit}
            if memory_type:
                search_kwargs["entry_type"] = memory_type
            results = memory.search(**search_kwargs)

            return ToolResult(
                success=True,
                output={
                    "entries": [e.to_dict() for e in results],
                    "count": len(results),
                },
            )
        except Exception:
            logger.exception("Memory operation failed for recall query %s", _redact_text(str(query)))
            return ToolResult(
                success=False,
                output=None,
                error="Memory operation failed",
            )


class MemoryRememberToolWrapper(Tool):
    """Wrapper for storing in memory."""

    def __init__(self):
        metadata = ToolMetadata(
            name="remember",
            description="Store information in memory for future recall",
            category=ToolCategory.SEARCH_ANALYSIS,
            version="1.0.0",
            parameters=[
                ToolParameter(
                    name="content",
                    type=str,
                    description="Content to remember",
                    required=True,
                ),
                ToolParameter(
                    name="memory_type",
                    type=str,
                    description="Type of memory",
                    required=False,
                    default="context",
                ),
                ToolParameter(
                    name="tags",
                    type=list[str],
                    description="Tags for the memory",
                    required=False,
                    default=None,
                ),
            ],
            required_permissions=[ToolPermission.MEMORY_WRITE],
            allowed_modes=[ExecutionMode.EXECUTION],
            tags=["memory", "remember", "store"],
        )
        super().__init__(metadata)

        self._memory = None

    def _get_memory(self):
        if self._memory is None:
            from vetinari.memory import get_unified_memory_store

            self._memory = get_unified_memory_store()
        return self._memory

    def execute(self, **kwargs) -> ToolResult:
        """Execute.

        Returns:
            The ToolResult result.

        Raises:
            ValueError: If constraints are present but not a mapping.
        """
        assert_closed_schema(kwargs, allowed_keys={"content", "memory_type", "tags"}, required_keys={"content"})
        content = sanitize_untrusted_text(kwargs.get("content", ""), max_length=20_000)
        memory_type = _safe_optional_text(kwargs.get("memory_type", "context"), max_length=80, default="context")
        tags = _safe_tags(kwargs.get("tags", []))

        try:
            memory = self._get_memory()

            from vetinari.memory import MemoryEntry
            from vetinari.types import MemoryType

            mem_type = MemoryType(memory_type)

            entry = MemoryEntry(
                content=content,
                entry_type=mem_type,
                metadata={"tags": tags} if tags else None,
            )
            entry_id = memory.remember(entry)

            return ToolResult(
                success=True,
                output={"entry_id": entry_id},
            )
        except Exception:
            logger.exception("Memory operation failed while storing content")
            return ToolResult(
                success=False,
                output=None,
                error="Memory operation failed",
            )


class ModelSelectToolWrapper(Tool):
    """Wrapper for dynamic model selection."""

    def __init__(self):
        metadata = ToolMetadata(
            name="select_model",
            description="Select the best model for a task based on capabilities",
            category=ToolCategory.MODEL_INFERENCE,
            version="1.0.0",
            parameters=[
                ToolParameter(
                    name="task_type",
                    type=str,
                    description="Type of task (coding, analysis, reasoning, etc.)",
                    required=True,
                ),
                ToolParameter(
                    name="task_description",
                    type=str,
                    description="Description of the task",
                    required=False,
                    default="",
                ),
            ],
            required_permissions=[ToolPermission.MODEL_INFERENCE],
            allowed_modes=[ExecutionMode.EXECUTION, ExecutionMode.PLANNING],
            tags=["model", "selection", "routing"],
        )
        super().__init__(metadata)

        self._router = None

    def _get_router(self):
        if self._router is None:
            from vetinari.models.dynamic_model_router import get_model_router

            self._router = get_model_router()
        return self._router

    def execute(self, **kwargs) -> ToolResult:
        """Execute the planning tool with closed-schema keyword arguments.

        Args:
            **kwargs: Must contain ``goal`` and may contain ``constraints``.

        Returns:
            The planning tool result.

        Raises:
            ValueError: If constraints are present but not a mapping.
        """
        assert_closed_schema(kwargs, allowed_keys={"task_type", "task_description"}, required_keys={"task_type"})
        task_type = sanitize_untrusted_text(kwargs.get("task_type", "general"), max_length=120)
        task_description = _safe_optional_text(kwargs.get("task_description", ""), max_length=4_000)

        try:
            router = self._get_router()

            from vetinari.models.dynamic_model_router import parse_task_type

            task_enum = parse_task_type(task_type)

            selection = router.select_model(task_enum, task_description)

            if selection:
                return ToolResult(
                    success=True,
                    output={
                        "model_id": selection.model.id,
                        "model_name": selection.model.name,
                        "reasoning": selection.reasoning,
                        "confidence": selection.confidence,
                        "alternatives": [a.id for a in selection.alternatives],
                    },
                )
            return ToolResult(
                success=False,
                output=None,
                error="No suitable model found",
            )
        except Exception:
            logger.exception("Model selection failed for task type %r", task_type)
            return ToolResult(
                success=False,
                output=None,
                error="Model selection failed",
            )


class GeneratePlanToolWrapper(Tool):
    """Wrapper for plan generation."""

    def __init__(self):
        metadata = ToolMetadata(
            name="generate_plan",
            description="Generate an execution plan from a goal",
            category=ToolCategory.SYSTEM_OPERATIONS,
            version="1.0.0",
            parameters=[
                ToolParameter(
                    name="goal",
                    type=str,
                    description="Goal to achieve",
                    required=True,
                ),
                ToolParameter(
                    name="constraints",
                    type=dict,
                    description="Constraints for the plan",
                    required=False,
                    default=None,
                ),
            ],
            required_permissions=[ToolPermission.PLANNING],
            allowed_modes=[ExecutionMode.EXECUTION, ExecutionMode.PLANNING],
            tags=["planning", "plan", "decomposition"],
        )
        super().__init__(metadata)

        self._orchestrator = None

    def _get_orchestrator(self):
        if self._orchestrator is None:
            from vetinari.orchestration.two_layer import get_two_layer_orchestrator

            self._orchestrator = get_two_layer_orchestrator()
        return self._orchestrator

    def execute(self, **kwargs) -> ToolResult:
        """Execute the plan generation tool.

        Returns:
            The ToolResult result.

        Raises:
            ValueError: If constraints are present but not a mapping.
        """
        assert_closed_schema(kwargs, allowed_keys={"goal", "constraints"}, required_keys={"goal"})
        goal = sanitize_untrusted_text(kwargs.get("goal", ""), max_length=20_000)
        constraints = kwargs.get("constraints", {})
        if constraints is not None and not isinstance(constraints, dict):
            raise ValueError("plan constraints must be a mapping")

        try:
            orchestrator = self._get_orchestrator()
            graph = orchestrator.generate_plan_only(goal, constraints)

            return ToolResult(
                success=True,
                output=graph.to_dict(),
            )
        except Exception:
            logger.exception("Plan generation failed for goal %r", goal)
            return ToolResult(
                success=False,
                output=None,
                error="Plan generation failed",
            )
