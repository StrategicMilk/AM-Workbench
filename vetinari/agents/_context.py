"""Typed context bag for agent initialization.

Replaces the untyped ``dict[str, Any]`` previously passed to
``BaseAgent.initialize()``.  The TypedDict is intentionally total=False
so callers can supply only the keys they have without breaking existing
code that omits optional services.

This is step 2 of the pipeline: after construction the context is handed
to ``BaseAgent.initialize()`` which extracts the shared services.
"""

from __future__ import annotations

from collections import UserDict
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from vetinari.adapters.manager import AdapterManager
    from vetinari.search.web import WebSearchTool
    from vetinari.workbench.tools.registry import ToolRegistry


class AgentContext(UserDict[str, Any]):
    """Typed initialization context for Vetinari agents.

    Uses ``UserDict`` to keep mapping-style key access without inheriting from
    the concrete ``dict`` type.

    Keys:
        adapter_manager: AdapterManager used for LLM inference dispatching.
        web_search: WebSearchTool for online research during task execution.
        tool_registry: ToolRegistry containing registered callable tools.

    Extra keys are allowed and forwarded to the agent unchanged, allowing
    agent-specific context to piggyback on the same dict.
    """

    # -- typed accessors -------------------------------------------------------

    @property
    def adapter_manager(self) -> AdapterManager | None:
        """LLM adapter manager, or None if not yet provisioned."""
        return cast("AdapterManager | None", self.get("adapter_manager"))

    @property
    def web_search(self) -> WebSearchTool | None:
        """Web search tool, or None if not configured."""
        return cast("WebSearchTool | None", self.get("web_search"))

    @property
    def tool_registry(self) -> ToolRegistry | None:
        """Tool registry, or None if not configured."""
        return cast("ToolRegistry | None", self.get("tool_registry"))
