"""Automated evaluation and ELO arena helpers for Workbench experiments."""

from __future__ import annotations

import logging
import math
import re
from dataclasses import asdict, dataclass
from typing import Any, Literal

logger = logging.getLogger(__name__)

OutcomeLabel = Literal["a", "b", "tie"]


@dataclass(frozen=True, slots=True)
class AutomatedEvalCase:
    """One deterministic evaluation case for an automated comparison run."""

    case_id: str
    prompt: str
    expected: str
    baseline_output: str
    candidate_output: str

    def __post_init__(self) -> None:
        for field_name in ("case_id", "prompt", "expected"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} is required")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"AutomatedEvalCase(case_id={self.case_id!r})"


@dataclass(frozen=True, slots=True)
class AutomatedEvalSummary:
    """Aggregate scores from an automated comparison run."""

    case_count: int
    baseline_score: float
    candidate_score: float
    winner: OutcomeLabel
    cases: tuple[dict[str, Any], ...]

    def to_dict(self) -> dict[str, Any]:
        """Serialize this summary for API responses."""
        return asdict(self)

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return (
            "AutomatedEvalSummary("
            f"case_count={self.case_count!r}, winner={self.winner!r}, "
            f"candidate_score={self.candidate_score!r})"
        )


@dataclass(frozen=True, slots=True)
class ArenaCompetitor:
    """One blind arena competitor and its current ELO rating."""

    model_id: str
    output: str
    rating: float = 1000.0

    def __post_init__(self) -> None:
        if not isinstance(self.model_id, str) or not self.model_id.strip():
            raise ValueError("model_id is required")
        if not isinstance(self.output, str) or not self.output.strip():
            raise ValueError("output is required")
        if not math.isfinite(float(self.rating)) or float(self.rating) <= 0:
            raise ValueError("rating must be positive and finite")


@dataclass(frozen=True, slots=True)
class ArenaMatchResult:
    """ELO result for one blind comparison match."""

    blind_labels: tuple[str, str]
    winner: OutcomeLabel
    model_a_rating: float
    model_b_rating: float
    rating_delta_a: float
    rating_delta_b: float

    def to_dict(self) -> dict[str, Any]:
        """Serialize this result for API responses."""
        return asdict(self)

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return (
            "ArenaMatchResult("
            f"winner={self.winner!r}, model_a_rating={self.model_a_rating!r}, "
            f"model_b_rating={self.model_b_rating!r})"
        )


def run_automated_eval(cases: list[AutomatedEvalCase]) -> AutomatedEvalSummary:
    """Score baseline and candidate outputs against deterministic expected text.

    Args:
        cases: Evaluation cases with baseline and candidate outputs.

    Returns:
        Aggregate score and per-case records.

    Raises:
        ValueError: If no cases are provided.
    """
    if not cases:
        raise ValueError("at least one evaluation case is required")

    rows: list[dict[str, Any]] = []
    baseline_total = 0.0
    candidate_total = 0.0
    for case in cases:
        expected = case.expected.strip().casefold()
        baseline_score = _contains_score(case.baseline_output, expected)
        candidate_score = _contains_score(case.candidate_output, expected)
        baseline_total += baseline_score
        candidate_total += candidate_score
        # Record a paired shadow comparison so the shadow testing subsystem can
        # track token-F1 quality deltas for automated eval cases.  The test_id
        # is derived from the case_id so multiple eval runs for the same case
        # accumulate observations on the same shadow test slot.
        try:
            from vetinari.learning.shadow_testing import record_shadow_comparison

            record_shadow_comparison(
                f"eval_{case.case_id}",
                expected_output=case.expected,
                actual_output=case.candidate_output,
            )
        except Exception:
            logger.warning(
                "Shadow comparison recording unavailable for eval case %s -- skipping",
                case.case_id,
            )
        rows.append({
            "case_id": case.case_id,
            "baseline_score": baseline_score,
            "candidate_score": candidate_score,
            "winner": _winner(candidate_score, baseline_score, candidate_label="b", baseline_label="a"),
        })

    case_count = len(cases)
    baseline_average = baseline_total / case_count
    candidate_average = candidate_total / case_count
    return AutomatedEvalSummary(
        case_count=case_count,
        baseline_score=baseline_average,
        candidate_score=candidate_average,
        winner=_winner(candidate_average, baseline_average, candidate_label="b", baseline_label="a"),
        cases=tuple(rows),
    )


def run_arena_match(
    competitor_a: ArenaCompetitor,
    competitor_b: ArenaCompetitor,
    *,
    winner: OutcomeLabel,
    k_factor: float = 32.0,
) -> ArenaMatchResult:
    """Apply an ELO update to one blind model comparison.

    Args:
        competitor_a: First competitor, presented to judges as blind label A.
        competitor_b: Second competitor, presented to judges as blind label B.
        winner: Blind winner label, or ``tie``.
        k_factor: ELO update factor.

    Returns:
        Updated ratings and deltas.

    Raises:
        ValueError: If the winner label or k-factor is invalid.
    """
    if winner not in {"a", "b", "tie"}:
        raise ValueError("winner must be one of: a, b, tie")
    if not math.isfinite(float(k_factor)) or float(k_factor) <= 0:
        raise ValueError("k_factor must be positive and finite")

    score_a = 0.5 if winner == "tie" else (1.0 if winner == "a" else 0.0)
    score_b = 1.0 - score_a
    expected_a = _expected_score(float(competitor_a.rating), float(competitor_b.rating))
    expected_b = 1.0 - expected_a
    delta_a = float(k_factor) * (score_a - expected_a)
    delta_b = float(k_factor) * (score_b - expected_b)
    return ArenaMatchResult(
        blind_labels=("A", "B"),
        winner=winner,
        model_a_rating=float(competitor_a.rating) + delta_a,
        model_b_rating=float(competitor_b.rating) + delta_b,
        rating_delta_a=delta_a,
        rating_delta_b=delta_b,
    )


def _contains_score(output: str, expected_casefold: str) -> float:
    """Score output quality against expected text.

    Replaces the prior length-based correctness heuristic with a tiered
    scoring strategy:

    - Empty output is always ``0.0`` (no signal to score).
    - When ``expected_casefold`` is non-empty, use token F1 overlap between
      the lowercased whitespace-split tokens of expected and actual. This
      treats partial matches proportionally instead of as a hard 1.0 / 0.0.
    - When ``expected_casefold`` is empty (no reference answer available),
      apply a weak structural heuristic: outputs that end in a sentence
      terminator score ``0.5``, anything else scores ``0.25``. This is still
      weak but does *not* treat raw length as a correctness signal.
    """
    haystack = str(output).casefold().strip()
    if not haystack:
        return 0.0
    if expected_casefold:
        expected_tokens = re.findall(r"\w+", expected_casefold)
        actual_tokens = re.findall(r"\w+", haystack)
        if not expected_tokens or not actual_tokens:
            return 0.0
        expected_set = set(expected_tokens)
        actual_set = set(actual_tokens)
        intersection = expected_set & actual_set
        if not intersection:
            return 0.0
        score = 1.0 if expected_set <= actual_set else (2 * len(intersection)) / (len(expected_set) + len(actual_set))
        expected_phrase = re.escape(expected_casefold.strip())
        contradiction = re.search(
            rf"\b(?:not|never|no|false|wrong|incorrect)\b.{{0,48}}{expected_phrase}|"
            rf"{expected_phrase}.{{0,48}}\b(?:not|never|no|false|wrong|incorrect)\b",
            haystack,
        )
        return min(score, 0.25) if contradiction else score
    return 0.5 if haystack[-1] in ".?!" else 0.25


def _expected_score(rating_a: float, rating_b: float) -> float:
    return 1.0 / (1.0 + 10.0 ** ((rating_b - rating_a) / 400.0))


def _winner(
    candidate_score: float,
    baseline_score: float,
    *,
    candidate_label: OutcomeLabel,
    baseline_label: OutcomeLabel,
) -> OutcomeLabel:
    if math.isclose(candidate_score, baseline_score):
        return "tie"
    return candidate_label if candidate_score > baseline_score else baseline_label
