"""Vetinari Coding Agent Package.

This package provides:
- CodeAgentEngine: In-process coding agent
- make_code_agent_task: Factory for creating coding AgentTasks
- CodeArtifact: Generated code artifacts

Legacy callers should use ``make_code_agent_task()`` to create normalized
``AgentTask`` payloads.
"""

from __future__ import annotations

from .engine import (
    CodeAgentEngine,
    CodeArtifact,
    CodingTaskType,
    StatusEnum,
    get_coding_agent,
    init_coding_agent,
    make_code_agent_task,
)

LEGACY_API_REPLACEMENTS = {
    "CodeTask": "AgentTask via make_code_agent_task",
}

__all__ = [
    "LEGACY_API_REPLACEMENTS",
    "CodeAgentEngine",
    "CodeArtifact",
    "CodingTaskType",
    "StatusEnum",
    "get_coding_agent",
    "init_coding_agent",
    "make_code_agent_task",
]
