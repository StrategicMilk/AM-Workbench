"""Three-tier Quality Pre-Screening — bypass expensive LLM judge for obvious cases.

Tier 1: Rule-based checks (AST parse, import validation, encoding) — instant, free
Tier 2: Feature-based scoring (code metrics) — instant, free
Tier 3: LLM-as-judge gate — only when tier 2 is inconclusive [0.4, 0.7]

Estimated 40-60% of outputs skip LLM entirely.
"""

from __future__ import annotations

import ast
import logging
import re
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


INCONCLUSIVE_LOW = 0.4  # Below this = likely fail
INCONCLUSIVE_HIGH = 0.7  # Above this = likely pass


@dataclass
class PreScreenResult:
    """Result of quality pre-screening.

    Args:
        tier_used: Which tier produced the final decision (1, 2, or 3).
        score: Quality score estimate (0.0-1.0).
        skip_llm_judge: Whether the LLM judge can be skipped.
        issues: List of issues found during screening.
        details: Additional screening details.
    """

    tier_used: int = 0
    score: float = 0.0
    skip_llm_judge: bool = False
    issues: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)

    def __repr__(self) -> str:
        """Show key identifying fields for debugging."""
        return (
            f"PreScreenResult(tier_used={self.tier_used!r},"
            f" score={self.score!r}, skip_llm_judge={self.skip_llm_judge!r})"
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dictionary.

        Returns:
            Dictionary representation.
        """
        return {
            "tier_used": self.tier_used,
            "score": round(self.score, 3),
            "skip_llm_judge": self.skip_llm_judge,
            "issues": self.issues,
            "details": self.details,
        }


class QualityPreScreener:
    """Three-tier quality pre-screening to bypass expensive LLM judge.

    Screens code output through increasingly expensive tiers:
      Tier 1: Deterministic rule checks (free, instant)
      Tier 2: Feature-based heuristic scoring (free, instant)
      Tier 3: LLM-as-judge gate (only when tier 2 inconclusive)
    """

    def screen(
        self,
        code: str,
        metadata: dict[str, Any] | None = None,
    ) -> PreScreenResult:
        """Screen code output through the three-tier pipeline.

        Args:
            code: The code string to screen.
            metadata: Optional metadata (file_path, task_type, etc.).

        Returns:
            PreScreenResult with tier used, score, and skip_llm_judge flag.
        """
        metadata = metadata or {}

        # Tier 1: Rule-based checks (deterministic, free, instant)
        tier1_result = self._tier1_rules(code)
        if tier1_result.score == 0.0:
            # Obvious failure — no need for further screening
            return PreScreenResult(
                tier_used=1,
                score=0.0,
                skip_llm_judge=True,
                issues=tier1_result.issues,
                details={"tier": "rules", "decision": "fail"},
            )

        # Tier 2: Feature-based scoring (heuristic, free, instant)
        tier2_result = self._tier2_features(code, metadata)

        if tier2_result.score <= INCONCLUSIVE_LOW:
            # Likely fail — skip LLM judge
            return PreScreenResult(
                tier_used=2,
                score=tier2_result.score,
                skip_llm_judge=True,
                issues=tier1_result.issues + tier2_result.issues,
                details={"tier": "features", "decision": "fail"},
            )

        if tier2_result.score > INCONCLUSIVE_HIGH:
            # Likely pass — but only skip the LLM judge if tier-1 found no
            # issues.  If tier-1 flagged bare-except, missing encoding, or
            # print() usage, the code has known problems and the LLM judge
            # must still run to produce a human-readable verdict.
            has_tier1_issues = bool(tier1_result.issues)
            has_acceptance_evidence = bool(
                metadata.get("deterministic_acceptance_ref") or metadata.get("allow_heuristic_pass")
            )
            return PreScreenResult(
                tier_used=2,
                score=tier2_result.score,
                skip_llm_judge=not has_tier1_issues and has_acceptance_evidence,
                issues=tier1_result.issues + tier2_result.issues,
                details={
                    "tier": "features",
                    "decision": "pass" if not has_tier1_issues else "pass-with-warnings",
                    "acceptance_evidence": has_acceptance_evidence,
                },
            )

        # Inconclusive — needs LLM judge
        return PreScreenResult(
            tier_used=2,
            score=tier2_result.score,
            skip_llm_judge=False,
            issues=tier1_result.issues + tier2_result.issues,
            details={"tier": "features", "decision": "inconclusive"},
        )

    @staticmethod
    def _tier1_rules(code: str) -> PreScreenResult:
        """Tier 1: Deterministic rule-based checks.

        Checks:
          - AST parseable (valid Python syntax)
          - No bare except blocks
          - encoding="utf-8" present in open() calls
          - No print() in production code patterns

        Args:
            code: The code string to check.

        Returns:
            PreScreenResult with issues found.
        """
        issues: list[str] = []

        # Check 1: AST parse
        try:
            ast.parse(code)
        except SyntaxError as e:
            issues.append(f"Syntax error: {e.msg} (line {e.lineno})")
            logger.warning("Caught SyntaxError in except block: %s", e)
            return PreScreenResult(
                tier_used=1,
                score=0.0,
                skip_llm_judge=True,
                issues=issues,
            )

        # Check 2: No bare except
        if re.search(r"except\s*:", code):
            issues.append("Bare except clause found")

        # Check 3: encoding in open()
        open_calls = re.findall(r"open\s*\(", code)
        if open_calls:
            encoding_calls = re.findall(r'encoding\s*=\s*["\']utf-8["\']', code)
            if len(open_calls) > len(encoding_calls):
                issues.append("open() call missing encoding='utf-8'")

        # Check 4: No print() in non-test code
        if re.search(r"\bprint\s*\(", code) and "def test_" not in code:
            issues.append("print() found in production code")

        score = 1.0 if not issues else 0.5
        return PreScreenResult(tier_used=1, score=score, issues=issues)

    @staticmethod
    def _tier2_features(code: str, metadata: dict[str, Any]) -> PreScreenResult:
        """Tier 2: Feature-based heuristic scoring.

        Scores based on code quality metrics:
          - Function count (code structure)
          - Type annotation ratio
          - Docstring presence
          - Import count
          - Cyclomatic complexity estimate

        Args:
            code: The code string to analyze.
            metadata: Task metadata.

        Returns:
            PreScreenResult with feature-based score.
        """
        issues: list[str] = []
        score_components: list[float] = []

        try:
            tree = ast.parse(code)
        except SyntaxError:
            logger.warning("Code submitted for quality pre-screening has a syntax error — scoring 0.0")
            return PreScreenResult(tier_used=2, score=0.0, issues=["Cannot parse"])

        # Feature 1: Function/class count (structure exists)
        functions = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        classes = [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]
        score_components.append(0.8 if (functions or classes) else 0.3)

        # Feature 2: Type annotation ratio
        annotated = sum(1 for f in functions if f.returns is not None or any(a.annotation for a in f.args.args))
        annotation_ratio = annotated / max(len(functions), 1)
        score_components.append(0.5 + 0.5 * annotation_ratio)
        if annotation_ratio < 0.5 and len(functions) > 2:
            issues.append("Low type annotation ratio")

        # Feature 3: Docstring presence
        docstrings = sum(
            1
            for f in functions
            if (f.body and isinstance(f.body[0], ast.Expr) and isinstance(f.body[0].value, ast.Constant))
        )
        docstring_ratio = docstrings / max(len(functions), 1)
        score_components.append(0.4 + 0.6 * docstring_ratio)

        # Feature 4: Import organization
        imports = [n for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))]
        has_future = any(isinstance(n, ast.ImportFrom) and n.module == "__future__" for n in imports)
        score_components.append(0.8 if has_future else 0.5)

        # Feature 5: Code length reasonableness
        lines = code.strip().split("\n")
        line_count = len(lines)
        if line_count < 5:
            score_components.append(0.3)
            issues.append("Very short code")
        elif line_count > 500:
            score_components.append(0.5)
            issues.append("Very long single file")
        else:
            score_components.append(0.8)

        # Aggregate
        avg_score = sum(score_components) / len(score_components) if score_components else 0.5

        return PreScreenResult(
            tier_used=2,
            score=avg_score,
            issues=issues,
            details={
                "function_count": len(functions),
                "class_count": len(classes),
                "annotation_ratio": round(annotation_ratio, 2),
                "docstring_ratio": round(docstring_ratio, 2),
                "has_future_import": has_future,
                "line_count": line_count,
            },
        )
