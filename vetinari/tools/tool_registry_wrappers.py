"""Tool wrapper classes used by the registry integration module."""

from __future__ import annotations

import logging

from vetinari.execution_context import ToolPermission
from vetinari.tool_interface import (
    Tool,
    ToolCategory,
    ToolMetadata,
    ToolParameter,
    ToolResult,
)
from vetinari.tools.tool_registry_memory_wrappers import (
    GeneratePlanToolWrapper as GeneratePlanToolWrapper,
)
from vetinari.tools.tool_registry_memory_wrappers import (
    MemoryRecallToolWrapper as MemoryRecallToolWrapper,
)
from vetinari.tools.tool_registry_memory_wrappers import (
    MemoryRememberToolWrapper as MemoryRememberToolWrapper,
)
from vetinari.tools.tool_registry_memory_wrappers import (
    ModelSelectToolWrapper as ModelSelectToolWrapper,
)
from vetinari.types import ExecutionMode
from vetinari.workbench.effective_config import capture_tool_use_config_snapshot

logger = logging.getLogger(__name__)


def _redact_text(text: str) -> str:
    return f"<redacted:{len(text)} chars>"


class WebSearchToolWrapper(Tool):
    """Wrapper for the web search tool."""

    def __init__(self):
        metadata = ToolMetadata(
            name="web_search",
            description="Search the web for information with citations and provenance tracking",
            category=ToolCategory.SEARCH_ANALYSIS,
            version="1.0.0",
            parameters=[
                ToolParameter(
                    name="query",
                    type=str,
                    description="Search query",
                    required=True,
                ),
                ToolParameter(
                    name="max_results",
                    type=int,
                    description="Maximum number of results",
                    required=False,
                    default=5,
                ),
                ToolParameter(
                    name="backend",
                    type=str,
                    description="Search backend to use",
                    required=False,
                    default="duckduckgo",
                ),
            ],
            required_permissions=[ToolPermission.WEB_ACCESS],
            allowed_modes=[ExecutionMode.EXECUTION],
            tags=["search", "web", "information", "research"],
        )
        super().__init__(metadata)

        # Lazy import
        self._search_tool = None

    def _get_search_tool(self):
        if self._search_tool is None:
            from vetinari.tools.web_search_tool import get_search_tool

            self._search_tool = get_search_tool()
        return self._search_tool

    def execute(self, **kwargs) -> ToolResult:
        """Execute.

        Returns:
            The ToolResult result.
        """
        query = kwargs.get("query", "")
        max_results = kwargs.get("max_results", 5)
        # backend param accepted for API compatibility; actual backend is set at startup
        backend = kwargs.get("backend", "duckduckgo")

        try:
            search_tool = self._get_search_tool()
            response = search_tool.search(query, max_results=max_results)
            snapshot = capture_tool_use_config_snapshot(
                self.metadata.name,
                {"query": query, "max_results": max_results, "backend": backend},
                success=True,
            )

            return ToolResult(
                success=True,
                output={
                    "results": [r.to_dict() for r in response.results],
                    "query": response.query,
                    "total_results": response.total_results,
                    "citations": response.get_citations(),
                },
                metadata={
                    "backend": backend,
                    "execution_time_ms": response.execution_time_ms,
                    "effective_config_snapshot_id": snapshot.snapshot_id,
                },
            )
        except Exception:
            logger.exception("Web search failed for query %s", _redact_text(str(query)))
            snapshot = capture_tool_use_config_snapshot(
                self.metadata.name,
                {"query": query, "max_results": max_results, "backend": backend},
                success=False,
            )
            return ToolResult(
                success=False,
                output=None,
                error="Web search failed",
                metadata={"effective_config_snapshot_id": snapshot.snapshot_id},
            )


class ResearchTopicToolWrapper(Tool):
    """Wrapper for comprehensive topic research."""

    def __init__(self):
        metadata = ToolMetadata(
            name="research_topic",
            description="Perform comprehensive research on a topic with multiple queries and sources",
            category=ToolCategory.SEARCH_ANALYSIS,
            version="1.0.0",
            parameters=[
                ToolParameter(
                    name="topic",
                    type=str,
                    description="Topic to research",
                    required=True,
                ),
                ToolParameter(
                    name="aspects",
                    type=list[str],
                    description="Specific aspects to investigate",
                    required=False,
                    default=None,
                ),
            ],
            required_permissions=[ToolPermission.WEB_ACCESS],
            allowed_modes=[ExecutionMode.EXECUTION],
            tags=["research", "web", "information"],
        )
        super().__init__(metadata)

        self._search_tool = None

    def _get_search_tool(self):
        if self._search_tool is None:
            from vetinari.tools.web_search_tool import get_search_tool

            self._search_tool = get_search_tool()
        return self._search_tool

    def execute(self, **kwargs) -> ToolResult:
        """Execute.

        Returns:
            The ToolResult result.
        """
        topic = kwargs.get("topic", "")
        aspects = kwargs.get("aspects")

        try:
            search_tool = self._get_search_tool()
            result = search_tool.research_topic(topic, aspects)

            return ToolResult(
                success=True,
                output=result,
            )
        except Exception:
            logger.exception("Topic research failed for topic %s", _redact_text(str(topic)))
            return ToolResult(
                success=False,
                output=None,
                error="Topic research failed",
            )


class CodeExecutionToolWrapper(Tool):
    """Wrapper for code execution sandbox."""

    def __init__(self):
        metadata = ToolMetadata(
            name="execute_code",
            description="Execute code in a sandboxed environment",
            category=ToolCategory.CODE_EXECUTION,
            version="1.0.0",
            parameters=[
                ToolParameter(
                    name="code",
                    type=str,
                    description="Code to execute",
                    required=True,
                ),
                ToolParameter(
                    name="language",
                    type=str,
                    description="Programming language",
                    required=False,
                    default="python",
                ),
                ToolParameter(
                    name="timeout",
                    type=int,
                    description="Execution timeout in seconds",
                    required=False,
                    default=60,
                ),
            ],
            required_permissions=[ToolPermission.CODE_EXECUTION],
            allowed_modes=[ExecutionMode.EXECUTION],
            tags=["code", "execution", "sandbox"],
        )
        super().__init__(metadata)

        self._executor = None

    def _get_executor(self):
        if self._executor is None:
            from vetinari.code_sandbox import get_subprocess_executor

            self._executor = get_subprocess_executor()
        return self._executor

    def execute(self, **kwargs) -> ToolResult:
        """Execute.

        Returns:
            The ToolResult result.
        """
        code = kwargs.get("code", "")
        language = kwargs.get("language", "python")
        timeout = kwargs.get("timeout", 60)

        try:
            if str(language).lower() != "python":
                return ToolResult(
                    success=False,
                    output=None,
                    error="Only python code execution is supported by the canonical sandbox manager",
                )

            from vetinari.sandbox_manager import get_sandbox_manager

            result = get_sandbox_manager().execute(
                code=code,
                sandbox_type="subprocess",
                timeout=timeout,
                context={},
                client_id="tool_registry.execute_code",
            )
            output = (
                result.to_dict()
                if hasattr(result, "to_dict")
                else {
                    "success": result.success,
                    "output": result.result,
                    "error": result.error,
                }
            )

            return ToolResult(
                success=output.get("success", False),
                output=output,
                error=None if output.get("success", False) else "Sandbox execution failed",
            )
        except Exception:
            logger.exception("Sandbox execution failed for language %r", language)
            return ToolResult(
                success=False,
                output=None,
                error="Sandbox execution failed",
            )
