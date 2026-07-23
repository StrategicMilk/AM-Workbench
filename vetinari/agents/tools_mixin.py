"""Tool access mixin for Vetinari agents.

Provides web search, tool registry access, and code context extraction
helpers to any agent that inherits from BaseAgent.  These methods rely on
``self._web_search``, ``self._tool_registry``, and ``self._log()`` being
present (supplied by BaseAgent at runtime).
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any

from vetinari.types import AgentType

logger = logging.getLogger(__name__)


# Role-appropriate tool deny list (principle of least privilege)
_TOOL_DENY: dict[str, set[str]] = {
    AgentType.FOREMAN.value: {"file_write", "code_execute", "build", "git_operations", "deployment", "database_write"},
    AgentType.WORKER.value: set(),
    AgentType.INSPECTOR.value: {
        "file_write",
        "code_execute",
        "build",
        "deployment",
        "database_write",
        "git_operations",
    },
}


def _text_fingerprint(value: str) -> str:
    digest = hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()[:12]
    return f"len={len(value)} sha256={digest}"


class AgentToolAccess:
    """Tool access capabilities mixed into BaseAgent.

    All methods rely on ``self._web_search``, ``self._tool_registry``, and
    ``self._log()`` being present on the host class (supplied by BaseAgent).
    """

    # ------------------------------------------------------------------
    # Web search helper
    # ------------------------------------------------------------------

    def _search(self, query: str, max_results: int = 5) -> list[dict[str, Any]]:
        """Perform a web search and return a list of result dicts.

        Each result dict has keys: title, url, snippet, source_reliability.
        Returns an empty list if no search tool is available.

        Args:
            query: The search query string.
            max_results: Maximum number of results to return.

        Returns:
            List of result dicts with title, url, snippet, source_reliability
            keys, or an empty list when no search tool is configured.
        """
        if self._web_search is None:
            try:
                from vetinari.tools.web_search_tool import get_search_tool

                self._web_search = get_search_tool()
            except Exception:
                logger.warning("Web search tool unavailable", exc_info=True)
                return []
        try:
            response = self._web_search.search(query, max_results=max_results)
            return [
                {
                    "title": r.title,
                    "url": r.url,
                    "snippet": r.snippet,
                    "source_reliability": r.source_reliability,
                }
                for r in response.results
            ]
        except Exception as e:
            query_fingerprint = _text_fingerprint(query)
            error_type = type(e).__name__
            self._log("warning", "Web search failed for query %s: %s", query_fingerprint, error_type)
            logger.warning(
                "Web search failed; query=%s error_type=%s "
                "impact=returning empty results and proceeding without search data",
                query_fingerprint,
                error_type,
            )
            return []

    # ------------------------------------------------------------------
    # Tool Registry access helpers
    # ------------------------------------------------------------------

    def _use_tool(self, tool_name: str, **kwargs: Any) -> dict[str, Any] | None:
        """Execute a registered tool by name, enforcing role-based deny list.

        Delegates to the ToolRegistry injected via ``initialize(context)``.
        Returns ``None`` when the registry is unavailable or the tool is
        not found, allowing callers to fall back gracefully.

        Args:
            tool_name: Name of the tool in the ToolRegistry.
            **kwargs: Parameters forwarded to ``Tool.run()``.

        Returns:
            Dict with ``success``, ``output``, ``error``, and
            ``execution_time_ms`` keys, or ``None`` if the tool cannot
            be resolved.

        Raises:
            CapabilityNotAvailable: If the tool is on this agent's deny list.
        """
        # Enforce deny list — must agree with _list_tools() filtering
        agent_type = getattr(self, "agent_type", None)
        if agent_type is not None:
            type_value = agent_type.value if hasattr(agent_type, "value") else str(agent_type)
            denied = _TOOL_DENY.get(type_value)
            if denied and tool_name in denied:
                from vetinari.exceptions import CapabilityNotAvailable

                raise CapabilityNotAvailable(
                    f"Tool {tool_name!r} is denied for agent type {type_value!r}. Denied tools: {sorted(denied)}"
                )

        if self._tool_registry is None:
            self._log("debug", "Tool registry unavailable, cannot use tool '%s'", tool_name)
            return None

        tool = self._tool_registry.get(tool_name)
        if tool is None:
            self._log("warning", "Tool '%s' not found in registry", tool_name)
            return None

        try:
            result = tool.run(agent_type=agent_type, **kwargs)
            return result.to_dict()
        except Exception as exc:
            self._log("error", "Tool '%s' raised an exception: %s", tool_name, exc)
            logger.warning(
                "Tool %r execution raised an unexpected error: %s — returning failure dict, caller should handle gracefully",
                tool_name,
                exc,
            )
            return {"success": False, "output": None, "error": str(exc), "execution_time_ms": 0, "metadata": {}}

    def _has_tool(self, tool_name: str) -> bool:
        """Check whether a named tool is available in the registry.

        Args:
            tool_name: The tool name to look up.

        Returns:
            True if the tool exists in the registry, False otherwise.
        """
        if self._tool_registry is None:
            return False
        return self._tool_registry.get(tool_name) is not None

    def _list_tools(self) -> list[str]:
        """Return tool names available to this agent, filtered by role deny list.

        Returns:
            List of permitted tool name strings.
        """
        if self._tool_registry is None:
            return []
        all_tools = [t.metadata.name for t in self._tool_registry.list_tools()]
        agent_type = getattr(self, "agent_type", None)
        if agent_type is None:
            return all_tools
        type_value = agent_type.value if hasattr(agent_type, "value") else str(agent_type)
        denied = _TOOL_DENY.get(type_value)
        if denied is None:
            return all_tools
        return [name for name in all_tools if name not in denied]

    # ------------------------------------------------------------------
    # Code context helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_code_context(
        file_paths: list[str],
        keywords: list[str],
        budget_chars: int = 2000,
    ) -> str:
        """Extract only relevant code context using grep.

        Use instead of reading whole files to reduce token usage by 40-60%.

        Args:
            file_paths: List of file paths to search within.
            keywords: Keywords to look for when selecting context lines.
            budget_chars: Maximum number of characters to return in total.

        Returns:
            Concatenated relevant context strings separated by double newlines.
        """
        from vetinari.grep_context import get_grep_context

        gc = get_grep_context()
        parts = []
        remaining = budget_chars
        for fp in file_paths:
            if remaining <= 0:
                break
            chunk = gc.extract_relevant_context(fp, keywords, budget_chars=remaining)
            if chunk:
                parts.append(chunk)
                remaining -= len(chunk)
        return "\n\n".join(parts)

    @staticmethod
    def _grep_patterns(
        file_paths: list[str],
        patterns: list[str],
        context_lines: int = 3,
    ) -> str:
        """Extract lines matching patterns with surrounding context.

        Args:
            file_paths: List of file paths to search within.
            patterns: Regex patterns to match against file content.
            context_lines: Number of lines of context to include around each match.

        Returns:
            Formatted string of pattern matches suitable for inclusion in a prompt.
        """
        from vetinari.grep_context import get_grep_context

        gc = get_grep_context()
        matches = gc.extract_patterns(file_paths, patterns, context_lines)
        return gc.format_for_prompt(matches)

    @staticmethod
    def _grep_imports(file_path: str) -> str:
        """Extract only the import statements from a source file.

        Returns a compact import-only view (~5% of file size) useful when an
        agent needs to understand a file's dependencies without loading its
        full content.

        Args:
            file_path: Absolute or relative path to the source file.

        Returns:
            Newline-joined string of ``import`` and ``from ... import`` lines,
            or an empty string if the file does not exist.
        """
        from vetinari.grep_context import get_grep_context

        return get_grep_context().extract_imports(file_path)

    @staticmethod
    def _grep_security_patterns(file_paths: list[str]) -> str:
        """Extract lines matching common security anti-patterns from source files.

        Searches for hardcoded credentials, unsafe eval/exec/pickle, SQL
        injection risks, and other common security issues.  Results are
        formatted as a prompt-ready string suitable for passing directly to
        the Inspector agent.

        Args:
            file_paths: Source files to scan for security issues.

        Returns:
            Formatted string of pattern matches (empty when none found).
        """
        from vetinari.grep_context import get_grep_context

        gc = get_grep_context()
        matches = gc.extract_security_patterns(file_paths)
        return gc.format_for_prompt(matches)
