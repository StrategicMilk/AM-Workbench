"""JSON conversion helpers for Workbench experiment records."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import asdict
from typing import Any

_T: dict[str, Any] = {}
_validate_project_id: Callable[[str], str] | None = None
_coerce_decision: Callable[[str], Any] | None = None


def configure_serialization(
    types: dict[str, Any], validate_project_id: Callable[[str], str], coerce_decision: Callable[[str], Any]
) -> None:
    """Bind ExperimentLab model constructors after runtime classes are defined.

    Args:
        types: Types value consumed by configure_serialization().
        validate_project_id: Project identifier that scopes the operation.
        coerce_decision: Coerce decision value consumed by configure_serialization().
    """
    _T.update(types)
    globals()["_validate_project_id"] = validate_project_id
    globals()["_coerce_decision"] = coerce_decision


def _record_to_json(record: Any) -> str:
    data = asdict(record)
    data["decision"] = record.decision.value
    return json.dumps(data, sort_keys=True, separators=(",", ":"))


def _artifact_from_json(value: Mapping[str, Any]) -> Any:
    return _T["ExperimentArtifactRef"](
        artifact_id=str(value["artifact_id"]),
        artifact_kind=str(value["artifact_kind"]),
        label=str(value.get("label", "")),
    )


def _sample_from_json(value: Mapping[str, Any]) -> Any:
    return _T["ExperimentSampleRef"](
        sample_id=str(value["sample_id"]),
        sample_kind=str(value["sample_kind"]),
        source=str(value.get("source", "")),
    )


def _metric_from_json(value: Mapping[str, Any]) -> Any:
    return _T["MetricObservation"](
        name=str(value["name"]),
        baseline_value=float(value["baseline_value"]),
        candidate_value=float(value["candidate_value"]),
        unit=str(value.get("unit", "")),
        higher_is_better=bool(value.get("higher_is_better", True)),
    )


def _review_from_json(value: Mapping[str, Any]) -> Any:
    return _T["ReviewNote"](reviewer=str(value.get("reviewer", "")), summary=str(value["summary"]))


def _record_from_json(data: Mapping[str, Any]) -> Any:
    return _T["ExperimentRecord"](
        experiment_id=str(data["experiment_id"]),
        project_id=_validate_project_id(str(data["project_id"])),
        hypothesis=str(data["hypothesis"]),
        baseline=_artifact_from_json(data["baseline"]),
        candidate=_artifact_from_json(data["candidate"]),
        sample_ref=_sample_from_json(data["sample_ref"]),
        metrics=tuple(_metric_from_json(row) for row in data["metrics"]),
        latency_ms=float(data["latency_ms"]),
        cost_usd=float(data["cost_usd"]),
        human_review=_review_from_json(data["human_review"]),
        decision=_coerce_decision(str(data["decision"])),
        rationale=str(data["rationale"]),
        created_at_utc=str(data["created_at_utc"]),
    )
