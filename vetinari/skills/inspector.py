"""Inspector skill compatibility helper."""

from __future__ import annotations

from dataclasses import dataclass

from vetinari.boundary_guards import require_nonempty

_MIN_SUBSTANTIVE_CONTENT_CHARS = 20
_LOW_EVIDENCE_MARKERS = frozenset({
    "ok",
    "pass",
    "passes",
    "looks good",
    "lgtm",
    "approved",
    "done",
})
_EVIDENCE_TERMS = frozenset({
    "evidence",
    "test",
    "tests",
    "pytest",
    "validation",
    "verified",
    "probe",
    "assert",
    "passed",
    "failed",
    "diff",
    "source",
})


@dataclass(frozen=True, slots=True)
class InspectorVerificationResult:
    """Inspector verification result."""

    passed: bool
    score: float
    reason: str = ""


class InspectorSkill:
    """Minimal inspector skill facade."""

    @staticmethod
    def _failure(score: float, reason: str) -> InspectorVerificationResult:
        return InspectorVerificationResult(
            passed=False,
            score=score,
            reason=require_nonempty(reason, field_name="reason"),
        )

    def verify(self, content: str) -> InspectorVerificationResult:
        """Verify content quality.

        Args:
            content: Content to verify.

        Returns:
            Verification result.
        """
        normalized = " ".join(content.split()) if isinstance(content, str) else ""
        if not normalized:
            return self._failure(0.0, "content is empty")
        if normalized.lower() in _LOW_EVIDENCE_MARKERS:
            return self._failure(0.1, "content is a low-evidence approval marker")
        if len(normalized) < _MIN_SUBSTANTIVE_CONTENT_CHARS:
            return self._failure(0.25, "content is below the substantive length threshold")
        if not any(char.isalpha() for char in normalized):
            return self._failure(0.0, "content contains no alphabetic evidence")
        lowered_words = {word.strip(".,:;()[]{}").lower() for word in normalized.split()}
        if not (lowered_words & _EVIDENCE_TERMS):
            return self._failure(0.45, "content does not mention evidence or validation")
        return InspectorVerificationResult(passed=True, score=0.8, reason="verification evidence present")


__all__ = ["InspectorSkill", "InspectorVerificationResult"]
