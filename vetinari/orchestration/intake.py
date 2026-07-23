"""Vetinari Request Intake — Configure-to-Order complexity classification.

Classifies incoming requests by complexity and routes to the appropriate
intake process (Express/Standard/Custom). Implements the factory principle:
"A well-specified order prevents 80% of downstream defects."

The classifier uses rule-based feature extraction with optional Thompson
Sampling override for adaptive tier routing based on historical outcomes.
"""

from __future__ import annotations

import hashlib
import logging
import re
import threading
import unicodedata
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from vetinari.types import AgentType
from vetinari.utils.serialization import dataclass_to_dict

logger = logging.getLogger(__name__)


# ── Tier enum ──────────────────────────────────────────────────────────

# Compiled patterns for feature extraction
_AMBIGUOUS_WORDS = re.compile(
    r"\b(something|stuff|things?|somehow|whatever|whichever|"
    r"maybe|perhaps|probably|might|could\s+be|"
    r"appropriate|properly|correctly|good\s+enough|nice)\b",
    re.IGNORECASE,
)

_CROSS_CUTTING_KEYWORDS = re.compile(
    r"\b(refactor|migrate|rename|across|all\s+files|everywhere|global|"
    r"security|auth|logging|database|schema|api|breaking\s+change|"
    r"architecture|redesign|overhaul)\b",
    re.IGNORECASE,
)

_FILE_REFERENCES = re.compile(
    r"(?:[\w./\\-]+\.(?:py|js|ts|yaml|yml|json|md|toml|cfg|ini|html|css|sql))"
    r"|(?:[\w/\\-]+/[\w/\\-]+)",
)

_NOVEL_DOMAIN_KEYWORDS = re.compile(
    r"\b(machine\s+learning|neural|transformer|blockchain|kubernetes|"
    r"distributed|real-?time|streaming|graphql|grpc|websocket|"
    r"microservice|event[- ]driven|cqrs|saga)\b",
    re.IGNORECASE,
)


class Tier(Enum):
    """Request complexity tier for pipeline routing."""

    EXPRESS = "express"
    STANDARD = "standard"
    CUSTOM = "custom"


@dataclass
class PipelinePaused:
    """Signal that the pipeline is paused waiting for user input.

    Returned when the clarification step detects ambiguity and needs
    user answers before proceeding.

    Args:
        questions: List of questions to ask the user.
        pipeline_state: Serializable pipeline state for resume.
        tier: The intake tier that triggered clarification.
        goal: The original user goal.
        confidence: The intake confidence score.
    """

    questions: list[str] = field(default_factory=list)
    pipeline_state: dict[str, Any] = field(default_factory=dict)
    tier: str = "custom"
    goal: str = ""
    confidence: float = 0.0

    def __repr__(self) -> str:
        return f"PipelinePaused(tier={self.tier!r}, confidence={self.confidence!r}, questions={len(self.questions)})"

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a dict with a ``paused`` sentinel for API consumers.

        Returns:
            Dictionary representation of this instance with an added ``paused``
            key set to ``True`` so callers can detect pipeline-pause responses.
        """
        result = dataclass_to_dict(self)
        result["paused"] = True
        return result


# ── Tier Pipelines ─────────────────────────────────────────────────────

# Maps each tier to the ordered sequence of agent types in its pipeline.
# CUSTOM uses a two-pass worker phase for complex execution, followed by
# inspector verification and final foreman synthesis.
TIER_PIPELINES: dict[Tier, list[str]] = {
    Tier.EXPRESS: [AgentType.WORKER.value, AgentType.INSPECTOR.value],
    Tier.STANDARD: [AgentType.FOREMAN.value, AgentType.WORKER.value, AgentType.INSPECTOR.value],
    Tier.CUSTOM: [
        AgentType.FOREMAN.value,
        AgentType.WORKER.value,
        AgentType.WORKER.value,
        AgentType.INSPECTOR.value,
        AgentType.FOREMAN.value,
    ],
}


# ── IntakeFeatures ─────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class IntakeFeatures:
    """Feature vector extracted from a goal string for classification.

    Args:
        word_count: Number of words in the goal.
        file_count: Number of file references detected.
        has_ambiguous_words: Whether vague/ambiguous language was detected.
        question_marks: Number of question marks in the goal.
        cross_cutting_keywords: Count of cross-cutting concern keywords.
        domain_novelty_score: 0.0-1.0 score of domain novelty (novel tech keywords).
        pattern_key: SHA-256 hash of normalized goal for Thompson arm lookup.
        confidence: 0.0-1.0 confidence that the spec is complete enough.
    """

    word_count: int = 0
    file_count: int = 0
    has_ambiguous_words: bool = False
    question_marks: int = 0
    cross_cutting_keywords: int = 0
    domain_novelty_score: float = 0.0
    pattern_key: str = ""
    confidence: float = 1.0

    def __repr__(self) -> str:
        return (
            f"IntakeFeatures(word_count={self.word_count!r}, confidence={self.confidence!r}, "
            f"has_ambiguous_words={self.has_ambiguous_words!r})"
        )


# ── RequestIntake ──────────────────────────────────────────────────────

# Thresholds for tier classification
EXPRESS_MAX_WORDS = 20  # Short, clear requests
EXPRESS_MAX_FILES = 1  # Single-file changes
CUSTOM_MIN_CROSS_CUTTING = 3  # Multiple cross-cutting concerns
CUSTOM_MIN_NOVELTY = 0.7  # High domain novelty
CONFIDENCE_THRESHOLD = 0.85  # Below this, route to clarification


class RequestIntake:
    """Configure-to-Order intake station.

    Classifies incoming requests by complexity and routes them to the
    appropriate pipeline tier (Express/Standard/Custom).

    Args:
        thompson: Optional Thompson Sampling selector for adaptive routing.
    """

    def __init__(self, thompson: Any | None = None) -> None:
        self._thompson = thompson

    def classify(
        self,
        goal: str,
        context: dict[str, Any] | None = None,
        *,
        _features: IntakeFeatures | None = None,
    ) -> Tier:
        """Classify a request into Express/Standard/Custom tier.

        Args:
            goal: The user's goal string.
            context: Optional context dict with additional metadata.
            _features: Pre-computed features to avoid double extraction
                (internal use by ``classify_with_features``).

        Returns:
            The classified Tier.
        """
        context = context or {}
        goal = unicodedata.normalize("NFC", goal)
        features = _features or self._extract_features(goal, context)

        # Rule-based initial classification — check Custom triggers first,
        # then Express (requires all simplicity checks), else Standard
        if (
            features.has_ambiguous_words
            or features.cross_cutting_keywords >= CUSTOM_MIN_CROSS_CUTTING
            or features.domain_novelty_score >= CUSTOM_MIN_NOVELTY
        ):
            initial_tier = Tier.CUSTOM
        elif (
            features.word_count < EXPRESS_MAX_WORDS
            and features.file_count <= EXPRESS_MAX_FILES
            and features.question_marks == 0
            and features.cross_cutting_keywords == 0
        ):
            initial_tier = Tier.EXPRESS
        else:
            initial_tier = Tier.STANDARD

        # Thompson override: if we have enough data, let the bandit decide
        if self._thompson is not None:
            try:
                if hasattr(self._thompson, "has_sufficient_data") and self._thompson.has_sufficient_data(
                    features.pattern_key,
                ):
                    learned_tier_str = self._thompson.select_tier(features.pattern_key)
                    learned_tier = Tier(learned_tier_str)
                    logger.info(
                        "[Intake] Thompson override: %s -> %s for pattern %s",
                        initial_tier.value,
                        learned_tier.value,
                        features.pattern_key[:12],
                    )
                    return learned_tier
            except Exception as exc:
                logger.warning("[Intake] Thompson override failed, using rule-based: %s", exc)

        logger.info(
            "[Intake] Classified as %s (words=%d, files=%d, ambiguous=%s, cross_cutting=%d, novelty=%.2f)",
            initial_tier.value,
            features.word_count,
            features.file_count,
            features.has_ambiguous_words,
            features.cross_cutting_keywords,
            features.domain_novelty_score,
        )
        return initial_tier

    def classify_with_features(
        self,
        goal: str,
        context: dict[str, Any] | None = None,
    ) -> tuple[Tier, IntakeFeatures]:
        """Classify and return both the tier and extracted features.

        Args:
            goal: The user's goal string.
            context: Optional context dict.

        Returns:
            Tuple of (Tier, IntakeFeatures).
        """
        context = context or {}
        features = self._extract_features(goal, context)
        tier = self.classify(goal, context, _features=features)
        return tier, features

    @staticmethod
    def _extract_features(goal: str, context: dict[str, Any]) -> IntakeFeatures:
        """Extract classification features from a goal string.

        Args:
            goal: The user's goal string.
            context: Context dict with additional metadata.

        Returns:
            Populated IntakeFeatures.
        """
        words = goal.split()
        word_count = len(words)

        # File references
        file_refs = _FILE_REFERENCES.findall(goal)
        file_count = len(file_refs) + context.get("file_count", 0)

        # Ambiguity detection
        ambiguous_matches = _AMBIGUOUS_WORDS.findall(goal)
        has_ambiguous = len(ambiguous_matches) > 0

        # Question marks
        question_marks = goal.count("?")

        # Cross-cutting keywords
        cross_cutting = _CROSS_CUTTING_KEYWORDS.findall(goal)
        cross_cutting_count = len(cross_cutting)

        # Domain novelty: proportion of novel-domain keywords
        novel_matches = _NOVEL_DOMAIN_KEYWORDS.findall(goal)
        novelty_score = min(len(novel_matches) / 3.0, 1.0) if word_count > 0 else 0.0

        # Pattern key: normalized hash for Thompson arm lookup
        normalized = " ".join(sorted({w.lower() for w in words if len(w) > 3}))
        pattern_key = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]

        # Confidence: high for clear, specific goals; low for vague ones
        confidence = 1.0
        if has_ambiguous:
            confidence -= 0.2
        if question_marks > 0:
            confidence -= 0.1 * min(question_marks, 3)
        if word_count < 5:
            confidence -= 0.3
        if cross_cutting_count >= CUSTOM_MIN_CROSS_CUTTING:
            confidence -= 0.15
        confidence = max(0.0, min(1.0, confidence))

        return IntakeFeatures(
            word_count=word_count,
            file_count=file_count,
            has_ambiguous_words=has_ambiguous,
            question_marks=question_marks,
            cross_cutting_keywords=cross_cutting_count,
            domain_novelty_score=novelty_score,
            pattern_key=pattern_key,
            confidence=confidence,
        )


# ── Singleton ──────────────────────────────────────────────────────────

_instance: RequestIntake | None = None
_instance_lock = threading.Lock()


def get_request_intake(thompson: Any | None = None) -> RequestIntake:
    """Return the singleton RequestIntake instance (thread-safe).

    Args:
        thompson: Optional Thompson selector for first-time initialization.

    Returns:
        The shared RequestIntake instance.
    """
    global _instance
    if _instance is None:
        with _instance_lock:
            if _instance is None:
                _instance = RequestIntake(thompson=thompson)
    return _instance


def reset_request_intake() -> None:
    """Reset the singleton (for testing)."""
    global _instance
    with _instance_lock:
        _instance = None
