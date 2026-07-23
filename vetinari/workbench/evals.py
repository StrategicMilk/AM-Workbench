"""Typed eval-result records consumed by the metadata spine."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from vetinari.utils.serialization import dataclass_to_dict
from vetinari.workbench.eval_integrity import validate_normalized_eval_score


def _require_non_empty(value: str, field_name: str) -> None:
    if not value or not value.strip():
        raise ValueError(f"{field_name} must be non-empty")


class EvalKind(str, Enum):
    """Kinds of evaluation records tracked by the workbench."""

    OFFLINE_SUITE = "offline_suite"
    RED_TEAM = "red_team"
    LIVE_TRACE_DERIVED = "live_trace_derived"
    HUMAN_ANNOTATION = "human_annotation"
    JUDGE_ONLY = "judge_only"


@dataclass(frozen=True, slots=True)
class EvalScore:
    """One scored metric inside an EvalResult."""

    metric_name: str
    value: float
    threshold: float
    passed: bool
    unit: str = ""

    def __post_init__(self) -> None:
        _require_non_empty(self.metric_name, "metric_name")
        validate_normalized_eval_score(self.value, field_name="value")
        validate_normalized_eval_score(self.threshold, field_name="threshold")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"EvalScore(metric_name={self.metric_name!r}, value={self.value!r}, threshold={self.threshold!r})"

    def to_dict(self) -> dict[str, Any]:
        """Return the operator-console JSON contract for this score."""
        return dataclass_to_dict(self)


@dataclass(frozen=True, slots=True)
class WorkbenchEvalResult:
    """One eval suite by asset revision by run result."""

    eval_id: str
    kind: EvalKind
    run_id: str
    asset_id: str
    asset_revision: str
    scores: tuple[EvalScore, ...]
    captured_at_utc: str
    notes: str = ""

    def __post_init__(self) -> None:
        _require_non_empty(self.eval_id, "eval_id")
        _require_non_empty(self.run_id, "run_id")
        _require_non_empty(self.asset_id, "asset_id")
        _require_non_empty(self.asset_revision, "asset_revision")
        _require_non_empty(self.captured_at_utc, "captured_at_utc")
        if not self.scores:
            raise ValueError("scores must be non-empty")
        for score in self.scores:
            if not isinstance(score, EvalScore):
                raise ValueError("scores must contain EvalScore instances")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"EvalResult(eval_id={self.eval_id!r}, kind={self.kind!r}, run_id={self.run_id!r})"

    def to_dict(self) -> dict[str, Any]:
        """Return the operator-console JSON contract for this eval result."""
        return {
            "eval_id": self.eval_id,
            "kind": self.kind.value,
            "run_id": self.run_id,
            "asset_id": self.asset_id,
            "asset_revision": self.asset_revision,
            "scores": [score.to_dict() for score in self.scores],
            "captured_at_utc": self.captured_at_utc,
            "notes": self.notes,
        }


EvalResult = WorkbenchEvalResult


__all__ = ["EvalKind", "EvalResult", "EvalScore", "WorkbenchEvalResult"]
