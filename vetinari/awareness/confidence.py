"""Canonical confidence computation — single source of truth for confidence scores.

Every component that needs to assess or route on confidence uses ConfidenceResult.
The ConfidenceComputer produces these from token logprobs (primary) or semantic
entropy (fallback for open-ended tasks).
"""

from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from vetinari.types import ConfidenceAction, ConfidenceLevel

logger = logging.getLogger(__name__)


# Logprob thresholds for confidence classification
# Mean logprob values (more negative = less confident)
_THRESHOLD_HIGH = -0.5  # Very confident — model is sure of its tokens
_THRESHOLD_MEDIUM = -1.5  # Moderate confidence — some uncertainty
_THRESHOLD_LOW = -3.0  # Low confidence — substantial uncertainty
# Below _THRESHOLD_LOW = VERY_LOW

# Mapping from confidence level to default routing action
_LEVEL_TO_ACTION: dict[ConfidenceLevel, ConfidenceAction] = {
    ConfidenceLevel.HIGH: ConfidenceAction.PROCEED,
    ConfidenceLevel.MEDIUM: ConfidenceAction.REFINE,
    ConfidenceLevel.LOW: ConfidenceAction.BEST_OF_N,
    ConfidenceLevel.VERY_LOW: ConfidenceAction.DEFER_TO_HUMAN,
}


class UnknownSituation(str, Enum):
    """Why the system cannot produce a confident answer.

    Used by the 'I don't know' protocol — when the system explicitly
    declares uncertainty rather than guessing.
    """

    NO_DATA = "no_data"  # No logprobs or input data available
    STALE_EVIDENCE = "stale_evidence"  # Evidence exists but is outdated
    CONTRADICTORY = "contradictory"  # Multiple signals disagree
    OUT_OF_DOMAIN = "out_of_domain"  # Task is outside the model's training domain
    LOW_EVIDENCE = "low_evidence"  # Some signal but not enough to be confident


@dataclass(frozen=True, slots=True)
class ConfidenceResult:
    """Canonical confidence assessment — the ONE type for all confidence data.

    Every confidence-bearing decision in the pipeline produces or consumes this.
    Replaces the former ConfidenceAssessment and RoutingDecision types.

    Args:
        score: Numeric confidence (mean logprob). More negative = less confident.
        level: Classified confidence level (HIGH/MEDIUM/LOW/VERY_LOW).
        action: Recommended post-generation routing action.
        explanation: Human-readable reason for this classification.
        factors: Contributing factors that influenced the assessment.
        source: What produced this assessment (e.g. "logprob", "semantic_entropy").
        unknown_situation: Set when system explicitly declares "I don't know".
        metadata: Additional context (token_count, min_logprob, std_logprob, etc.).
    """

    score: float
    level: ConfidenceLevel
    action: ConfidenceAction
    explanation: str
    factors: dict[str, float] = field(default_factory=dict)
    source: str = "logprob"
    unknown_situation: UnknownSituation | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __repr__(self) -> str:
        unknown_part = f", unknown={self.unknown_situation.value}" if self.unknown_situation else ""
        return (
            f"ConfidenceResult(score={self.score:.3f}, level={self.level.value!r}, "
            f"action={self.action.value!r}{unknown_part})"
        )

    @property
    def is_actionable(self) -> bool:
        """Whether the confidence is high enough to proceed without intervention."""
        return self.action == ConfidenceAction.PROCEED

    @property
    def needs_human(self) -> bool:
        """Whether this result should be deferred to a human."""
        return self.action == ConfidenceAction.DEFER_TO_HUMAN


class UnknownSituationProtocol:
    """The 'I don't know' protocol — explicit uncertainty handling.

    Instead of guessing when evidence is insufficient, the system produces
    a ConfidenceResult with unknown_situation set, explaining WHY it can't
    be confident rather than producing a low-confidence guess.
    """

    @staticmethod
    def declare_unknown(
        situation: UnknownSituation,
        explanation: str,
        source: str = "unknown_protocol",
    ) -> ConfidenceResult:
        """Produce a ConfidenceResult that explicitly says 'I don't know'.

        Args:
            situation: The type of unknown situation encountered.
            explanation: Human-readable description of what's unknown and why.
            source: What detected the unknown situation.

        Returns:
            ConfidenceResult with VERY_LOW level, DEFER_TO_HUMAN action,
            and the unknown_situation field set.
        """
        return ConfidenceResult(
            score=-999.0,
            level=ConfidenceLevel.VERY_LOW,
            action=ConfidenceAction.DEFER_TO_HUMAN,
            explanation=explanation,
            source=source,
            unknown_situation=situation,
        )


class ConfidenceComputer:
    """Computes canonical ConfidenceResult from token logprobs or response diversity.

    This replaces the assess_confidence() and route_by_confidence() methods
    from the old ConfidenceGate — unified into one class that always returns
    ConfidenceResult.

    Thresholds can be overridden at construction for task-specific tuning.
    """

    def __init__(
        self,
        threshold_high: float = _THRESHOLD_HIGH,
        threshold_medium: float = _THRESHOLD_MEDIUM,
        threshold_low: float = _THRESHOLD_LOW,
    ) -> None:
        self._threshold_high = threshold_high
        self._threshold_medium = threshold_medium
        self._threshold_low = threshold_low

    def compute(self, logprobs: list[float], task_type: str = "general") -> ConfidenceResult:
        """Compute confidence from a sequence of token logprobs.

        Args:
            logprobs: Per-token log probabilities from the model.
                      Empty list triggers the 'I don't know' protocol.
            task_type: Task type for context in the explanation.

        Returns:
            Canonical ConfidenceResult with score, level, action, and factors.
        """
        if not logprobs:
            return UnknownSituationProtocol.declare_unknown(
                UnknownSituation.NO_DATA,
                f"No logprobs available for {task_type} task — cannot assess confidence",
                source="logprob",
            )

        mean_lp = statistics.mean(logprobs)
        min_lp = min(logprobs)
        std_lp = statistics.stdev(logprobs) if len(logprobs) > 1 else 0.0

        level = self._classify_logprob(mean_lp)

        # Track contributing factors
        factors: dict[str, float] = {
            "mean_logprob": round(mean_lp, 4),
            "min_logprob": round(min_lp, 4),
            "std_logprob": round(std_lp, 4),
            "token_count": float(len(logprobs)),
        }

        # Downgrade if variance is very high (inconsistent confidence)
        is_high_variance = std_lp > 2.0
        if is_high_variance and level in (ConfidenceLevel.HIGH, ConfidenceLevel.MEDIUM):
            downgrade_map = {
                ConfidenceLevel.HIGH: ConfidenceLevel.MEDIUM,
                ConfidenceLevel.MEDIUM: ConfidenceLevel.LOW,
            }
            level = downgrade_map[level]
            factors["variance_downgrade"] = 1.0

        action = _LEVEL_TO_ACTION[level]

        explanations = {
            ConfidenceLevel.HIGH: "Model confidence is high; proceed directly.",
            ConfidenceLevel.MEDIUM: "Model confidence is moderate; trigger self-refinement.",
            ConfidenceLevel.LOW: "Model confidence is low; sample multiple answers and pick the best supported result.",
            ConfidenceLevel.VERY_LOW: "Model confidence is very low; defer to a human reviewer.",
        }

        return ConfidenceResult(
            score=mean_lp,
            level=level,
            action=action,
            explanation=explanations[level],
            factors=factors,
            source="logprob",
            metadata={"task_type": task_type},
        )

    def compute_from_responses(
        self,
        responses: list[str],
        similarity_threshold: float = 0.7,
    ) -> ConfidenceResult:
        """Compute confidence via semantic entropy — response diversity as a proxy.

        For open-ended tasks where logprobs alone are unreliable, multiple
        generations are compared. High agreement = high confidence.

        Args:
            responses: Multiple generated responses for the same prompt.
            similarity_threshold: Fraction of similar responses needed for HIGH.

        Returns:
            ConfidenceResult based on response diversity.
        """
        if len(responses) < 2:
            return UnknownSituationProtocol.declare_unknown(
                UnknownSituation.LOW_EVIDENCE,
                "Cannot assess semantic entropy with fewer than 2 responses",
                source="semantic_entropy",
            )

        # Simple heuristic: pairwise word-overlap similarity
        # (production version would use embedding similarity)
        similarities: list[float] = []
        for i in range(len(responses)):
            for j in range(i + 1, len(responses)):
                sim = self._text_similarity(responses[i], responses[j])
                similarities.append(sim)

        mean_sim = statistics.mean(similarities) if similarities else 0.0

        if mean_sim >= similarity_threshold:
            level = ConfidenceLevel.HIGH
        elif mean_sim >= 0.4:
            level = ConfidenceLevel.MEDIUM
        elif mean_sim >= 0.2:
            level = ConfidenceLevel.LOW
        else:
            level = ConfidenceLevel.VERY_LOW

        action = _LEVEL_TO_ACTION[level]

        return ConfidenceResult(
            score=mean_sim,  # Similarity as the "score" for entropy-based assessment
            level=level,
            action=action,
            explanation=f"Semantic entropy assessment: mean similarity {mean_sim:.2f} across {len(responses)} responses",
            factors={
                "mean_similarity": round(mean_sim, 4),
                "response_count": float(len(responses)),
                "pair_count": float(len(similarities)),
            },
            source="semantic_entropy",
        )

    def _classify_logprob(self, mean_logprob: float) -> ConfidenceLevel:
        """Map mean logprob to confidence level.

        Args:
            mean_logprob: Mean log probability from the model output.

        Returns:
            Classified ConfidenceLevel for the given mean logprob.
        """
        if mean_logprob >= self._threshold_high:
            return ConfidenceLevel.HIGH
        if mean_logprob >= self._threshold_medium:
            return ConfidenceLevel.MEDIUM
        if mean_logprob >= self._threshold_low:
            return ConfidenceLevel.LOW
        return ConfidenceLevel.VERY_LOW

    @staticmethod
    def _text_similarity(text_a: str, text_b: str) -> float:
        """Compute word-overlap Jaccard similarity as a fast proxy.

        Args:
            text_a: First text.
            text_b: Second text.

        Returns:
            Similarity score between 0.0 and 1.0.
        """
        if not text_a or not text_b:
            return 0.0
        set_a = set(text_a.lower().split())
        set_b = set(text_b.lower().split())
        if not set_a or not set_b:
            return 0.0
        intersection = set_a & set_b
        union = set_a | set_b
        return len(intersection) / len(union)


def classify_confidence_score(score: float) -> ConfidenceLevel:
    """Classify an agent confidence score (0.0-1.0) into a ConfidenceLevel.

    This is for classifying raw confidence floats stored in the approval queue
    and other non-logprob sources. The ConfidenceComputer's ``_classify_logprob()``
    handles logprob-based classification instead.

    Score thresholds:
    - >= 0.85 → HIGH
    - >= 0.60 → MEDIUM
    - >= 0.30 → LOW
    - < 0.30  → VERY_LOW (includes 0.0 which also signals a fallback)

    Args:
        score: Agent confidence score in the range 0.0 to 1.0.

    Returns:
        The corresponding ConfidenceLevel for operator display.
    """
    if score >= 0.85:
        return ConfidenceLevel.HIGH
    if score >= 0.6:
        return ConfidenceLevel.MEDIUM
    if score >= 0.3:
        return ConfidenceLevel.LOW
    return ConfidenceLevel.VERY_LOW
