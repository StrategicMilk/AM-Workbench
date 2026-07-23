"""Canonical display labels for public Workbench tokens."""

from __future__ import annotations

import re
from collections.abc import Iterable
from enum import Enum


class DisplayLabelError(KeyError):
    """Raised when a user-facing token has no registered label."""


_DISPLAY_LABELS: dict[str, str] = {
    "accepted": "Accepted",
    "FOREMAN": "Foreman",
    "WORKER": "Worker",
    "INSPECTOR": "Inspector",
    "TRAINING": "Training",
    "RELEASE": "Release",
    "WORKBENCH": "Workbench",
    "adapter": "Adapter",
    "activate": "Activate",
    "all": "All",
    "allow": "Allow",
    "analysis": "Analysis",
    "apply_diff": "Apply diff",
    "approval": "Approval",
    "architecture": "Architecture",
    "artifact": "Artifact",
    "approval_required": "Approval required",
    "awq": "AWQ",
    "automation": "Automation",
    "block": "Block",
    "blocked": "Blocked",
    "blocked_by_export_boundary": "Blocked by export boundary",
    "build": "Build",
    "cancel": "Cancel",
    "checkpoint": "Checkpoint",
    "code_discovery": "Code discovery",
    "code_review": "Code review",
    "collection_disabled": "Collection disabled",
    "complete": "Complete",
    "conflict": "Conflict",
    "context_management": "Context management",
    "cost": "Cost",
    "correct": "Correct",
    "critical": "Critical",
    "data_card": "Data card",
    "dataset": "Dataset",
    "dataset_card": "Dataset card",
    "decision": "Decision",
    "degraded": "Degraded",
    "default": "Default",
    "delete": "Delete",
    "dependency_resolution": "Dependency resolution",
    "discovery": "Discovery",
    "documentation": "Documentation",
    "documentation_generation": "Documentation generation",
    "domain_research": "Domain research",
    "draft": "Draft",
    "draft_only": "Draft only",
    "edit_file": "Edit file",
    "effort_accounting": "Effort accounting",
    "explain_code": "Explain code",
    "evidence": "Evidence",
    "evidenced_by": "Evidenced by",
    "eval_suite": "Eval suite",
    "export_blocked": "Export blocked",
    "explicit": "Explicit",
    "fast": "Fast",
    "friction": "Friction",
    "generate_code": "Generate code",
    "generation": "Generation",
    "gguf": "GGUF",
    "gptq": "GPTQ",
    "graph_node": "Graph node",
    "goal_decomposition": "Goal decomposition",
    "governance": "Governance",
    "high": "High",
    "ignored_recommendation": "Ignored recommendation",
    "implicit": "Implicit",
    "llama_cpp": "llama.cpp",
    "local_only": "Local only",
    "low": "Low",
    "memory": "Memory",
    "medium": "Medium",
    "model": "Model",
    "model_card": "Model card",
    "nim": "NIM",
    "optimization": "Optimization",
    "pass": "Pass",
    "policy": "Policy",
    "plan_consolidation": "Plan consolidation",
    "preference_candidate": "Preference candidate",
    "private": "Private",
    "project_knowledge": "Project knowledge",
    "prompt": "Prompt",
    "prompt_card": "Prompt card",
    "prompt_optimization": "Prompt optimization",
    "question_debt": "Question debt",
    "quarantine": "Quarantine",
    "queue_review": "Queue for review",
    "ready": "Ready",
    "read_file": "Read file",
    "recalled_by_run": "Recalled by run",
    "recommendation_feedback": "Recommendation feedback",
    "registry-only": "Registry only",
    "release_export": "Release export",
    "repeated_workflow": "Repeated workflow",
    "review": "Review",
    "review-required": "Review required",
    "review_code": "Review code",
    "route": "Route",
    "run_tests": "Run tests",
    "safetensors": "Safetensors",
    "search_codebase": "Search codebase",
    "shareable": "Shareable",
    "slow": "Slow",
    "source": "Source",
    "stale": "Stale",
    "start": "Start",
    "stop": "Stop",
    "suggest": "Suggest",
    "supersede": "Supersede",
    "synthesis": "Synthesis",
    "system_card": "System card",
    "task_sequencing": "Task sequencing",
    "testing": "Testing",
    "tool": "Tool",
    "trust_boundary": "Trust boundary",
    "unverified": "Unverified",
    "unknown": "Unknown",
    "used_by_run": "Used by run",
    "user_question": "User question",
    "vllm": "vLLM",
    "warn": "Warn",
    "user_clarification": "User clarification",
    "verification": "Verification",
}

_SEPARATORS_RE = re.compile(r"[_\-.]+")


def display_label(token: str | Enum) -> str:
    """Return the registered user-facing label for a token.

    Returns:
        Registered display label for the token value.

    Raises:
        DisplayLabelError: If the token has no registered label.
    """
    key = token.value if isinstance(token, Enum) else str(token)
    label = _DISPLAY_LABELS.get(key)
    if label is None:
        raise DisplayLabelError(f"missing display label for token: {key}")
    return label


def humanize_identifier(token: str | Enum) -> str:
    """Return a readable label for extensible, user-supplied identifiers.

    Returns:
        Human-readable label derived from the token value.

    Raises:
        DisplayLabelError: If the token is empty after trimming separators.
    """
    key = token.value if isinstance(token, Enum) else str(token)
    words = [part for part in _SEPARATORS_RE.split(key.strip()) if part]
    if not words:
        raise DisplayLabelError("missing display label for empty token")
    return " ".join(words).capitalize()


def display_label_or_humanize(token: str | Enum) -> str:
    """Return a registered label, or humanize an extensible identifier.

    Returns:
        Registered display label when available, otherwise a humanized label.
    """
    key = token.value if isinstance(token, Enum) else str(token)
    return _DISPLAY_LABELS.get(key) or humanize_identifier(key)


def display_labels_for(tokens: Iterable[str | Enum]) -> tuple[str, ...]:
    """Return display labels for every token, failing closed on gaps."""
    return tuple(display_label(token) for token in tokens)


def has_display_label(token: str | Enum) -> bool:
    """Return whether a token has a registered user-facing label.

    Returns:
        True when the token is present in the display-label registry.
    """
    key = token.value if isinstance(token, Enum) else str(token)
    return key in _DISPLAY_LABELS


__all__ = [
    "DisplayLabelError",
    "display_label",
    "display_label_or_humanize",
    "display_labels_for",
    "has_display_label",
    "humanize_identifier",
]
