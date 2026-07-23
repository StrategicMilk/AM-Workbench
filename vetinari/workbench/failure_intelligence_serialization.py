"""Serialization helpers for failure-intelligence autopsies."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

_T: dict[str, Any] = {}


def configure_serialization(types: dict[str, Any]) -> None:
    """Bind failure-intelligence model constructors after runtime classes are defined."""
    _T.update(types)


def _safe_tuple(values: Any) -> tuple[str, ...]:
    if values is None:
        return ()
    if isinstance(values, str):
        return (values,) if values.strip() else ()
    return tuple(str(value) for value in values if str(value).strip())


def _context_from_mapping(data: dict[str, Any]) -> Any:
    return _T["FailedRunContext"](
        project_id=str(data.get("project_id", "default")),
        run_id=str(data.get("run_id", "")),
        status=str(data.get("status", "failed")),
        task_profile=data.get("task_profile"),
        prompt=str(data.get("prompt", "")),
        output_summary=str(data.get("output_summary", "")),
        error_message=str(data.get("error_message", "")),
        method_kind=data.get("method_kind"),
        method_card_id=data.get("method_card_id"),
        method_promotion_status=data.get("method_promotion_status"),
        source_freshness=data.get("source_freshness"),
        stale_source_ids=_safe_tuple(data.get("stale_source_ids")),
        tool_card_ids=_safe_tuple(data.get("tool_card_ids")),
        unavailable_tool_names=_safe_tuple(data.get("unavailable_tool_names")),
        hallucinated_tool_names=_safe_tuple(data.get("hallucinated_tool_names")),
        policy_rejection=data.get("policy_rejection"),
        runtime_unavailable=bool(data.get("runtime_unavailable")),
        missing_capability=data.get("missing_capability"),
        eval_count=data.get("eval_count"),
        eval_failures=_safe_tuple(data.get("eval_failures")),
        dataset_id=data.get("dataset_id"),
        dataset_revision=data.get("dataset_revision"),
        expected_dataset_revision=data.get("expected_dataset_revision"),
        user_request=str(data.get("user_request", "")),
        ambiguity_markers=_safe_tuple(data.get("ambiguity_markers")),
        evidence_refs=_safe_tuple(data.get("evidence_refs")),
    )


def _candidate_to_dict(candidate: Any) -> dict[str, Any]:
    data = asdict(candidate)
    data["failure_kind"] = candidate.failure_kind.value
    return data


def _followup_to_dict(followup: Any) -> dict[str, Any]:
    data = asdict(followup)
    data["kind"] = followup.kind.value
    data["source_failure_kind"] = followup.source_failure_kind.value
    return data


def _result_to_dict(result: Any) -> dict[str, Any]:
    data = asdict(result)
    data["candidates"] = [_candidate_to_dict(candidate) for candidate in result.candidates]
    data["followup"] = _followup_to_dict(result.followup)
    return data


def _candidate_from_dict(data: dict[str, Any]) -> Any:
    return _T["FailureCandidate"](
        failure_kind=_T["FailureKind"](str(data["failure_kind"])),
        confidence=float(data["confidence"]),
        reason=str(data["reason"]),
        evidence_refs=_safe_tuple(data.get("evidence_refs")),
    )


def _followup_from_dict(data: dict[str, Any]) -> Any:
    return _T["FollowupArtifact"](
        kind=_T["FollowupKind"](str(data["kind"])),
        title=str(data["title"]),
        description=str(data["description"]),
        source_failure_kind=_T["FailureKind"](str(data["source_failure_kind"])),
    )


def _result_from_dict(data: dict[str, Any]) -> Any:
    return _T["AutopsyResult"](
        autopsy_id=str(data["autopsy_id"]),
        project_id=str(data["project_id"]),
        run_id=str(data["run_id"]),
        status=str(data["status"]),
        degraded=bool(data["degraded"]),
        degraded_reason=data.get("degraded_reason"),
        candidates=tuple(_candidate_from_dict(row) for row in data.get("candidates", [])),
        followup=_followup_from_dict(data["followup"]),
        evidence_refs=_safe_tuple(data.get("evidence_refs")),
        created_at_utc=str(data["created_at_utc"]),
    )
