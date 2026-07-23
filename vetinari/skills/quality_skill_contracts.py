"""Shared contracts for the quality skill tool."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from vetinari.skills.quality_skill_patterns import build_security_patterns
from vetinari.types import QualityGrade, SeverityLevel


class QualityMode(str, Enum):
    """Modes of the unified quality skill."""

    CODE_REVIEW = "code_review"
    SECURITY_AUDIT = "security_audit"
    TEST_GENERATION = "test_generation"
    SIMPLIFICATION = "simplification"
    PERFORMANCE_REVIEW = "performance_review"
    BEST_PRACTICES = "best_practices"


SECURITY_PATTERNS = build_security_patterns()


@dataclass(frozen=True, slots=True)
class QualityIssue:
    """A single quality or security issue."""

    title: str
    severity: SeverityLevel
    description: str
    location: str | None = None
    suggestion: str | None = None
    cwe_id: str | None = None
    owasp_category: str | None = None

    def __repr__(self) -> str:
        """Show key identifying fields for debugging."""
        return f"QualityIssue(title={self.title!r}, severity={self.severity!r}, location={self.location!r})"

    def to_dict(self) -> dict[str, Any]:
        """Serialize this issue to a plain dictionary for JSON output.

        Returns:
            Dictionary with title, severity value, description, and suggestion,
            plus optional location, cwe_id, and owasp_category when present.
        """
        result: dict[str, Any] = {
            "title": self.title,
            "severity": self.severity.value,
            "description": self.description,
            "suggestion": self.suggestion,
        }
        if self.location:
            result["location"] = self.location
        if self.cwe_id:
            result["cwe_id"] = self.cwe_id
        if self.owasp_category:
            result["owasp_category"] = self.owasp_category
        return result


@dataclass
class QualityResult:
    """Result of a quality operation."""

    success: bool
    issues: list[QualityIssue] = field(default_factory=list)
    grade: QualityGrade | None = None
    score: float = 0.0
    summary: str | None = None
    recommendations: list[str] = field(default_factory=list)
    tests: str | None = None
    metrics: dict[str, Any] = field(default_factory=dict)

    def __repr__(self) -> str:
        """Show key identifying fields for debugging."""
        return f"QualityResult(success={self.success!r}, grade={self.grade!r}, score={self.score!r})"

    def to_dict(self) -> dict[str, Any]:
        """Serialize this QualityResult to a plain dictionary suitable for JSON output.

        Returns:
            Dictionary containing the quality grade, score, issues,
            recommendations, and associated metrics.
        """
        return {
            "success": self.success,
            "issues": [i.to_dict() for i in self.issues],
            "quality_grade": self.grade.value if self.grade else None,
            "score": self.score,
            "summary": self.summary,
            "recommendations": self.recommendations,
            "tests": self.tests,
            "metrics": self.metrics,
        }


__all__ = [
    "SECURITY_PATTERNS",
    "QualityIssue",
    "QualityMode",
    "QualityResult",
]
