"""Vetinari Goal Verifier.

========================
Verifies that the final deliverable satisfies the original ProjectSpec.

This implements the output review → correction loop described in the
project plan:

1. Parse the original ProjectSpec (features, requirements, avoid list)
2. Run EvaluatorAgent to check compliance
3. Generate a verification matrix (Feature → Implemented? → Evidence)
4. SecurityAuditorAgent runs security check
5. If gaps found → create corrective tasks and re-enter execution
6. Present report to user and accept further changes

Usage:
    verifier = GoalVerifier()
    report = verifier.verify(project_spec, final_output, task_outputs)
    if not report.fully_compliant:
        corrective_tasks = report.get_corrective_tasks()
"""

from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any

# AgentType imported lazily to avoid circular imports at module level
from vetinari.types import AgentType
from vetinari.validation.goal_verifier_agents import llm_goal_evaluation, security_check

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class FeatureVerification:
    """Verification result for a single feature/requirement."""

    feature: str
    implemented: bool
    confidence: float  # 0.0 - 1.0
    evidence: str = ""
    location: str = ""  # File/section where found
    severity: str = "major"  # major|minor|info

    def __repr__(self) -> str:
        """Show key identifying fields for debugging."""
        return (
            f"FeatureVerification(feature={self.feature!r},"
            f" implemented={self.implemented!r}, severity={self.severity!r})"
        )


@dataclass
class GoalVerificationReport:
    """Complete verification report for a project delivery."""

    project_id: str
    goal: str
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    # Feature compliance matrix
    features: list[FeatureVerification] = field(default_factory=list)

    # Security
    security_passed: bool = False
    security_findings: list[dict[str, Any]] = field(default_factory=list)
    security_score: float = 0.0

    # Code quality
    quality_score: float = 0.0
    quality_issues: list[str] = field(default_factory=list)

    # Tests
    tests_present: bool = False
    test_coverage_estimate: float = 0.0

    # Overall
    compliance_score: float = 0.0
    fully_compliant: bool = False
    missing_features: list[str] = field(default_factory=list)
    corrective_suggestions: list[str] = field(default_factory=list)

    # Metadata
    evaluator_verdict: str = "inconclusive"
    model_used: str = ""

    def __repr__(self) -> str:
        """Show key identifying fields for debugging."""
        return (
            f"GoalVerificationReport(project_id={self.project_id!r},"
            f" fully_compliant={self.fully_compliant!r},"
            f" compliance_score={self.compliance_score!r})"
        )

    def get_corrective_tasks(self) -> list[dict[str, Any]]:
        """Generate corrective tasks for all identified gaps.

        Returns:
            List of task dicts ready for re-entry into the execution pipeline.
            Each dict has 'type', 'description', 'priority', and
            'assigned_agent'. Security findings and missing test tasks are
            included when relevant.
        """
        # Tasks for missing features — skip AVOID: prefixed items (they need removal, not implementation)
        tasks: list[dict[str, Any]] = []
        for feat in self.missing_features:
            if feat.upper().startswith("AVOID:"):
                # Avoid-list violation: emit a removal task, not an implementation task
                avoid_item = feat[len("AVOID:") :].strip()
                tasks.append({
                    "type": "remove_avoided_element",
                    "description": f"Remove prohibited element from deliverable: {avoid_item}",
                    "priority": "high",
                    "assigned_agent": AgentType.WORKER.value,
                })
            else:
                tasks.append({
                    "type": "implement_missing_feature",
                    "description": f"Implement missing feature: {feat}",
                    "priority": "high",
                    "assigned_agent": AgentType.WORKER.value,
                })

        # Task for security issues — Inspector reviews, Worker implements the fix
        if not self.security_passed and self.security_findings:
            tasks.append({
                "type": "fix_security_issues",
                "description": f"Fix {len(self.security_findings)} security findings",
                "priority": "critical",
                "assigned_agent": AgentType.INSPECTOR.value,
                "details": self.security_findings,
            })

        # Task for missing tests — prescribing test requirements is an Inspector role
        if not self.tests_present:
            tasks.append({
                "type": "add_tests",
                "description": "Add unit tests (none found in deliverable)",
                "priority": "high",
                "assigned_agent": AgentType.INSPECTOR.value,
            })

        # General quality improvement — Inspector prescribes quality improvements
        # Top 3 suggestions
        tasks.extend(
            {
                "type": "quality_improvement",
                "description": issue,
                "priority": "medium",
                "assigned_agent": AgentType.INSPECTOR.value,
            }
            for issue in self.corrective_suggestions[:3]
        )

        return tasks

    def to_dict(self) -> dict[str, Any]:
        """Serialize this GoalVerificationReport to a plain dictionary suitable for JSON output.

        Returns:
            Dictionary containing the project goal, feature compliance matrix,
            security findings, quality scores, and corrective suggestions.
        """
        return {
            "project_id": self.project_id,
            "goal": self.goal,
            "created_at": self.created_at,
            "features": [
                {
                    "feature": f.feature,
                    "implemented": f.implemented,
                    "confidence": f.confidence,
                    "evidence": f.evidence,
                    "location": f.location,
                    "severity": f.severity,
                }
                for f in self.features
            ],
            "security_passed": self.security_passed,
            "security_score": self.security_score,
            "security_findings": self.security_findings,
            "quality_score": self.quality_score,
            "quality_issues": self.quality_issues,
            "tests_present": self.tests_present,
            "test_coverage_estimate": self.test_coverage_estimate,
            "compliance_score": self.compliance_score,
            "fully_compliant": self.fully_compliant,
            "missing_features": self.missing_features,
            "corrective_suggestions": self.corrective_suggestions,
            "evaluator_verdict": self.evaluator_verdict,
            "model_used": self.model_used,
        }


class GoalVerifier:
    """Verifies final deliverables against the original project goal and spec."""

    def __init__(self, quality_threshold: float = 0.75):
        self._threshold = quality_threshold

    def verify(
        self,
        project_id: str,
        goal: str,
        final_output: str,
        required_features: list[str] | None = None,
        things_to_avoid: list[str] | None = None,
        task_outputs: list[dict[str, Any]] | None = None,
        expected_outputs: list[str] | None = None,
    ) -> GoalVerificationReport:
        """Verify a final deliverable against the original project spec.

        Args:
            project_id: The project identifier
            goal: The original goal statement
            final_output: The assembled final deliverable text
            required_features: List of required features from the intake form
            things_to_avoid: List of anti-requirements from the intake form
            task_outputs: List of individual task outputs
            expected_outputs: Expected output types (code, tests, docs, etc.)

        Returns:
            GoalVerificationReport with compliance matrix and corrective tasks
        """
        report = GoalVerificationReport(project_id=project_id, goal=goal)

        required_features = required_features or []
        things_to_avoid = things_to_avoid or []
        task_outputs = task_outputs or []

        # 1. Check required features
        report.features = self._verify_features(goal, required_features, final_output, task_outputs)

        self._record_avoid_violations(report, things_to_avoid, final_output)

        # 3. Check for tests
        report.tests_present = self._check_tests_present(final_output, task_outputs)

        # 4. Check expected output types
        if expected_outputs:
            for expected in expected_outputs:
                has_it = self._check_output_type(expected, final_output, task_outputs)
                if not has_it:
                    report.quality_issues.append(f"Expected output not delivered: {expected}")

        self._apply_llm_evaluation(report, goal, final_output, required_features, things_to_avoid)
        self._apply_security_check(report, final_output, task_outputs)
        self._calculate_overall_compliance(report, project_id)

        logger.info(
            f"Goal verification for {project_id}: "
            f"score={report.compliance_score:.2f}, "
            f"compliant={report.fully_compliant}, "
            f"missing={len(report.missing_features)}",
        )
        return report

    # ─── Private helpers ──────────────────────────────────────────────────────

    def _record_avoid_violations(
        self,
        report: GoalVerificationReport,
        things_to_avoid: list[str],
        final_output: str,
    ) -> None:
        for violation in self._check_avoid_list(things_to_avoid, final_output):
            report.features.append(
                FeatureVerification(
                    feature=f"AVOID: {violation}",
                    implemented=False,
                    confidence=0.8,
                    evidence=f"Found violation: {violation}",
                    severity="major",
                ),
            )

    def _apply_llm_evaluation(
        self,
        report: GoalVerificationReport,
        goal: str,
        final_output: str,
        required_features: list[str],
        things_to_avoid: list[str],
    ) -> None:
        try:
            llm_result = self._llm_evaluation(goal, final_output, required_features, things_to_avoid)
        except Exception as e:
            logger.warning(
                "LLM evaluation failed in goal verifier; defaulting quality_score to 0.3 "
                "(failing value) to avoid masking verification gaps: %s",
                e,
            )
            report.quality_score = 0.3
            return
        if not llm_result:
            return
        report.quality_score = llm_result.get("quality_score", 0.7)
        report.evaluator_verdict = llm_result.get("verdict", "pass")
        report.corrective_suggestions = llm_result.get("improvements", [])
        report.model_used = llm_result.get("model_used", "")
        self._merge_llm_feature_checks(report, llm_result.get("feature_checks", []))

    @staticmethod
    def _merge_llm_feature_checks(report: GoalVerificationReport, feature_checks: list[dict[str, Any]]) -> None:
        for feat_data in feature_checks:
            feat_name = feat_data.get("feature", "")
            existing_index = next((idx for idx, f in enumerate(report.features) if f.feature == feat_name), None)
            existing = report.features[existing_index] if existing_index is not None else None
            if existing:
                report.features[existing_index] = replace(
                    existing,
                    implemented=feat_data.get("implemented", existing.implemented),
                    confidence=max(existing.confidence, feat_data.get("confidence", 0.5)),
                    evidence=feat_data.get("evidence") or existing.evidence,
                )

    def _apply_security_check(
        self,
        report: GoalVerificationReport,
        final_output: str,
        task_outputs: list[dict[str, Any]],
    ) -> None:
        try:
            report.security_passed, report.security_findings, report.security_score = self._security_check(
                final_output,
                task_outputs,
            )
        except Exception as e:
            logger.warning("Security check failed; failing closed: %s", e)
            report.security_passed = False
            report.security_findings = [{"severity": "critical", "description": f"Security check error: {e}"}]
            report.security_score = 0.0

    def _calculate_overall_compliance(self, report: GoalVerificationReport, project_id: str) -> None:
        report.missing_features = [
            f.feature for f in report.features if not f.implemented and f.severity in ("major", "critical")
        ]
        evaluator_failed = report.evaluator_verdict.strip().lower() in {"fail", "failed", "noncompliant"}
        feature_score = self._feature_score(report, project_id)
        security_weight = 0.2 if not report.security_passed else 0.0
        tests_score = (1.0 if report.tests_present else 0.5) * 0.2
        report.compliance_score = max(
            0.0,
            feature_score * 0.5 + report.quality_score * 0.3 + tests_score - security_weight,
        )
        report.fully_compliant = (
            report.compliance_score >= self._threshold
            and report.security_passed
            and not report.missing_features
            and not evaluator_failed
        )

    @staticmethod
    def _feature_score(report: GoalVerificationReport, project_id: str) -> float:
        if report.features:
            return sum(f.confidence for f in report.features if f.implemented) / len(report.features)
        logger.warning("No features to verify for project %s; defaulting feature_score to 0.5", project_id)
        return 0.5

    # Negation phrases that indicate a feature is explicitly absent or incomplete.
    # A keyword match preceded by one of these phrases must NOT count as proof of delivery.
    _NEGATION_PATTERNS: tuple[str, ...] = (
        " not ",
        " no ",
        "isn't ",
        "isn't ",
        "is not ",
        "has not ",
        "hasn't ",
        "hasn't ",
        "have not ",
        "haven't ",
        "haven't ",
        "missing",
        "lacks ",
        "lack ",
        "absent",
        "without ",
        "unimplemented",
        "not implemented",
        "not yet",
        "todo",
        "fixme",
    )

    def _has_negation_context(self, keyword: str, combined_lower: str) -> bool:
        """Return True if *keyword* appears near a negation phrase.

        Scans a 60-character window before each occurrence of *keyword* to detect
        phrases like "is not implemented" or "authentication is missing".

        Args:
            keyword: The feature keyword to check.
            combined_lower: Lowercased combined output text.

        Returns:
            True when a negation phrase precedes an occurrence of *keyword*.
        """
        start = 0
        while True:
            idx = combined_lower.find(keyword, start)
            if idx == -1:
                break
            window = combined_lower[max(0, idx - 60) : idx + len(keyword)]
            if any(neg in window for neg in self._NEGATION_PATTERNS):
                return True
            start = idx + 1
        return False

    def _verify_features(
        self,
        goal: str,
        features: list[str],
        final_output: str,
        task_outputs: list[dict[str, Any]],
    ) -> list[FeatureVerification]:
        """Heuristically check if features appear in the output.

        Keyword matches that occur in a negation context (e.g. "authentication
        is not implemented") are excluded so that negated mentions do not
        inflate confidence scores.
        """
        verified = []
        combined_text = final_output + "\n" + "\n".join(str(t.get("output", "")) for t in task_outputs)
        combined_lower = combined_text.lower()

        for feature in features:
            if not feature.strip():
                continue
            feature_lower = feature.lower()
            # Extract keywords from feature description (skip short stop-words)
            keywords = [w for w in feature_lower.split() if len(w) > 3]
            # Count keyword hits, but exclude those that only appear in negation context
            matches = sum(
                1 for kw in keywords if kw in combined_lower and not self._has_negation_context(kw, combined_lower)
            )
            confidence = min(1.0, matches / max(len(keywords), 1))
            implemented = confidence >= 0.5

            verified.append(
                FeatureVerification(
                    feature=feature,
                    implemented=implemented,
                    confidence=confidence,
                    evidence=f"Found {matches}/{len(keywords)} keywords" if implemented else "Not found in output",
                    severity="major",
                ),
            )

        return verified

    @staticmethod
    def _check_avoid_list(avoid_list: list[str], final_output: str) -> list[str]:
        """Check if any 'avoid' items appear in the output."""
        violations = []
        output_lower = final_output.lower()
        for item in avoid_list:
            if not item.strip():
                continue
            if item.lower() in output_lower:
                violations.append(item)
        return violations

    @staticmethod
    def _check_tests_present(final_output: str, task_outputs: list[dict[str, Any]]) -> bool:
        """Check if test files or test code is present in a code context.

        Requires indicators to appear near other code patterns to avoid false
        positives from prose descriptions of tests (e.g. documentation that
        mentions "assert" or "testing.T" without actual test code).
        """
        combined = final_output + "\n" + "\n".join(str(t.get("output", "")) for t in task_outputs)
        # Unambiguous test-code indicators: these never appear in prose
        unambiguous = [
            "def test_",
            "import pytest",
            "import unittest",
            "@pytest",
            "func Test",  # Go test functions always start with "func Test"
            "testing.T",  # Go testing type — only appears in Go test code
        ]
        if any(ind in combined for ind in unambiguous):
            return True
        # Detect Python assert statements: "assert <expr>" where the next word is not
        # a prose conjunction (that, this, the) — distinguishes code from "I assert that..."
        if re.search(r"\bassert\s+(?!that\b|this\b|the\b)[^\s]", combined):
            return True
        # Ambiguous indicators require a code-context neighbour within 200 chars
        # to avoid matching generic test-adjacent words in prose.
        ambiguous = ["class Test", "describe(", "it(", "expect("]
        code_neighbours = ["def ", "import ", "class ", "{", "}", "=>", "function "]
        for indicator in ambiguous:
            idx = combined.find(indicator)
            while idx != -1:
                window = combined[max(0, idx - 200) : idx + len(indicator) + 200]
                if any(nb in window for nb in code_neighbours):
                    return True
                idx = combined.find(indicator, idx + 1)
        return False

    @staticmethod
    def _check_output_type(
        expected: str,
        final_output: str,
        task_outputs: list[dict[str, Any]],
    ) -> bool:
        """Check if an expected output type is present."""
        combined = final_output + "\n" + "\n".join(str(t.get("output", "")) for t in task_outputs)
        type_patterns = {
            "code": ["def ", "class ", "function ", "import ", "require("],
            "tests": ["def test_", "pytest", "unittest", "describe("],
            "docs": ["# ", "## ", "### ", "README", "docstring"],
            "ci": ["github/workflows", ".yml", "pipeline", "CI/CD"],
            "docker": ["FROM ", "Dockerfile", "docker-compose"],
            "assets": ["<svg", ".png", ".jpg", "image"],
        }
        patterns = type_patterns.get(expected.lower(), [expected.lower()])
        return any(p in combined for p in patterns)

    @staticmethod
    def _llm_evaluation(
        goal: str,
        final_output: str,
        required_features: list[str],
        things_to_avoid: list[str],
    ) -> dict[str, Any] | None:
        """Use EvaluatorAgent for LLM-powered goal compliance check."""
        return llm_goal_evaluation(goal, final_output, required_features, things_to_avoid)

    @staticmethod
    def _security_check(
        final_output: str,
        task_outputs: list[dict[str, Any]],
    ) -> tuple:
        """Run SecurityAuditorAgent on the output."""
        return security_check(final_output, task_outputs)


_goal_verifier: GoalVerifier | None = None
_goal_verifier_lock = threading.Lock()


def get_goal_verifier() -> GoalVerifier:
    """Return the process-global GoalVerifier, creating it on first call.

    Returns:
        The singleton GoalVerifier instance with default quality threshold.
    """
    global _goal_verifier
    if _goal_verifier is None:
        with _goal_verifier_lock:
            if _goal_verifier is None:
                _goal_verifier = GoalVerifier()
    return _goal_verifier
