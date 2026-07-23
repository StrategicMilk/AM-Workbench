"""Lightweight ML Classifiers — replacing LLM calls for classification tasks.

Three classifiers that use feature engineering (no ML training needed initially):
  - GoalClassifier: Classifies user goals into routing categories
  - DefectClassifier: Classifies quality rejection reasons into DefectCategory
  - AmbiguityDetector: Detects whether a request needs clarification

All use keyword/feature-based approaches as cold-start, designed to be
upgraded with trained models when sufficient labeled data accumulates.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from vetinari.types import GoalCategory

from .classifier_keywords import _DEFECT_KEYWORDS, _GOAL_KEYWORDS, DEFECT_CATEGORIES, GOAL_CATEGORIES

logger = logging.getLogger(__name__)
__all__ = [
    "DEFECT_CATEGORIES",
    "GOAL_CATEGORIES",
    "AmbiguityDetector",
    "AmbiguityResult",
    "DefectClassification",
    "DefectClassifier",
    "GoalClassification",
    "GoalClassifier",
]


@dataclass
class GoalClassification:
    """Result of goal classification.

    Args:
        category: The classified goal category.
        confidence: Confidence score (0.0-1.0).
        keyword_matches: Keywords that matched.
    """

    category: str = GoalCategory.GENERAL.value
    confidence: float = 0.0
    keyword_matches: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dictionary.

        Returns:
            Dictionary representation.
        """
        return {
            "category": self.category,
            "confidence": round(self.confidence, 3),
            "keyword_matches": self.keyword_matches,
        }


class GoalClassifier:
    """Classifies user goals into routing categories using keyword features.

    Cold-start approach: TF-IDF-like keyword matching. Designed to be
    upgraded with a trained DistilBERT classifier when sufficient labeled
    data accumulates (50+ examples per category).
    """

    def classify(self, goal_text: str) -> GoalClassification:
        """Classify a goal text into a routing category.

        Args:
            goal_text: The user's goal description.

        Returns:
            GoalClassification with category, confidence, and matched keywords.
        """
        if not goal_text or not goal_text.strip():
            return GoalClassification(category=GoalCategory.GENERAL.value, confidence=0.5)

        text_lower = goal_text.lower()
        scores: dict[str, tuple[float, list[str]]] = {}

        for category, keywords in _GOAL_KEYWORDS.items():
            # Use word-boundary matching to prevent short keywords (e.g. "ui")
            # from matching inside longer words (e.g. "guide", "build", "quit").
            matches = [kw for kw in keywords if re.search(r"\b" + re.escape(kw) + r"\b", text_lower)]
            if matches:
                # Score: number of matches / total keywords, boosted by match count
                score = len(matches) / len(keywords) + 0.1 * len(matches)
                scores[category] = (min(score, 1.0), matches)

        if not scores:
            return GoalClassification(category=GoalCategory.GENERAL.value, confidence=0.3)

        best_category = max(scores, key=lambda k: scores[k][0])
        best_score, best_matches = scores[best_category]

        return GoalClassification(
            category=best_category,
            confidence=best_score,
            keyword_matches=best_matches,
        )


# ---------------------------------------------------------------------------
# Defect Classification
# ---------------------------------------------------------------------------


@dataclass
class DefectClassification:
    """Result of defect classification.

    Args:
        category: The classified defect category.
        confidence: Confidence score (0.0-1.0).
        evidence: Keywords or patterns that matched.
    """

    category: str = "logic_error"
    confidence: float = 0.0
    evidence: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dictionary.

        Returns:
            Dictionary representation.
        """
        return {
            "category": self.category,
            "confidence": round(self.confidence, 3),
            "evidence": self.evidence,
        }


class DefectClassifier:
    """Classifies quality rejection reasons into DefectCategory using text features.

    Cold-start: keyword matching on rejection text. Designed for upgrade
    to trained classifier on (rejection_text_embedding, code_diff_features).
    """

    def classify(
        self,
        rejection_text: str,
        code_diff: str = "",
    ) -> DefectClassification:
        """Classify a rejection reason into a defect category.

        Args:
            rejection_text: The rejection reason text.
            code_diff: Optional code diff for additional context.

        Returns:
            DefectClassification with category, confidence, and evidence.
        """
        if not rejection_text:
            return DefectClassification(category="logic_error", confidence=0.3)

        combined = f"{rejection_text} {code_diff}".lower()
        scores: dict[str, tuple[float, list[str]]] = {}

        for category, keywords in _DEFECT_KEYWORDS.items():
            matches = [kw for kw in keywords if kw in combined]
            if matches:
                score = len(matches) / len(keywords) + 0.1 * len(matches)
                scores[category] = (min(score, 1.0), matches)

        if not scores:
            return DefectClassification(category="logic_error", confidence=0.2)

        best_category = max(scores, key=lambda k: scores[k][0])
        best_score, best_evidence = scores[best_category]

        return DefectClassification(
            category=best_category,
            confidence=best_score,
            evidence=best_evidence,
        )


# ---------------------------------------------------------------------------
# Ambiguity Detection
# ---------------------------------------------------------------------------


@dataclass
class AmbiguityResult:
    """Result of ambiguity detection.

    Args:
        is_ambiguous: Whether the request is ambiguous.
        confidence: Confidence in the ambiguity assessment (0.0-1.0).
        features: Feature values used for detection.
    """

    is_ambiguous: bool = False
    confidence: float = 0.0
    features: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dictionary.

        Returns:
            Dictionary representation.
        """
        return {
            "is_ambiguous": self.is_ambiguous,
            "confidence": round(self.confidence, 3),
            "features": self.features,
        }


# Hedge words that signal uncertainty/vagueness
_HEDGE_WORDS = [
    "maybe",
    "perhaps",
    "possibly",
    "might",
    "could",
    "should",
    "would",
    "somehow",
    "something",
    "somewhere",
    "whatever",
    "anything",
    "stuff",
    "things",
    "kind of",
    "sort of",
    "a bit",
    "somewhat",
]

# Conditional phrases
_CONDITIONAL_PHRASES = [
    "if possible",
    "when needed",
    "as appropriate",
    "as necessary",
    "if applicable",
    "when applicable",
    "depending on",
    "based on",
    "unless",
    "except",
]


# Vague pronouns that indicate the subject of the request is not specified
_VAGUE_PRONOUNS = [
    r"\bit\b",
    r"\bthis\b",
    r"\bthat\b",
    r"\bthey\b",
    r"\bthem\b",
    r"\bthese\b",
    r"\bthose\b",
]

# Patterns that indicate a missing grammatical subject (imperative without object)
_MISSING_SUBJECT_PATTERNS = [
    r"^(?:fix|update|change|make|add|remove|refactor|improve|check|verify|ensure)\s*$",
    r"^(?:fix|update|change|make|add|remove|refactor|improve|check|verify|ensure)\s+(?:it|this|that|them|these|those)\b",
]


class AmbiguityDetector:
    """Detects whether a request is too ambiguous to execute without clarification.

    Uses feature engineering on the request text:
      - Hedge word count
      - Question marks
      - Conditional phrases
      - Specificity score (concrete references vs vague language)
      - Word count
      - File reference count
      - Vague pronoun density (Feature 6)
      - Missing subject detection (Feature 7)
    """

    AMBIGUITY_THRESHOLD = 0.5  # Above this = ambiguous

    def detect(self, request_text: str) -> AmbiguityResult:
        """Detect whether a request needs clarification.

        Returns:
            Value produced for the caller.
        """
        if not request_text or not request_text.strip():
            return AmbiguityResult(
                is_ambiguous=True,
                confidence=0.9,
                features={"reason": "empty_request"},
            )
        text_lower = request_text.lower()
        words = text_lower.split()
        word_count = len(words)
        hedge_count = sum(1 for hw in _HEDGE_WORDS if hw in text_lower)
        hedge_density = hedge_count / max(word_count, 1)
        question_marks = request_text.count("?")
        conditional_count = sum(1 for cp in _CONDITIONAL_PHRASES if cp in text_lower)
        file_refs = len(re.findall(r"\b[\w/]+\.(?:py|js|ts|yaml|json|md)\b", request_text))
        func_refs = len(re.findall(r"\b\w+\(\)", request_text))
        class_refs = len(re.findall(r"\b[A-Z][a-zA-Z]+\b", request_text))
        specificity = (file_refs + func_refs + class_refs * 0.5) / max(word_count, 1)
        length_score = 0.0 if word_count < 3 else (0.5 if word_count < 10 else 1.0)
        vague_pronoun_count = sum(1 for pat in _VAGUE_PRONOUNS if re.search(pat, text_lower))
        vague_pronoun_density = vague_pronoun_count / max(word_count, 1)
        has_missing_subject = any(re.search(pat, text_lower.strip()) for pat in _MISSING_SUBJECT_PATTERNS)
        ambiguity_score = (
            hedge_density * 3.0  # Hedging is strong signal
            + (question_marks * 0.15)  # Questions need answers
            + (conditional_count * 0.2)  # Conditionals add uncertainty
            - specificity * 2.0  # Specific references reduce ambiguity
            - length_score * 0.3  # Longer requests tend to be clearer
            + vague_pronoun_density * 2.0  # Feature 6: vague pronouns boost ambiguity
            + (0.15 if has_missing_subject else 0.0)  # Feature 7: missing subject
        )
        ambiguity_score = max(0.0, min(1.0, ambiguity_score))
        features = {
            "hedge_count": hedge_count,
            "hedge_density": round(hedge_density, 3),
            "question_marks": question_marks,
            "conditional_count": conditional_count,
            "file_refs": file_refs,
            "func_refs": func_refs,
            "specificity": round(specificity, 3),
            "word_count": word_count,
            "length_score": round(length_score, 2),
            "vague_pronoun_count": vague_pronoun_count,
            "vague_pronoun_density": round(vague_pronoun_density, 3),
            "has_missing_subject": has_missing_subject,
        }
        is_ambiguous = ambiguity_score > self.AMBIGUITY_THRESHOLD
        _BORDERLINE_THRESHOLD = 0.25
        if ambiguity_score > _BORDERLINE_THRESHOLD:
            try:
                from vetinari.llm_helpers import check_ambiguity_via_llm

                llm_result = check_ambiguity_via_llm(request_text)
                if llm_result is not None:
                    llm_ambiguous, clarifying_question = llm_result
                    features["llm_override"] = llm_ambiguous
                    features["clarifying_question"] = clarifying_question
                    is_ambiguous = llm_ambiguous
                    logger.info(
                        "LLM ambiguity check overrode heuristic: ambiguous=%s (heuristic_score=%.2f)",
                        llm_ambiguous,
                        ambiguity_score,
                    )
            except Exception:
                logger.warning("LLM ambiguity check unavailable — using heuristic score only for ambiguity detection")
        return AmbiguityResult(
            is_ambiguous=is_ambiguous,
            confidence=abs(ambiguity_score - 0.5) * 2,  # confidence highest at extremes
            features=features,
        )
