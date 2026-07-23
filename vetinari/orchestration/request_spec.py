"""Vetinari Request Specification — Engineering Drawing for Orders.

Before any work starts, a RequestSpec is produced — the "engineering
drawing" for this order, built from clarification answers + smart defaults.
This ensures the system has a fully specified understanding of what to build,
what's in scope, and what "done" looks like.
"""

from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

try:
    from vetinari.orchestration.intake import TIER_PIPELINES, Tier
except (ImportError, AttributeError):  # pragma: no cover - exercised by partial import/stub environments

    class RequestSpecTierFallback(Enum):
        """Fallback request tier used when intake is temporarily unavailable."""

        EXPRESS = "express"
        STANDARD = "standard"
        CUSTOM = "custom"

    Tier = RequestSpecTierFallback
    TIER_PIPELINES = {
        Tier.EXPRESS: ["worker"],
        Tier.STANDARD: ["foreman", "worker", "inspector"],
        Tier.CUSTOM: ["foreman", "worker", "inspector"],
    }
from vetinari.utils.serialization import dataclass_to_dict

logger = logging.getLogger(__name__)


# Compiled patterns for file reference extraction
_FILE_PATTERN = re.compile(
    r"(?:[\w./\\-]+\.(?:py|js|ts|jsx|tsx|yaml|yml|json|md|toml|cfg|ini|html|css|sql|sh))",
)

# Category-default acceptance criteria
_CATEGORY_CRITERIA: dict[str, list[str]] = {
    "code": ["All tests pass", "No lint errors", "Type hints on new functions"],
    "research": ["Findings are cited", "Sources are listed"],
    "docs": ["Documentation is updated", "Examples are correct"],
    "security": ["No new vulnerabilities introduced", "Security tests pass"],
    "test": ["Test coverage increased", "All new tests pass"],
    "refactor": ["All existing tests still pass", "No behavior changes"],
    "debug": ["Root cause identified", "Fix verified with regression test"],
    "general": ["Task requirements met"],
    "analysis": ["Analysis is complete with evidence"],
}


# ── QA Dataclass ───────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class QA:
    """A question/answer pair from clarification.

    Args:
        question: The clarification question asked.
        answer: The answer received.
    """

    question: str = ""
    answer: str = ""

    def to_dict(self) -> dict[str, str]:
        """Converts the question/answer pair to a plain dictionary."""
        return dataclass_to_dict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> QA:
        """Deserialize from dict.

        Args:
            data: Dict with question and answer keys.

        Returns:
            QA instance.
        """
        return cls(
            question=str(data.get("question", "")),
            answer=str(data.get("answer", "")),
        )


# ── RequestSpec ────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class RequestSpec:
    """Engineering drawing — fully specified before production starts.

    Args:
        goal: What the user wants.
        tier: Express / standard / custom.
        category: Code / research / docs / etc.
        acceptance_criteria: How we know it's done.
        scope: Files/modules in scope.
        out_of_scope: What NOT to touch.
        constraints: User-specified constraints.
        clarifications: Questions asked and answers received.
        confidence: System's confidence the spec is complete (0.0-1.0).
        estimated_complexity: Complexity estimate (1-10 scale).
        suggested_pipeline: Agent types in the suggested pipeline.
    """

    goal: str = ""
    tier: str = "standard"
    category: str = "general"
    acceptance_criteria: list[str] = field(default_factory=list)
    scope: list[str] = field(default_factory=list)
    out_of_scope: list[str] = field(default_factory=list)
    constraints: dict[str, Any] = field(default_factory=dict)
    clarifications: list[QA] = field(default_factory=list)
    confidence: float = 1.0
    confidence_source: str = "heuristic_request_spec_v1"
    confidence_calibrated: bool = False
    confidence_baseline_ref: str = "none:heuristic_not_eval_calibrated"
    estimated_complexity: int = 5
    suggested_pipeline: list[str] = field(default_factory=list)

    def __repr__(self) -> str:
        return (
            f"RequestSpec(tier={self.tier!r}, category={self.category!r}, "
            f"confidence={self.confidence!r}, estimated_complexity={self.estimated_complexity!r})"
        )

    def to_dict(self) -> dict[str, Any]:
        """Converts the request specification to a plain dictionary for serialization."""
        return dataclass_to_dict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RequestSpec:
        """Deserialize from dict.

        Args:
            data: Dict representation.

        Returns:
            RequestSpec instance.
        """
        clarifications_raw = data.get("clarifications", [])
        if not isinstance(clarifications_raw, list):
            clarifications_raw = []
        clarifications = [
            QA.from_dict(qa) if isinstance(qa, dict) else QA(question=str(qa)) for qa in clarifications_raw
        ]

        def _as_list(val: object, key: str) -> list:
            """Coerce field to list; log a warning if malformed."""
            if isinstance(val, list):
                return val
            if val is None:
                return []
            logger.warning(
                "[RequestSpec] Expected list for field %r but got %s — coercing to empty list",
                key,
                type(val).__name__,
            )
            return []

        def _as_dict(val: object, key: str) -> dict:
            """Coerce field to dict; log a warning if malformed."""
            if isinstance(val, dict):
                return val
            if val is None:
                return {}
            logger.warning(
                "[RequestSpec] Expected dict for field %r but got %s — coercing to empty dict",
                key,
                type(val).__name__,
            )
            return {}

        return cls(
            goal=str(data.get("goal", "")),
            tier=str(data.get("tier", "standard")),
            category=str(data.get("category", "general")),
            acceptance_criteria=_as_list(data.get("acceptance_criteria"), "acceptance_criteria"),
            scope=_as_list(data.get("scope"), "scope"),
            out_of_scope=_as_list(data.get("out_of_scope"), "out_of_scope"),
            constraints=_as_dict(data.get("constraints"), "constraints"),
            clarifications=clarifications,
            confidence=float(data.get("confidence", 1.0)),
            confidence_source=str(data.get("confidence_source", "heuristic_request_spec_v1")),
            confidence_calibrated=bool(data.get("confidence_calibrated")),
            confidence_baseline_ref=str(data.get("confidence_baseline_ref", "none:heuristic_not_eval_calibrated")),
            estimated_complexity=int(data.get("estimated_complexity", 5)),
            suggested_pipeline=_as_list(data.get("suggested_pipeline"), "suggested_pipeline"),
        )


# ── RequestSpecBuilder ─────────────────────────────────────────────────


class RequestSpecBuilder:
    """Builds a RequestSpec from goal + tier + category + clarifications.

    Assembles the engineering drawing by extracting file references,
    generating acceptance criteria from category defaults and
    clarification answers, estimating complexity, and computing
    confidence scores.
    """

    def build(
        self,
        goal: str,
        tier: Tier,
        category: str,
        clarifications: list[QA] | None = None,
    ) -> RequestSpec:
        """Build a complete RequestSpec.

        Args:
            goal: The user's goal string.
            tier: The intake tier.
            category: The goal category (code, research, etc.).
            clarifications: Optional list of QA pairs from clarification.

        Returns:
            Fully assembled RequestSpec.
        """
        clarifications = clarifications or []

        # 1. Parse goal for file references → scope
        scope = self._extract_file_references(goal)

        # 2. Generate acceptance criteria
        criteria = self._generate_acceptance_criteria(goal, category, clarifications)

        # 3. Estimate complexity
        complexity = self._estimate_complexity(goal, scope, category)

        # 4. Compute confidence
        confidence = self._compute_confidence(goal, clarifications, criteria)

        # 5. Get suggested pipeline from tier
        pipeline = list(TIER_PIPELINES.get(tier, TIER_PIPELINES[Tier.STANDARD]))

        logger.info(
            "[RequestSpec] Built spec: tier=%s, category=%s, scope=%d files, "
            "criteria=%d, complexity=%d, confidence=%.2f",
            tier.value,
            category,
            len(scope),
            len(criteria),
            complexity,
            confidence,
        )

        return RequestSpec(
            goal=goal,
            tier=tier.value,
            category=category,
            acceptance_criteria=criteria,
            scope=scope,
            out_of_scope=[],
            constraints={},
            clarifications=clarifications,
            confidence=confidence,
            confidence_source="heuristic_request_spec_v1",
            confidence_calibrated=False,
            confidence_baseline_ref="none:heuristic_not_eval_calibrated",
            estimated_complexity=complexity,
            suggested_pipeline=pipeline,
        )

    @staticmethod
    def _extract_file_references(goal: str) -> list[str]:
        """Parse file paths from the goal text.

        Args:
            goal: The user's goal string.

        Returns:
            List of file paths found in the goal.
        """
        matches = _FILE_PATTERN.findall(goal)
        # Deduplicate while preserving order
        seen: set[str] = set()
        result: list[str] = []
        for m in matches:
            if m not in seen:
                seen.add(m)
                result.append(m)
        return result

    @staticmethod
    def _generate_acceptance_criteria(
        goal: str,
        category: str,
        clarifications: list[QA],
    ) -> list[str]:
        """Generate acceptance criteria from category defaults + clarifications.

        Args:
            goal: The user's goal.
            category: The goal category.
            clarifications: QA pairs from clarification.

        Returns:
            List of acceptance criteria strings.
        """
        criteria: list[str] = []

        # Category defaults
        category_criteria = _CATEGORY_CRITERIA.get(category, _CATEGORY_CRITERIA["general"])
        criteria.extend(category_criteria)

        # Extract explicit criteria from clarification answers
        criteria.extend(f"Clarified: {qa.answer[:200]}" for qa in clarifications if qa.answer and len(qa.answer) > 5)

        return criteria

    @staticmethod
    def _estimate_complexity(
        goal: str,
        scope: list[str],
        category: str,
    ) -> int:
        """Estimate task complexity on a 1-10 scale.

        Args:
            goal: The user's goal.
            scope: Files in scope.
            category: The goal category.

        Returns:
            Complexity estimate (1-10).
        """
        words = len(goal.split())
        file_count = len(scope)

        # Base complexity from word count
        if words < 10:
            complexity = 2
        elif words < 30:
            complexity = 4
        elif words < 60:
            complexity = 6
        else:
            complexity = 8

        # Adjust for file count
        complexity += min(file_count, 3)

        # Category multiplier
        if category in ("security", "refactor"):
            complexity += 1

        return max(1, min(10, complexity))

    @staticmethod
    def _compute_confidence(
        goal: str,
        clarifications: list[QA],
        criteria: list[str],
    ) -> float:
        """Compute confidence that the spec is complete.

        Args:
            goal: The user's goal.
            clarifications: QA pairs.
            criteria: Generated acceptance criteria.

        Returns:
            Confidence score (0.0-1.0).
        """
        confidence = 0.7  # Base confidence

        # More criteria = more confidence
        if len(criteria) >= 3:
            confidence += 0.1

        # Clarifications answered = higher confidence
        answered = sum(1 for qa in clarifications if qa.answer)
        if answered > 0:
            confidence += 0.1 * min(answered, 2)

        # Longer goals tend to be more specific
        words = len(goal.split())
        if words >= 15:
            confidence += 0.05
        if words >= 30:
            confidence += 0.05

        return max(0.0, min(0.85, confidence))


# ── Singleton ──────────────────────────────────────────────────────────

_instance: RequestSpecBuilder | None = None
_instance_lock = threading.Lock()


def get_spec_builder() -> RequestSpecBuilder:
    """Return the singleton RequestSpecBuilder instance (thread-safe).

    Returns:
        The shared RequestSpecBuilder instance.
    """
    global _instance
    if _instance is None:
        with _instance_lock:
            if _instance is None:
                _instance = RequestSpecBuilder()
    return _instance


def reset_spec_builder() -> None:
    """Reset the singleton (for testing)."""
    global _instance
    with _instance_lock:
        _instance = None
