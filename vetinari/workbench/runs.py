"""Typed workbench run records consumed by the metadata spine."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from vetinari.agents.contracts import OutcomeSignal
from vetinari.types import AgentType, ShardKind


def _require_non_empty(value: str, field_name: str) -> None:
    if not value or not value.strip():
        raise ValueError(f"{field_name} must be non-empty")


class RunKind(str, Enum):
    """Kinds of execution captured in the workbench spine."""

    AGENT_RUN = "agent_run"
    TRAINING_RUN = "training_run"
    EVAL_RUN = "eval_run"
    GATEWAY_REQUEST = "gateway_request"
    PLAYGROUND_RUN = "playground_run"


class RunStatus(str, Enum):
    """Lifecycle states for a workbench run."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class RunMetric:
    """One numeric metric attached to a run."""

    name: str
    value: float
    unit: str = ""

    def __post_init__(self) -> None:
        _require_non_empty(self.name, "name")

    def to_dict(self) -> dict[str, Any]:
        """Return the compact metric representation used by APIs."""
        return {"name": self.name, "value": self.value, "unit": self.unit}


@dataclass(frozen=True, slots=True)
class WorkbenchRun:
    """One training, eval, agent, gateway, or playground run."""

    run_id: str
    kind: RunKind
    status: RunStatus
    started_at_utc: str
    finished_at_utc: str
    actor_agent_type: AgentType
    asset_revisions: tuple[tuple[str, str], ...]
    lease_id: str
    shard_kind: ShardKind | None
    metrics: tuple[RunMetric, ...] = ()
    outcome: OutcomeSignal | None = None
    project_id: str = "default"

    def __post_init__(self) -> None:
        _require_non_empty(self.run_id, "run_id")
        _require_non_empty(self.started_at_utc, "started_at_utc")
        _require_non_empty(self.project_id, "project_id")
        object.__setattr__(self, "kind", _coerce_enum(RunKind, self.kind))
        object.__setattr__(self, "status", _coerce_enum(RunStatus, self.status))
        terminal = {
            RunStatus.SUCCEEDED,
            RunStatus.FAILED,
            RunStatus.BLOCKED,
            RunStatus.CANCELLED,
        }
        if self.status in terminal and not self.finished_at_utc.strip():
            raise ValueError("terminal run status requires finished_at_utc")
        if self.status not in terminal and self.finished_at_utc:
            raise ValueError("non-terminal run status requires empty finished_at_utc")
        for pair in self.asset_revisions:
            if len(pair) != 2 or not pair[0].strip() or not pair[1].strip():
                raise ValueError("asset_revisions entries must be non-empty (asset_id, revision) pairs")
        for metric in self.metrics:
            if not isinstance(metric, RunMetric):
                raise ValueError("metrics must contain RunMetric instances")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"WorkbenchRun(run_id={self.run_id!r}, kind={self.kind!r}, status={self.status!r})"

    def to_dict(self) -> dict[str, Any]:
        """Return the operator-console JSON contract for this run."""
        return {
            "run_id": self.run_id,
            "kind": self.kind.value,
            "status": self.status.value,
            "started_at_utc": self.started_at_utc,
            "finished_at_utc": self.finished_at_utc,
            "actor_agent_type": self.actor_agent_type.value,
            "asset_revisions": list(self.asset_revisions),
            "lease_id": self.lease_id,
            "shard_kind": self.shard_kind.value if self.shard_kind is not None else None,
            "outcome": self._outcome_to_dict(),
            "metrics": [metric.to_dict() for metric in self.metrics],
            "project_id": self.project_id,
        }

    def _outcome_to_dict(self) -> dict[str, Any] | None:
        if self.outcome is None:
            return None
        return {
            "passed": bool(self.outcome.passed),
            "score": float(self.outcome.score),
            "basis": _enum_value(self.outcome.basis),
            "issues": list(self.outcome.issues),
            "suggestions": list(self.outcome.suggestions),
            "use_case": self.outcome.use_case,
            "kind": _enum_value(self.outcome.kind),
        }


__all__ = ["RunKind", "RunMetric", "RunStatus", "WorkbenchRun"]


def _coerce_enum(enum_type: type[Enum], value: Enum | str) -> Enum:
    raw_value = value.value if isinstance(value, Enum) else value
    return value if isinstance(value, enum_type) else enum_type(raw_value)


def _enum_value(value: Any) -> str:
    return str(getattr(value, "value", value))
