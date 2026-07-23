"""Coding task compatibility helpers."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class CodingTask:
    """Single coding task."""

    repo_root: str
    description: str = ""
    target_file: str | None = None
    module_name: str | None = None

    def __repr__(self) -> str:
        """Return a compact task representation."""
        return f"CodingTask(repo_root={self.repo_root!r}, target_file={self.target_file!r})"

    def __post_init__(self) -> None:
        if not isinstance(self.repo_root, str):
            raise ValueError("repo_root must be a string")
        if self.target_file:
            root = Path(self.repo_root).resolve()
            raw_target = str(self.target_file)
            target = Path(raw_target)
            is_rooted = target.is_absolute() or raw_target.startswith(("/", "\\"))
            candidate = target.resolve() if is_rooted else (root / target).resolve()
            try:
                relative = candidate.is_relative_to(root)
            except ValueError:
                relative = False
            if not relative:
                raise ValueError("target_file must be under repo_root")
        if self.module_name and not re.fullmatch(
            r"[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*", self.module_name
        ):
            raise ValueError("module_name must be a valid dotted Python identifier")


@dataclass(frozen=True, slots=True)
class GeneratedTestResult:
    """Generated test result."""

    target_file: str
    content: str


class FallbackCodingAgent:
    """Fallback coding agent."""

    def __init__(self, repo_root: str) -> None:
        self.repo_root = repo_root

    def generate_tests(self, *, source_file: str, llm: Any) -> GeneratedTestResult:
        """Generate tests for a source file.

        Args:
            source_file: Source file path.
            llm: LLM facade with a ``complete`` method.

        Returns:
            Generated test result.
        """
        content = llm.complete(source_file)
        return GeneratedTestResult(target_file=source_file.replace("\\", "/"), content=content)


@dataclass(frozen=True, slots=True)
class MultiStepCodingTask:
    """Multi-step coding task."""

    repo_root: str
    subtasks: list[dict[str, Any]] | None = None
    target_files: list[str] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.repo_root, str):
            raise ValueError("repo_root must be a string")
        if self.subtasks is not None and any(not isinstance(item, dict) for item in self.subtasks):
            raise ValueError("subtasks must be dictionaries")
        if isinstance(self.target_files, str):
            raise ValueError("target_files must be a list, not a string")


__all__ = ["CodingTask", "FallbackCodingAgent", "GeneratedTestResult", "MultiStepCodingTask"]
