"""Verification and post-execution system for Vetinari."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from vetinari.boundary_guards import assert_dependency_success
from vetinari.validation.verification_pipeline import (
    CodeSyntaxVerifier,
    ImportVerifier,
    JSONStructureVerifier,
    SecurityVerifier,
    VerificationPipeline,
    Verifier,
)
from vetinari.validation.verification_types import (
    ValidationVerificationResult,
    VerificationIssue,
    VerificationLevel,
    VerificationStatus,
)

logger = logging.getLogger(__name__)

_ScoreConfidence = Callable[[str, str], float | None]


def score_confidence_via_llm(task_description: str, content: str) -> float | None:
    """Score confidence with the optional LLM helper, warning when unavailable.

    Args:
        task_description: Description of the task being verified.
        content: Candidate content to score.

    Returns:
        Confidence score from the optional helper, or ``None`` when unavailable.
    """
    try:
        from vetinari.llm_helpers import score_confidence_via_llm as scorer
    except ImportError:
        logger.warning("llm_helpers unavailable -- LLM scoring disabled")
        return None
    assert_dependency_success(True, dependency_id="llm_helpers.score_confidence_via_llm")
    return scorer(task_description, content)


@dataclass
class CascadeVerdict:
    """Aggregated result from the three-tier verification cascade."""

    passed: bool
    tier_reached: str
    static_findings: list[str] = field(default_factory=list)
    entailment_coverage: float | None = None
    llm_score: float | None = None

    def __repr__(self) -> str:
        """Show key fields for debugging."""
        return (
            f"CascadeVerdict(passed={self.passed!r}, tier_reached={self.tier_reached!r}, "
            f"entailment_coverage={self.entailment_coverage!r})"
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize to plain dictionary."""
        return {
            "passed": self.passed,
            "tier_reached": self.tier_reached,
            "static_findings": self.static_findings,
            "entailment_coverage": self.entailment_coverage,
            "llm_score": self.llm_score,
        }


@dataclass(frozen=True, slots=True)
class CascadeThresholds:
    """Calibrated thresholds for the verification cascade."""

    min_entailment_coverage: float = 0.2
    llm_pass_threshold: float = 0.5
    fallback_coverage_threshold: float = 0.4

    def __post_init__(self) -> None:
        for name in ("min_entailment_coverage", "llm_pass_threshold", "fallback_coverage_threshold"):
            value = getattr(self, name)
            if value < 0.0 or value > 1.0:
                raise ValueError(f"{name} must be in [0.0, 1.0]")


class CascadeOrchestrator:
    """Three-tier verification cascade that minimizes LLM calls."""

    def __init__(self, thresholds: CascadeThresholds | None = None) -> None:
        from vetinari.validation.entailment_checker import EntailmentChecker
        from vetinari.validation.static_verifier import StaticVerifier

        self._static = StaticVerifier()
        self._entailment = EntailmentChecker()
        self._thresholds = thresholds or CascadeThresholds()

    def verify(self, content: str, task_description: str = "") -> CascadeVerdict:
        """Run the three-tier cascade on content.

        Args:
            content: Output text or code to verify.
            task_description: Task or requirement the output should satisfy.

        Returns:
            CascadeVerdict with the final tier and evidence fields.
        """
        static_results = self._static.verify(content, task_description)
        static_findings = [r.finding for r in static_results if not r.passed and r.finding]

        if static_findings:
            logger.info(
                "CascadeOrchestrator: Tier 1 FAILED (%d findings) - short-circuiting",
                len(static_findings),
            )
            return CascadeVerdict(
                passed=False,
                tier_reached="static",
                static_findings=static_findings,
            )

        entailment_result = self._entailment.check(task_description, content)
        logger.debug(
            "CascadeOrchestrator: Tier 2 coverage=%.3f entailed=%s",
            entailment_result.coverage,
            entailment_result.entailed,
        )

        if entailment_result.entailed:
            return CascadeVerdict(
                passed=True,
                tier_reached="entailment",
                static_findings=[],
                entailment_coverage=entailment_result.coverage,
            )

        if entailment_result.coverage < self._thresholds.min_entailment_coverage:
            logger.info(
                "CascadeOrchestrator: Tier 2 coverage %.3f too low - rejecting without LLM",
                entailment_result.coverage,
            )
            return CascadeVerdict(
                passed=False,
                tier_reached="entailment",
                static_findings=[],
                entailment_coverage=entailment_result.coverage,
            )

        llm_score: float | None = None
        try:
            llm_score = score_confidence_via_llm(task_description, content)
        except Exception as exc:
            logger.warning(
                "CascadeOrchestrator: Tier 3 LLM unavailable (%s); using entailment coverage as fallback",
                exc,
            )

        tier_reached = "llm"
        if llm_score is not None:
            passed = llm_score >= self._thresholds.llm_pass_threshold
        else:
            passed = entailment_result.coverage >= self._thresholds.fallback_coverage_threshold
            tier_reached = "entailment"

        logger.info(
            "CascadeOrchestrator: Tier 3 llm_score=%s passed=%s",
            llm_score,
            passed,
        )
        return CascadeVerdict(
            passed=passed,
            tier_reached=tier_reached,
            static_findings=[],
            entailment_coverage=entailment_result.coverage,
            llm_score=llm_score,
        )


_cascade_orchestrator: CascadeOrchestrator | None = None
_cascade_orchestrator_lock = threading.Lock()


def get_cascade_orchestrator() -> CascadeOrchestrator:
    """Return the process-wide CascadeOrchestrator singleton.

    Returns:
        Shared CascadeOrchestrator instance.
    """
    global _cascade_orchestrator
    if _cascade_orchestrator is None:
        with _cascade_orchestrator_lock:
            if _cascade_orchestrator is None:
                _cascade_orchestrator = CascadeOrchestrator()
    return _cascade_orchestrator


_verifier_pipeline: VerificationPipeline | None = None
_verifier_pipeline_lock = threading.Lock()


def get_verifier_pipeline() -> VerificationPipeline:
    """Get or create the global verification pipeline.

    Returns:
        Shared standard VerificationPipeline instance.
    """
    global _verifier_pipeline
    if _verifier_pipeline is None:
        with _verifier_pipeline_lock:
            if _verifier_pipeline is None:
                _verifier_pipeline = VerificationPipeline(VerificationLevel.STANDARD)
    return _verifier_pipeline


def __getattr__(name: str) -> Any:
    """Lazily preserve compatibility for moved verification helpers."""
    if name == "QualityGateVerifier":
        from vetinari.validation.verification_quality_gate import QualityGateVerifier

        return QualityGateVerifier
    if name == "Validator":
        from vetinari.validation.output_validator import Validator

        return Validator
    raise AttributeError(name)


__all__ = [
    "CascadeOrchestrator",
    "CascadeThresholds",
    "CascadeVerdict",
    "CodeSyntaxVerifier",
    "ImportVerifier",
    "JSONStructureVerifier",
    "SecurityVerifier",
    "ValidationVerificationResult",
    "VerificationIssue",
    "VerificationLevel",
    "VerificationPipeline",
    "VerificationStatus",
    "Verifier",
    "get_cascade_orchestrator",
    "get_verifier_pipeline",
    "score_confidence_via_llm",
]
