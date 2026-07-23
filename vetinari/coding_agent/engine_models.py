"""Shared models and public task helpers for the coding agent engine."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from vetinari.agents.contracts import AgentTask
from vetinari.types import AgentType, CodingTaskType, StatusEnum
from vetinari.utils.serialization import dataclass_to_dict


class CodingArtifactType(str, Enum):
    """Types of code artifacts."""

    PATCH = "patch"
    FILE_CONTENTS = "file_contents"
    BUILD_ARTIFACT = "build_artifact"
    TEST_ARTIFACT = "test_artifact"


def _normalize_coding_task_type(value: str | CodingTaskType) -> CodingTaskType:
    if isinstance(value, CodingTaskType):
        return value
    if isinstance(value, str):
        try:
            return CodingTaskType(value)
        except ValueError as exc:
            raise ValueError(f"unknown coding task type: {value!r}") from exc
    raise TypeError(f"coding task type must be a string or CodingTaskType, got {type(value).__name__}")


def _require_text_list(value: Any, *, field_name: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise TypeError(f"{field_name} must be a list or tuple of strings")
    result: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"{field_name}[{index}] must be a non-empty string")
        result.append(item)
    return result


def _normalize_constraints(value: Any) -> str | list[str]:
    if isinstance(value, str):
        return value
    return _require_text_list(value, field_name="constraints")


def _module_name_from_target(target: str) -> str:
    """Convert a generated-code target path into an importable module name."""
    if not isinstance(target, str) or not target.strip():
        raise ValueError("target must be a non-empty string")
    target_path = Path(target)
    if target_path.is_absolute() or ".." in target_path.parts or any(part.endswith(":") for part in target_path.parts):
        raise ValueError(f"target must be a repo-relative module path, got {target!r}")
    if target_path.name == "__init__.py":
        module_path = Path(*target_path.parts[:-1]) if len(target_path.parts) > 1 else Path(target_path.parent.name)
    elif target_path.suffix == ".py":
        module_path = target_path.with_suffix("")
    else:
        module_path = target_path

    module_parts = [
        part.replace("-", "_") for part in module_path.parts if part not in {"", ".", ".."} and not part.endswith(":")
    ]
    return ".".join(module_parts) if module_parts else "module"


@dataclass
class _CodeTask:
    """Internal coding task representation.

    External callers should use ``make_code_agent_task()`` which returns an
    ``AgentTask``. The engine converts it internally via ``_from_agent_task()``.
    """

    task_id: str = field(default_factory=lambda: f"code_{uuid.uuid4().hex[:8]}")
    plan_id: str = ""
    subtask_id: str = ""
    type: CodingTaskType = CodingTaskType.SCAFFOLD
    language: str = "python"
    framework: str = ""
    repo_path: str = ""
    target_files: list[str] = field(default_factory=list)
    constraints: str | list[str] = ""
    description: str = ""
    status: StatusEnum = StatusEnum.PENDING
    rationale: str = ""
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def __repr__(self) -> str:
        return f"_CodeTask(task_id={self.task_id!r}, type={self.type!r}, status={self.status!r})"

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-compatible dictionary."""
        return dataclass_to_dict(self)

    @classmethod
    def from_agent_task(cls, task: AgentTask) -> _CodeTask:
        """Convert an AgentTask with coding context into the internal format.

        Args:
            task: An AgentTask created by ``make_code_agent_task()``.

        Returns:
            Internal _CodeTask for engine processing.
        """
        ctx = task.context
        task_type_raw = ctx.get("task_type", CodingTaskType.SCAFFOLD.value)
        task_type = _normalize_coding_task_type(task_type_raw)
        constraints = _normalize_constraints(ctx.get("constraints", ""))
        target_files = _require_text_list(ctx.get("target_files", []), field_name="target_files")
        return cls(
            task_id=task.task_id,
            plan_id=ctx.get("plan_id", ""),
            subtask_id=ctx.get("subtask_id", ""),
            type=task_type,
            language=ctx.get("language", "python"),
            framework=ctx.get("framework", ""),
            repo_path=ctx.get("repo_path", ""),
            target_files=target_files,
            constraints=constraints,
            description=task.description,
        )


def make_code_agent_task(
    description: str,
    *,
    task_type: CodingTaskType = CodingTaskType.SCAFFOLD,
    language: str = "python",
    framework: str = "",
    repo_path: str = "",
    target_files: list[str] | None = None,
    constraints: str | list[str] = "",
    plan_id: str = "",
    subtask_id: str = "",
) -> AgentTask:
    """Create an AgentTask carrying coding-specific context fields.

    This is the public API for creating coding tasks. The engine converts
    the AgentTask to its internal format automatically.

    Args:
        description: Human-readable task description.
        task_type: Type of coding task (scaffold, implement, test, etc.).
        language: Programming language for the task.
        framework: Target framework (e.g. "fastapi", "pytest").
        repo_path: Repository root path for file resolution.
        target_files: Files to generate or modify.
        constraints: Coding constraints (style, perf, etc.).
        plan_id: Parent plan ID if part of a plan.
        subtask_id: Parent subtask ID if part of a subtask.

    Returns:
        An AgentTask with coding metadata in context.
    """
    normalized_task_type = _normalize_coding_task_type(task_type)
    normalized_targets = _require_text_list(target_files, field_name="target_files")
    normalized_constraints = _normalize_constraints(constraints)
    return AgentTask(
        task_id=f"code_{uuid.uuid4().hex[:8]}",
        agent_type=AgentType.WORKER,
        description=description,
        prompt=description,
        context={
            "task_type": normalized_task_type.value,
            "language": language,
            "framework": framework,
            "repo_path": repo_path,
            "target_files": normalized_targets,
            "constraints": normalized_constraints,
            "plan_id": plan_id,
            "subtask_id": subtask_id,
        },
    )


@dataclass(frozen=True, slots=True)
class CodeArtifact:
    """A code artifact generated by the coding agent."""

    artifact_id: str = field(default_factory=lambda: f"art_{uuid.uuid4().hex[:8]}")
    task_id: str = ""
    type: CodingArtifactType = CodingArtifactType.FILE_CONTENTS
    path: str = ""
    content: str = ""
    diff: str = ""
    provenance: str = ""
    language: str = "python"
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def __repr__(self) -> str:
        return f"CodeArtifact(artifact_id={self.artifact_id!r}, type={self.type!r}, path={self.path!r})"

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-compatible dictionary."""
        return dataclass_to_dict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CodeArtifact:
        """Deserialize a CodeArtifact from a plain dictionary.

        Converts the ``type`` string value back to the CodingArtifactType enum
        before constructing the instance.

        Args:
            data: Dictionary of field values, typically from ``to_dict()`` or JSON storage.

        Returns:
            A new CodeArtifact instance populated from the provided dictionary.
        """
        data = dict(data)
        if "type" in data and isinstance(data["type"], str):
            data["type"] = CodingArtifactType(data["type"])
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


__all__ = [
    "CodeArtifact",
    "CodingArtifactType",
    "CodingTaskType",
    "StatusEnum",
    "_CodeTask",
    "_module_name_from_target",
    "make_code_agent_task",
]
