"""ACON-Style Context Condenser for Agent Handoffs.

Implements progressive context condensation at agent-to-agent handoffs.
Instead of carrying forward full output from one agent to the next,
each handoff extracts only the essential information.

This reduces context accumulation across the three-agent pipeline:
    Foreman -> Worker: task spec + criteria + file paths
    Worker -> Inspector: changed code + what and why
    Inspector -> Worker (rework): specific issues + file refs

Based on ACON (Agentic Context Optimization, Microsoft 2025).
"""

from __future__ import annotations

import json
import logging
import threading
from typing import Any

from vetinari.constants import TRUNCATE_CONDENSED
from vetinari.types import AgentType

logger = logging.getLogger(__name__)


# Maximum length of condensed output (characters)
_MAX_CONDENSED_LENGTH = TRUNCATE_CONDENSED  # ~1000 tokens


class ContextCondenser:
    """Progressive context condensation for agent handoffs.

    Extracts only the information the receiving agent needs from the
    sending agent's output, reducing context by 50-70% at each handoff.
    """

    def condense_for_handoff(
        self,
        from_agent: str,
        to_agent: str,
        result_output: Any,
        result_metadata: dict[str, Any] | None = None,
    ) -> str:
        """Summarize agent output for the next agent in the pipeline.

        Uses match/case on known agent pairs to extract the most relevant
        information. Falls back to generic structured extraction for
        unknown pairs.

        Args:
            from_agent: Sending agent type value (e.g., "FOREMAN").
            to_agent: Receiving agent type value (e.g., "WORKER").
            result_output: The output from the sending agent (str or dict).
            result_metadata: Optional metadata from the AgentResult.

        Returns:
            Condensed context string ready for the receiving agent.
        """
        metadata = result_metadata or {}

        match (from_agent.upper(), to_agent.upper()):
            case (AgentType.FOREMAN.value, AgentType.WORKER.value):
                return self._condense_planner_to_builder(result_output, metadata)

            case (AgentType.WORKER.value, AgentType.INSPECTOR.value):
                return self._condense_builder_to_quality(result_output, metadata)

            case (AgentType.INSPECTOR.value, AgentType.WORKER.value):
                return self._condense_quality_to_builder_rework(result_output, metadata)

            case _:
                return self._condense_generic(result_output, metadata)

    def _condense_planner_to_builder(
        self,
        output: Any,
        metadata: dict[str, Any],
    ) -> str:
        """Extract task spec + acceptance criteria + file paths for Worker.

        Worker only needs: what to build, where, and how to verify.
        """
        parts = ["## Task Assignment from Planner"]

        if isinstance(output, dict):
            if "description" in output:
                parts.append(f"\n**Task:** {output['description']}")
            if "acceptance_criteria" in output:
                parts.append("\n**Acceptance Criteria:**")
                parts.extend(f"- {criterion}" for criterion in output["acceptance_criteria"])
            if "files" in output:
                parts.append(f"\n**Files:** {', '.join(output['files'])}")
            if "dependencies" in output:
                parts.append(f"\n**Dependencies:** {', '.join(output['dependencies'])}")
        elif isinstance(output, str):
            parts.append(f"\n{self._truncate(output)}")

        return self._cap_length("\n".join(parts))

    def _condense_builder_to_quality(
        self,
        output: Any,
        metadata: dict[str, Any],
    ) -> str:
        """Extract changed code + what was changed and why for Inspector.

        Inspector needs: the code changes, test results, and rationale.
        """
        parts = ["## Build Output for Review"]

        if isinstance(output, dict):
            if "changes" in output:
                parts.append("\n**Changes:**")
                parts.extend(f"- {change}" for change in output.get("changes", []))
            if "files_modified" in output:
                parts.append(f"\n**Files Modified:** {', '.join(output['files_modified'])}")
            if "test_results" in output:
                parts.append(f"\n**Test Results:** {output['test_results']}")
            if "summary" in output:
                parts.append(f"\n**Summary:** {output['summary']}")
        elif isinstance(output, str):
            parts.append(f"\n{self._truncate(output)}")

        return self._cap_length("\n".join(parts))

    def _condense_quality_to_builder_rework(
        self,
        output: Any,
        metadata: dict[str, Any],
    ) -> str:
        """Extract specific issues + file references for Worker rework.

        Worker needs: exactly what to fix, where, and how.
        """
        parts = ["## Rework Instructions from Quality"]

        if isinstance(output, dict):
            if "issues" in output:
                parts.append("\n**Issues to Fix:**")
                for i, issue in enumerate(output["issues"], 1):
                    if isinstance(issue, dict):
                        file_ref = issue.get("file", "unknown")
                        line_ref = issue.get("line", "")
                        desc = issue.get("description", str(issue))
                        loc = f"{file_ref}:{line_ref}" if line_ref else file_ref
                        parts.append(f"{i}. [{loc}] {desc}")
                    else:
                        parts.append(f"{i}. {issue}")
            if "suggestions" in output:
                parts.append("\n**Suggested Fixes:**")
                parts.extend(f"- {suggestion}" for suggestion in output["suggestions"])
        elif isinstance(output, str):
            parts.append(f"\n{self._truncate(output)}")

        return self._cap_length("\n".join(parts))

    def _condense_generic(
        self,
        output: Any,
        metadata: dict[str, Any],
    ) -> str:
        """Extract structured output, drop verbose reasoning.

        Default handler for unknown agent pairs.
        """
        parts = ["## Agent Output Summary"]

        if isinstance(output, dict):
            # Extract key fields, skip verbose reasoning/trace
            skip_keys = {"reasoning", "trace", "debug", "raw_output", "full_text"}
            for key, value in output.items():
                if key.lower() in skip_keys:
                    continue
                if isinstance(value, str) and len(value) > 500:
                    value = value[:497] + "\n\n[...output truncated...]"
                parts.append(f"**{key}:** {value}")
        elif isinstance(output, str):
            parts.append(f"\n{self._truncate(output)}")
        else:
            try:
                text = json.dumps(output, default=str)
                parts.append(f"\n{self._truncate(text)}")
            except (TypeError, ValueError):
                parts.append(f"\n{self._truncate(str(output))}")

        return self._cap_length("\n".join(parts))

    def _truncate(self, text: str, max_len: int = 3000) -> str:
        """Truncate text to max length with a visible truncation indicator.

        Args:
            text: Text to truncate.
            max_len: Maximum character length.

        Returns:
            Truncated text with "[...output truncated...]" suffix if shortened,
            otherwise the original text unchanged.
        """
        _indicator = "\n\n[...output truncated...]"
        if len(text) <= max_len:
            return text
        return text[: max_len - len(_indicator)] + _indicator

    @staticmethod
    def _cap_length(text: str) -> str:
        """Cap total condensed output length with a visible truncation indicator.

        Args:
            text: The assembled condensed text.

        Returns:
            Text capped at _MAX_CONDENSED_LENGTH with "[...output truncated...]"
            appended when the cap is applied.
        """
        _indicator = "\n\n[...output truncated...]"
        if len(text) <= _MAX_CONDENSED_LENGTH:
            return text
        return text[: _MAX_CONDENSED_LENGTH - len(_indicator)] + _indicator


# ── Singleton ──────────────────────────────────────────────────────────

_condenser: ContextCondenser | None = None
_condenser_lock = threading.Lock()


def get_context_condenser() -> ContextCondenser:
    """Return the singleton ContextCondenser instance.

    Returns:
        The singleton ContextCondenser.
    """
    global _condenser
    if _condenser is None:
        with _condenser_lock:
            if _condenser is None:
                _condenser = ContextCondenser()
    return _condenser
