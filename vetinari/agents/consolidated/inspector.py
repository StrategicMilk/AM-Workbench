"""Kind-aware Inspector grading for shard execution evidence."""

from __future__ import annotations

from typing import Any

from vetinari.agents.contracts import OutcomeSignal, ToolEvidence
from vetinari.agents.inspector_rubrics import load_rubric
from vetinari.types import EvidenceBasis, ShardKind


def grade_shard(shard: dict[str, Any], kind: str | ShardKind = ShardKind.STANDARD) -> OutcomeSignal:
    """Grade a shard dictionary against the rubric selected by kind.

    Args:
        shard: Evidence dictionary produced by a shard execution.
        kind: Shard kind whose rubric should grade the evidence.

    Returns:
        OutcomeSignal with pass/fail, score, issues, and normalized kind.

    Raises:
        ValueError: If kind is not one of the supported ShardKind values.
    """
    # Decision: per-kind grading rubrics (ADR-0117).
    shard_kind = ShardKind(kind)
    rubric = load_rubric(shard_kind)
    required = [criterion for criterion in rubric["grading_criteria"] if criterion.get("required", True)]
    if not required:
        # Q-L2 defensive guard: a rubric with zero required criteria would
        # vacuously score 1.0 for every input. That is governance theater.
        raise ValueError(
            f"Rubric for ShardKind.{shard_kind.name} has zero required criteria - "
            f"cannot grade. Fix the rubric YAML to mark at least one criterion required."
        )
    passed_ids: list[str] = []
    failed_ids: list[str] = []
    issues: list[str] = []

    for criterion in required:
        if _criterion_passes(shard, criterion):
            passed_ids.append(str(criterion.get("id", "")))
            continue
        failed_id = str(criterion.get("id", "unknown"))
        failed_ids.append(failed_id)
        issues.append(str(criterion.get("rejection_reason") or failed_id))

    score = len(passed_ids) / len(required)
    suggestions = tuple(f"failed_check_id:{item}" for item in failed_ids)
    tool_evidence = ToolEvidence(
        tool_name="inspector_rubric",
        command=f"grade_shard kind={shard_kind.value}",
        exit_code=0 if not failed_ids else 1,
        stdout_snippet=f"passed={len(passed_ids)} failed={len(failed_ids)}",
        passed=not failed_ids,
    )
    return OutcomeSignal(
        passed=not failed_ids,
        score=score,
        basis=EvidenceBasis.TOOL_EVIDENCE,
        tool_evidence=(tool_evidence,),
        issues=tuple(issues),
        suggestions=suggestions,
        kind=shard_kind,
    )


def _criterion_passes(shard: dict[str, Any], criterion: dict[str, Any]) -> bool:
    key = criterion.get("evidence_key")
    if isinstance(key, str):
        return _value_satisfies(shard.get(key), criterion)
    keys = criterion.get("evidence_key_any_of")
    if isinstance(keys, list):
        return any(_value_satisfies(shard.get(str(candidate)), criterion) for candidate in keys)
    return False


def _value_satisfies(value: Any, criterion: dict[str, Any]) -> bool:
    evidence_type = criterion.get("evidence_type")
    allowed_values = criterion.get("allowed_values")
    if evidence_type in {"artifact_path", "artifact_content"}:
        return isinstance(value, str) and bool(value.strip())
    if evidence_type in {"criterion_field", "hypothesis_field"}:
        if isinstance(allowed_values, list):
            return value in allowed_values
        return _has_value(value)
    if evidence_type == "test_set":
        return _has_value(value)
    return _has_value(value)


def _has_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, list | tuple | set):
        return bool(value)
    return True


__all__ = ["grade_shard"]
