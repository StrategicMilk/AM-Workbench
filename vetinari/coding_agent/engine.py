"""Vetinari Coding Agent - In-process coding agent for code generation.

This module provides the compatibility facade for the coding agent public API:
- CodeAgentEngine: In-process coding agent using internal LM
- make_code_agent_task: Factory for creating coding AgentTasks
- CodeArtifact: Data model for generated code artifacts
- Integration with UnifiedMemoryStore for provenance

CodeTask was consolidated into AgentTask in M4 ontology unification.
Use ``make_code_agent_task()`` to create coding tasks.
"""

from __future__ import annotations

import logging
import os
import threading

from vetinari.code_sandbox import CodeSandbox

from .engine_execution import CodeAgentExecutionMixin, CodeAgentValidationMixin
from .engine_generation import CodeAgentGenerationMixin
from .engine_models import (
    CodeArtifact,
    CodingArtifactType,
    CodingTaskType,
    StatusEnum,
    _CodeTask,
    _module_name_from_target,
    make_code_agent_task,
)

logger = logging.getLogger(__name__)


class CodeAgentEngine(CodeAgentExecutionMixin, CodeAgentGenerationMixin, CodeAgentValidationMixin):
    """In-process coding agent engine.

    This MVP uses an internal LM wrapper to draft code.
    It can also delegate to an external bridge for heavier tasks.
    """

    def __init__(self, lm_provider: str = "internal"):
        self.lm_provider = lm_provider
        self.enabled = os.environ.get("CODING_AGENT_ENABLED", "true").lower() in ("1", "true", "yes")
        self._sandbox = CodeSandbox(network_isolation=True)

        logger.info(
            "CodeAgentEngine initialized (provider=%s, enabled=%s)",
            lm_provider,
            self.enabled,
        )

    def is_available(self) -> bool:
        """Check if the coding agent is available."""
        return self.enabled

    def _repo_map_context(self, task: _CodeTask) -> str:
        """Return bounded task context without walking the entire repository.

        Coding-agent tasks often arrive with the default repository path, which
        used to trigger a full repo-map scan during generation. Generated-code
        safety probes must stay bounded and branch-discriminating, so the engine
        supplies the useful task-local routing hints directly from task metadata.
        """
        task_context = getattr(task, "context", {}) or {}
        repo_path = getattr(task, "repo_path", "") or task_context.get("repo_path") or "."
        target_files = list(getattr(task, "target_files", None) or task_context.get("target_files") or [])
        task_type = getattr(getattr(task, "type", None), "value", None) or task_context.get("task_type") or "unknown"

        lines = [
            "Repository context is intentionally bounded for coding-agent generation.",
            f"task_type: {task_type}",
            f"repo_path: {repo_path}",
        ]
        if target_files:
            lines.append("target_files:")
            lines.extend(f"- {target_file}" for target_file in target_files)
        else:
            lines.append("target_files: none")
        return "\n".join(lines)


_coding_agent: CodeAgentEngine | None = None
_coding_agent_lock = threading.Lock()


def get_coding_agent() -> CodeAgentEngine:
    """Get or create the global coding agent instance.

    Returns:
        The singleton CodeAgentEngine, creating one with default settings on first call.
    """
    global _coding_agent
    if _coding_agent is None:
        with _coding_agent_lock:
            if _coding_agent is None:
                _coding_agent = CodeAgentEngine()
    return _coding_agent


def init_coding_agent(lm_provider: str = "internal") -> CodeAgentEngine:
    """Initialize a new coding agent instance, replacing any existing singleton.

    Args:
        lm_provider: LM provider identifier passed to CodeAgentEngine
            (e.g. ``"internal"`` for the built-in adapter).

    Returns:
        The newly created CodeAgentEngine, now stored as the global singleton.
    """
    global _coding_agent
    _coding_agent = CodeAgentEngine(lm_provider=lm_provider)
    return _coding_agent


__all__ = [
    "CodeAgentEngine",
    "CodeArtifact",
    "CodingArtifactType",
    "CodingTaskType",
    "StatusEnum",
    "_CodeTask",
    "_module_name_from_target",
    "get_coding_agent",
    "init_coding_agent",
    "make_code_agent_task",
]
