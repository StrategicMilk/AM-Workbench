"""Fail-closed Workbench quality gate decisions."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class QualityGateDecision:
    """Operator-facing quality gate result."""

    passed: bool
    score: float | None
    confidence: float | None
    blockers: tuple[str, ...]

    def __repr__(self) -> str:
        return (
            "QualityGateDecision("
            f"passed={self.passed!r}, score={self.score!r}, "
            f"confidence={self.confidence!r}, blockers={len(self.blockers)})"
        )


def evaluate_quality_gate(
    *,
    score: float | None,
    confidence: float | None,
    safety_passed: bool | None,
    provenance_ref: str | None,
    scorer_available: bool,
    min_score: float = 0.75,
    min_confidence: float = 0.7,
) -> QualityGateDecision:
    """Evaluate promotion quality signals, failing closed for unknown inputs.

    Returns:
        A quality-gate decision with explicit blockers.
    """
    blockers: list[str] = []
    if not scorer_available:
        blockers.append("quality scorer unavailable")
    if score is None:
        blockers.append("quality score missing")
    elif score < min_score:
        blockers.append("quality score below threshold")
    if confidence is None:
        blockers.append("confidence missing")
    elif confidence < min_confidence:
        blockers.append("confidence below threshold")
    if safety_passed is not True:
        blockers.append("safety signal missing or failed")
    if not provenance_ref or not provenance_ref.strip():
        blockers.append("provenance missing")
    return QualityGateDecision(
        passed=not blockers,
        score=score,
        confidence=confidence,
        blockers=tuple(blockers),
    )


__all__ = ["QualityGateDecision", "evaluate_quality_gate"]
