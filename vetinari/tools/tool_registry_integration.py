"""Tool Registry Integration for Vetinari.

Provides convenience wrappers that register tools in the tool registry:
- Web Search Tool
- Code Sandbox
- Memory Tools
- Model Router Tools
- Orchestration Tools
"""

from __future__ import annotations

import logging
import threading

from vetinari.boundary_guards import assert_dependency_success
from vetinari.tool_interface import (
    Tool,
    get_tool_registry,
)
from vetinari.tools.tool_registry_wrappers import (
    CodeExecutionToolWrapper as CodeExecutionToolWrapper,
)
from vetinari.tools.tool_registry_wrappers import (
    GeneratePlanToolWrapper as GeneratePlanToolWrapper,
)
from vetinari.tools.tool_registry_wrappers import (
    MemoryRecallToolWrapper as MemoryRecallToolWrapper,
)
from vetinari.tools.tool_registry_wrappers import (
    MemoryRememberToolWrapper as MemoryRememberToolWrapper,
)
from vetinari.tools.tool_registry_wrappers import (
    ModelSelectToolWrapper as ModelSelectToolWrapper,
)
from vetinari.tools.tool_registry_wrappers import (
    ResearchTopicToolWrapper as ResearchTopicToolWrapper,
)
from vetinari.tools.tool_registry_wrappers import (
    WebSearchToolWrapper as WebSearchToolWrapper,
)
from vetinari.tools.tool_registry_wrappers import (
    _redact_text as _redact_text,
)

logger = logging.getLogger(__name__)


def _make_file_tool() -> Tool | None:
    """Lazily create a FileOperationsTool instance rooted at the project root.

    The project root is resolved as two levels above the ``vetinari/`` package
    directory (i.e. the repository root).  This avoids using ``os.getcwd()``,
    which is process-dependent and unsafe for sandboxed file operations.
    """
    try:
        from pathlib import Path

        from vetinari.tools.file_tool import FileOperationsTool

        # vetinari/tools/tool_registry_integration.py → up 3 parents → repo root
        project_root = Path(__file__).resolve().parents[2]
        return FileOperationsTool(str(project_root))
    except Exception as e:
        logger.warning("FileOperationsTool unavailable — project root resolution failed: %s", e)
        return None


def _make_git_tool() -> Tool | None:
    """Lazily create a GitOperationsTool instance rooted at the project repository.

    The repo root is resolved as two levels above the ``vetinari/`` package
    directory (i.e. the repository root).  This avoids using ``os.getcwd()``,
    which is process-dependent and unsafe for sandboxed git operations.
    """
    try:
        from pathlib import Path

        from vetinari.tools.git_tool import GitOperationsTool

        # vetinari/tools/tool_registry_integration.py → up 3 parents → repo root
        repo_root = Path(__file__).resolve().parents[2]
        return GitOperationsTool(str(repo_root))
    except Exception as e:
        logger.warning("GitOperationsTool unavailable — repo root resolution failed: %s", e)
        return None


def _make_plan_tool() -> Tool | None:
    """Lazily create a GeneratePlanToolWrapper only when planning is available.

    Importing GeneratePlanToolWrapper unconditionally would register the tool
    even when the planning subsystem is absent, causing confusing
    "capability unavailable" errors at call time rather than at registration.
    """
    try:
        return GeneratePlanToolWrapper()
    except Exception as e:
        logger.warning("GeneratePlanToolWrapper unavailable — planning capability absent: %s", e)
        return None


def register_all_tools() -> None:
    """Register all tools in the global registry.

    Returns:
        The len result.
    """
    registry = get_tool_registry()

    # List of tool instances to register
    tools = [
        WebSearchToolWrapper(),
        ResearchTopicToolWrapper(),
        CodeExecutionToolWrapper(),
        MemoryRecallToolWrapper(),
        MemoryRememberToolWrapper(),
        ModelSelectToolWrapper(),
    ]

    # Concrete tools (file I/O, git, planning) — may fail gracefully
    for factory in (_make_file_tool, _make_git_tool, _make_plan_tool):
        tool = factory()
        if tool is not None:
            tools.append(tool)

    # Register each tool
    failed_tools: list[str] = []
    for tool in tools:
        try:
            registry.register(tool)
            logger.info("Registered tool: %s", tool.metadata.name)
        except Exception as e:
            failed_tools.append(tool.metadata.name)
            logger.error("Failed to register tool %s: %s", tool.metadata.name, e)
    if failed_tools:
        assert_dependency_success(
            False,
            dependency_id=f"tool registration failed for: {', '.join(failed_tools)}",
        )

    return len(tools)


def _auto_register() -> bool:
    """Register all tools with the tool registry.

    Returns:
        True if registration completed without error, False on failure.
        A False return allows the next call to ``ensure_tools_registered``
        to retry rather than permanently skipping registration.
    """
    try:
        count = register_all_tools()
        logger.info("Auto-registered %s tools", count)
        return True
    except Exception as exc:
        logger.warning(
            "Auto-registration failed — tools not registered, next call will retry: %s",
            exc,
        )
        return False


# Flag is only set True after a successful registration attempt so that
# a transient failure on the first call allows a retry on the next call.
_auto_registered: bool = False
_auto_register_lock: threading.Lock = threading.Lock()


def ensure_tools_registered() -> None:
    """Register tools at most once, retrying after transient failures.

    Uses double-checked locking for thread safety.  Unlike a strict
    "exactly-once" pattern, the flag is only committed on success so that
    a transient registration error on one call does not permanently prevent
    tools from being registered on a subsequent call.
    """
    global _auto_registered
    if not _auto_registered:
        with _auto_register_lock:
            if not _auto_registered:
                # Only mark as registered when the attempt actually succeeded.
                _auto_registered = _auto_register()
                if not _auto_registered:
                    assert_dependency_success(False, dependency_id="tool auto-registration")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    # Manual registration for testing
    count = register_all_tools()
    logger.info("Registered %d tools", count)

    # List registered tools
    registry = get_tool_registry()
    logger.info("Registered tools:")
    for tool in registry.list_tools():
        logger.info("  - %s: %s", tool.metadata.name, tool.metadata.description)
