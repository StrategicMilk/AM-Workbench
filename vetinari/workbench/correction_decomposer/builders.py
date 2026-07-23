"""Derivative builders for user-correction decomposition."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

_CorrectionDerivative: Any = None
_CorrectionKind: Any = None


def configure_builders(correction_derivative: Any, correction_kind: Any) -> None:
    """Bind runtime model constructors after runtime classes are defined.

    Args:
        correction_derivative: Correction derivative value consumed by configure_builders().
        correction_kind: Kind discriminator used to select the operation branch.
    """
    global _CorrectionDerivative, _CorrectionKind
    _CorrectionDerivative = correction_derivative
    _CorrectionKind = correction_kind


def _coerce_confidence(value: float | int | str) -> float | None:
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"correction confidence must be numeric, got {value!r}") from None
    if confidence < 0 or confidence > 1:
        raise ValueError(f"correction confidence must be between 0 and 1, got {confidence!r}")
    return confidence


def _build_feedback_signal(correction: Any) -> Any:
    return _derivative(
        correction,
        "feedback_signal",
        correction.target_ref,
        {"summary": correction.summary, "confidence": _coerce_confidence(correction.confidence)},
        consent_refs=(correction.authority.feedback_authority_ref,),
    )


def _build_corrected_output(correction: Any) -> Any:
    return _derivative(
        correction,
        "corrected_output",
        correction.original_output_ref,
        {"corrected_output": correction.corrected_output, "provenance_ref": correction.provenance_ref},
    )


def _build_preference_candidate(correction: Any) -> Any:
    return _derivative(
        correction,
        "preference_candidate",
        correction.target_ref,
        {"candidate": correction.summary, "task_shape": correction.scope.task_shape},
        consent_refs=(correction.authority.preference_approval_ref,),
    )


def _build_eval_case(correction: Any) -> Any:
    return _derivative(
        correction,
        "eval_case",
        correction.target_ref,
        {
            "input_ref": correction.original_output_ref,
            "expected_output": correction.corrected_output,
            "model_version": correction.scope.model_version,
        },
        consent_refs=(correction.safety.safety_review_ref,),
    )


def _build_failure_label(correction: Any) -> Any:
    return _derivative(
        correction,
        "failure_label",
        correction.target_ref,
        {"label": _failure_label(correction), "source_kind": _CorrectionKind(correction.kind).value},
    )


def _build_method_update_candidate(correction: Any) -> Any:
    return _derivative(
        correction,
        "method_update_candidate",
        correction.scope.affected_method_ref,
        {"change_summary": correction.summary, "source_correction_id": correction.correction_id},
    )


def _build_source_update_candidate(correction: Any) -> Any:
    return _derivative(
        correction,
        "source_update_candidate",
        correction.scope.affected_source_ref,
        {"change_summary": correction.summary, "source_correction_id": correction.correction_id},
    )


def _build_tool_card_update_candidate(correction: Any) -> Any:
    return _derivative(
        correction,
        "tool_card_update_candidate",
        correction.scope.affected_tool_card_ref,
        {"change_summary": correction.summary, "source_correction_id": correction.correction_id},
    )


def _build_annotation_task(correction: Any) -> Any:
    return _derivative(
        correction,
        "annotation_task",
        correction.target_ref,
        {"instruction": "review corrected output and label reusable failure mode"},
        consent_refs=(correction.authority.annotation_approval_ref,),
    )


def _build_training_candidate(correction: Any) -> Any:
    return _derivative(
        correction,
        "training_candidate",
        correction.target_ref,
        {
            "input_ref": correction.original_output_ref,
            "corrected_output": correction.corrected_output,
            "redaction_ref": correction.safety.redaction_ref,
            "model_version": correction.scope.model_version,
        },
        consent_refs=(correction.authority.training_approval_ref, correction.safety.redaction_ref),
    )


def _derivative(
    correction: Any,
    kind: str,
    target_ref: str,
    payload: dict[str, Any],
    *,
    consent_refs: tuple[str, ...] = (),
) -> Any:
    return _CorrectionDerivative(
        kind=kind,
        artifact_id=f"{kind}:{correction.correction_id}",
        target_ref=target_ref,
        payload={
            **payload,
            "project_id": correction.project_id,
            "persisted_state_ref": correction.persisted_state_ref,
        },
        consent_refs=tuple(ref for ref in consent_refs if ref.strip()),
        evidence_refs=correction.evidence_refs,
    )


def _is_positive_correction(correction: Any) -> bool:
    kind = _CorrectionKind(correction.kind)
    if kind is _CorrectionKind.APPROVAL:
        return True
    if kind is _CorrectionKind.RUBRIC_SCORE and correction.rubric_score is not None:
        return float(correction.rubric_score) >= 0.8
    return False


def _failure_label(correction: Any) -> str:
    if _CorrectionKind(correction.kind) is _CorrectionKind.REJECTION:
        return "user_rejected_output"
    if _CorrectionKind(correction.kind) is _CorrectionKind.RUBRIC_SCORE:
        return "low_rubric_score"
    return "user_corrected_output"
