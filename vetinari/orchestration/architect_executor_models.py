"""Data models for the architect-executor pipeline."""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from vetinari.config.inference_config import get_inference_config
from vetinari.config.model_config import get_task_default_model
from vetinari.utils.serialization import dataclass_to_dict

logger = logging.getLogger(__name__)

DEVELOPER_WORKFLOW_CONTRACT_ID = "wave3-rcg0014p06-scope-followup-P09"
ARCHITECT_EXECUTOR_MODEL_WORKFLOW_GUARDS: tuple[str, ...] = (
    "UTC timestamps normalize naive datetimes to UTC",
    "pipeline model defaults fall back only after config failure",
    "pipeline config validation bounds step counts and temperatures",
    "architect plans reject missing goals, steps, and invalid dependencies",
)


def developer_workflow_contract() -> dict[str, object]:
    """Return architect-executor model workflow guarantees verified by this follow-up pack."""
    return {
        "pack": DEVELOPER_WORKFLOW_CONTRACT_ID,
        "surface": "vetinari/orchestration/architect_executor_models.py",
        "guards": ARCHITECT_EXECUTOR_MODEL_WORKFLOW_GUARDS,
    }


def utc_timestamp(now: datetime | None = None) -> str:
    """Return a testable UTC ISO-8601 timestamp for architect plans.

    Args:
        now: Optional timestamp to normalize; naive values are treated as UTC.

    Returns:
        UTC ISO-8601 timestamp with second precision and a trailing ``Z``.
    """
    value = now or datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _default_pipeline_model(task_type: str, fallback: str) -> str:
    """Resolve a default model from runtime config, falling back only on config failure."""
    try:
        model_id = get_task_default_model(task_type)
        if model_id:
            return model_id
    except Exception:
        logger.warning("Default model resolution failed for %s; using fallback %s", task_type, fallback)
    return fallback


@dataclass(frozen=True, slots=True)
class PipelineConfig:
    """Configuration for the architect-executor pipeline."""

    enabled: bool = True
    architect_model: str = field(default_factory=lambda: _default_pipeline_model("architect", "qwen2.5-coder-32b"))
    executor_model: str = field(default_factory=lambda: _default_pipeline_model("executor", "qwen2.5-coder-7b"))
    auto_commit: bool = False
    commit_style: str = "conventional"
    max_steps: int = 20
    fallback_to_single: bool = True
    architect_temperature: float = 0.4
    executor_temperature: float = 0.2
    architect_max_tokens: int = 4096
    executor_max_tokens: int = 2048

    def __post_init__(self) -> None:
        """Resolve default temperatures from inference profiles when available."""
        try:
            config = get_inference_config()
            profiles = set(config.list_profiles())
            if "architect" in profiles:
                object.__setattr__(
                    self,
                    "architect_temperature",
                    config.get_effective_params("architect", self.architect_model).get("temperature", 0.4),
                )
            else:
                logger.warning("Inference profile 'architect' missing; using fallback temperature 0.4")
            if "executor" in profiles:
                object.__setattr__(
                    self,
                    "executor_temperature",
                    config.get_effective_params("executor", self.executor_model).get("temperature", 0.2),
                )
            else:
                logger.warning("Inference profile 'executor' missing; using fallback temperature 0.2")
        except Exception:
            logger.warning("InferenceConfigManager unavailable; using architect/executor fallback temperatures")

    def __repr__(self) -> str:
        """Show key identifying fields for debugging."""
        return (
            f"PipelineConfig(architect_model={self.architect_model!r},"
            f" executor_model={self.executor_model!r},"
            f" max_steps={self.max_steps!r})"
        )

    def to_dict(self) -> dict[str, Any]:
        """Converts pipeline configuration to a JSON-serializable dictionary."""
        return dataclass_to_dict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PipelineConfig:
        """Create from dictionary, ignoring unknown keys.

        Returns:
            PipelineConfig populated from known keys in ``data``.
        """
        known = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in data.items() if k in known}
        return cls(**filtered)

    def validate(self) -> list[str]:
        """Validate configuration and return list of errors.

        Returns:
            Human-readable validation errors, or an empty list when valid.
        """
        errors: list[str] = []
        if self.max_steps < 1:
            errors.append("max_steps must be >= 1")
        if self.max_steps > 100:
            errors.append("max_steps must be <= 100")
        if self.architect_temperature < 0 or self.architect_temperature > 2:
            errors.append("architect_temperature must be between 0 and 2")
        if self.executor_temperature < 0 or self.executor_temperature > 2:
            errors.append("executor_temperature must be between 0 and 2")
        if self.commit_style not in ("conventional", "descriptive"):
            errors.append("commit_style must be 'conventional' or 'descriptive'")
        if self.architect_max_tokens < 1:
            errors.append("architect_max_tokens must be >= 1")
        if self.executor_max_tokens < 1:
            errors.append("executor_max_tokens must be >= 1")
        return errors


@dataclass
class ArchitectPlan:
    """Plan created by the architect model."""

    plan_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    goal: str = ""
    steps: list[dict[str, Any]] = field(default_factory=list)
    dependencies: dict[str, list[str]] = field(default_factory=dict)
    estimated_tokens: int = 0
    architect_model: str = ""
    created_at: str = field(default_factory=utc_timestamp)

    def __repr__(self) -> str:
        """Show key identifying fields for debugging."""
        return (
            f"ArchitectPlan(plan_id={self.plan_id!r},"
            f" architect_model={self.architect_model!r},"
            f" steps={len(self.steps)!r})"
        )

    def to_dict(self) -> dict[str, Any]:
        """Converts the architect plan to a JSON-serializable dictionary."""
        return dataclass_to_dict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ArchitectPlan:
        """Deserialize from dictionary.

        Returns:
            ArchitectPlan populated from known keys in ``data``.
        """
        known = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in data.items() if k in known}
        return cls(**filtered)

    def get_step(self, step_id: str) -> dict[str, Any] | None:
        """Get a step by its ID.

        Returns:
            Matching step dictionary, or None when no step has ``step_id``.
        """
        for step in self.steps:
            if step.get("id") == step_id:
                return step
        return None

    def get_ready_steps(self, completed_ids: set) -> list[dict[str, Any]]:
        """Get steps whose dependencies have all been completed.

        Returns:
            Steps not completed yet whose dependencies are all complete.
        """
        ready = []
        for step in self.steps:
            sid = step.get("id", "")
            if sid in completed_ids:
                continue
            deps = self.dependencies.get(sid, [])
            if all(d in completed_ids for d in deps):
                ready.append(step)
        return ready

    def step_count(self) -> int:
        """Return number of steps in the plan."""
        return len(self.steps)

    def validate(self) -> list[str]:
        """Validate the plan and return list of errors.

        Returns:
            Human-readable validation errors, or an empty list when valid.
        """
        errors: list[str] = []
        if not self.goal:
            errors.append("Plan must have a goal")
        if not self.steps:
            errors.append("Plan must have at least one step")

        step_ids = {s.get("id") for s in self.steps}
        for sid, deps in self.dependencies.items():
            if sid not in step_ids:
                errors.append(f"Dependency key '{sid}' is not a valid step ID")
            for dep in deps:
                if dep not in step_ids:
                    errors.append(f"Dependency '{dep}' for step '{sid}' is not a valid step ID")
                if dep == sid:
                    errors.append(f"Step '{sid}' cannot depend on itself")
        return errors
