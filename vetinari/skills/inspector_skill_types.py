"""Shared types for InspectorSkillTool."""

from __future__ import annotations

import dataclasses
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from vetinari.agents.contracts import OutcomeSignal, Provenance, ToolEvidence
from vetinari.types import EvidenceBasis

_FUNC_DEF_RE = re.compile(r"def\s+(\w+)\s*\(")


class InspectorMode(str, Enum):
    """Modes of the Inspector skill tool."""

    CODE_REVIEW = "code_review"
    SECURITY_AUDIT = "security_audit"
    TEST_GENERATION = "test_generation"
    SIMPLIFICATION = "simplification"


@dataclass(frozen=True, slots=True)
class ReviewIssue:
    """A single issue found during review."""

    severity: str  # critical, high, medium, low, info
    description: str
    file: str = ""
    line: int = 0
    category: str = ""
    cwe: str = ""
    owasp: str = ""
    suggestion: str = ""

    def __repr__(self) -> str:
        """Show key identifying fields for debugging."""
        return f"ReviewIssue(severity={self.severity!r}, category={self.category!r}, file={self.file!r})"


@dataclass
class InspectorResult:
    """Result from Inspector skill execution."""

    passed: bool = True
    issues: list[ReviewIssue] = field(default_factory=list)
    grade: str = "A"  # A, B, C, D, F
    score: float = 1.0  # 0.0 to 1.0
    suggestions: list[str] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    self_check_passed: bool | None = None

    def __repr__(self) -> str:
        """Show key identifying fields for debugging."""
        return f"InspectorResult(passed={self.passed!r}, grade={self.grade!r}, score={self.score!r})"

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dictionary.

        Returns:
            Dictionary with review result fields.
        """
        result: dict[str, Any] = {
            "passed": self.passed,
            "issues": [dataclasses.asdict(i) for i in self.issues],
            "grade": self.grade,
            "score": self.score,
            "suggestions": self.suggestions,
            "metrics": self.metrics,
        }
        if self.self_check_passed is not None:
            result["self_check_passed"] = self.self_check_passed
        return result


def _inspector_result_to_signal(result: InspectorResult, mode: str) -> OutcomeSignal:
    """Convert an InspectorResult to a heuristic-based OutcomeSignal.

    Produces a ``TOOL_EVIDENCE`` basis signal because these Inspector modes are
    deterministic scanners, not live LLM judgments. Callers that have separate
    model-review signals should merge them via
    ``aggregate_outcome_signals()``.

    Args:
        result: The InspectorResult produced by an inspection mode handler.
        mode: The InspectorMode value string for provenance labelling.

    Returns:
        OutcomeSignal with basis=LLM_JUDGMENT and populated provenance.
    """
    issues: tuple[str, ...] = tuple(
        f"{i.severity.upper()}: {i.description}" + (f" (line {i.line})" if i.line else "") for i in result.issues
    )
    suggestions: tuple[str, ...] = tuple(result.suggestions)

    return OutcomeSignal(
        passed=result.passed,
        score=result.score,
        basis=EvidenceBasis.TOOL_EVIDENCE,
        tool_evidence=(
            ToolEvidence(
                tool_name="InspectorSkill",
                command=f"inspector:{mode}",
                exit_code=0 if result.passed else 1,
                stdout_snippet=f"issues={len(issues)} suggestions={len(suggestions)} grade={result.grade}",
                passed=result.passed,
            ),
        ),
        llm_judgment=None,
        issues=issues,
        suggestions=suggestions,
        provenance=Provenance(
            source=f"vetinari.skills.inspector_skill.{mode}",
            timestamp_utc=datetime.now(timezone.utc).isoformat(),
            model_id=None,
        ),
    )
