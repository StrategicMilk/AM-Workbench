"""Vetinari Tools Package.

Contains actual tool implementations (file I/O, git, web search).
Skill implementations live in vetinari/skills/.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

from vetinari.tools.consolidated_operations import (
    InvestigateTaskResult,
    PrepareModelResult,
    investigate_task,
    prepare_model,
)
from vetinari.tools.file_tool import FileOperationsTool
from vetinari.tools.git_tool import GitOperationsTool
from vetinari.tools.web_search_tool import WebSearchTool

if TYPE_CHECKING:
    from vetinari.tools import semgrep_tool as semgrep_tool
    from vetinari.tools.brave_search_tool import BraveSearchTool

__all__ = [
    "BraveSearchTool",
    "FileOperationsTool",
    "GitOperationsTool",
    "InvestigateTaskResult",
    "PrepareModelResult",
    "WebSearchTool",
    "investigate_task",
    "prepare_model",
    "semgrep_tool",
]


def __getattr__(name: str) -> object:
    """Resolve reload-sensitive tool exports from their live modules."""
    if name == "BraveSearchTool":
        from vetinari.tools.brave_search_tool import BraveSearchTool

        return BraveSearchTool
    if name == "semgrep_tool":
        return importlib.import_module("vetinari.tools.semgrep_tool")
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
