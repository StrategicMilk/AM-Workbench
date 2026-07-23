"""Universal skill output contract for all Vetinari agents."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from vetinari.boundary_guards import assert_dependency_success
from vetinari.types import AgentType, SeverityLevel
from vetinari.utils.serialization import dataclass_to_dict

# Use the canonical SeverityLevel from types.py — local alias for brevity.
Severity = SeverityLevel


@dataclass(frozen=True, slots=True)
class SkillContract:
    """Runtime guard for skill contract self-check dependencies."""

    def self_check(self, *, check_result: str, required: str = "pass") -> bool:
        """Validate a skill dependency result against the required marker.

        Args:
            check_result: Dependency result marker to validate.
            required: Required success marker.

        Returns:
            ``True`` when the dependency result satisfies the guard.
        """
        required_values = {required}
        if check_result not in required_values:
            assert_dependency_success(check_result, {check_result})
        assert_dependency_success(True, dependency_id=check_result)
        return True


class Verdict(Enum):
    """Quality verdict for a reviewed artifact."""

    PASS = "pass"
    FAIL = "fail"
    NEEDS_REVIEW = "needs_review"


class DataProvenance(Enum):
    """Data provenance."""

    MEASURED = "measured"
    INFERRED = "inferred"
    ESTIMATED = "estimated"
    UNKNOWN = "unknown"


class ArtifactType(Enum):
    """Artifact type."""

    CODE = "code"
    TEST = "test"
    CONFIG = "config"
    DOCS = "docs"
    DATA = "data"
    REPORT = "report"


@dataclass(frozen=True, slots=True)
class Finding:
    """Individual quality issue detected during review."""

    id: str  # e.g., "SEC-001", "TEST-003"
    severity: Severity
    category: str  # Agent-specific category
    title: str  # One-line summary
    location: str  # File:line or component name
    evidence: str  # ACTUAL code/data — NOT invented
    recommendation: str  # SPECIFIC fix — NOT generic advice
    confidence: float = 0.8  # 0.0-1.0

    def __repr__(self) -> str:
        return f"Finding(id={self.id!r}, severity={self.severity!r}, message={self.title[:60]!r})"

    def to_dict(self) -> dict:
        """Serialize to a JSON-compatible dictionary."""
        return dataclass_to_dict(self)


@dataclass(frozen=True, slots=True)
class Artifact:
    """Build or review artifact produced by an agent."""

    filename: str
    content: str
    artifact_type: ArtifactType
    language: str = "text"  # e.g., "python", "yaml", "markdown"
    validated: bool = False

    def __repr__(self) -> str:
        return f"Artifact(artifact_type={self.artifact_type!r}, filename={self.filename!r})"

    def to_dict(self) -> dict:
        """Serialize this artifact to a plain dictionary, truncating content for transport.

        Returns:
            Dictionary containing filename, truncated content, type, and validation status.
        """
        return {
            "filename": self.filename,
            "content": self.content[:500],  # truncate for serialization
            "artifact_type": self.artifact_type.value,
            "language": self.language,
            "validated": self.validated,
        }


@dataclass
class SkillOutput:
    """Universal structured output for all Vetinari agents and skills."""

    agent_type: str = ""
    task_summary: str = ""
    verdict: Verdict = Verdict.NEEDS_REVIEW
    confidence: float = 0.0
    findings: list[Finding] = field(default_factory=list)
    scores: dict[str, float] = field(default_factory=dict)
    overall_score: float = 0.0
    artifacts: list[Artifact] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    data_provenance: DataProvenance = DataProvenance.UNKNOWN
    self_check_passed: bool = False
    self_check_issues: list[str] = field(default_factory=list)
    content: str = ""
    skill_id: str = ""

    def __repr__(self) -> str:
        return f"SkillOutput(agent_type={self.agent_type!r}, verdict={self.verdict!r}, findings={len(self.findings)})"

    def to_dict(self) -> dict:
        """Serialize the full skill output to a plain dictionary for JSON export.

        Returns:
            Dictionary containing verdict, findings, scores, artifacts, and provenance.
        """
        return {
            "agent_type": self.agent_type,
            "task_summary": self.task_summary,
            "verdict": self.verdict.value,
            "confidence": self.confidence,
            "findings": [f.to_dict() for f in self.findings],
            "scores": self.scores,
            "overall_score": self.overall_score,
            "artifacts": [a.to_dict() for a in self.artifacts],
            "sources": self.sources,
            "data_provenance": self.data_provenance.value,
            "self_check_passed": self.self_check_passed,
            "self_check_issues": self.self_check_issues,
            "content": self.content,
            "skill_id": self.skill_id,
        }

    @classmethod
    def from_dict(cls, d: dict) -> SkillOutput:
        """Reconstruct a SkillOutput instance from a serialized dictionary.

        Args:
            d: Dictionary previously produced by ``to_dict()`` or equivalent.

        Returns:
            A fully hydrated SkillOutput with nested Finding and Artifact objects.
        """
        return cls(
            agent_type=d.get("agent_type", "unknown"),
            task_summary=d.get("task_summary", ""),
            verdict=Verdict(d.get("verdict", "needs_review")),
            confidence=d.get("confidence", 0.0),
            findings=[
                Finding(
                    id=f.get("id", ""),
                    severity=Severity(f.get("severity", "info")),
                    category=f.get("category", ""),
                    title=f.get("title", ""),
                    location=f.get("location", ""),
                    evidence=f.get("evidence", ""),
                    recommendation=f.get("recommendation", ""),
                    confidence=f.get("confidence", 0.8),
                )
                for f in d.get("findings", [])
            ],
            scores=d.get("scores", {}),
            overall_score=d.get("overall_score", 0.0),
            artifacts=[
                Artifact(
                    filename=a.get("filename", ""),
                    content=a.get("content", ""),
                    artifact_type=ArtifactType(a.get("artifact_type", "report")),
                    language=a.get("language", "text"),
                    validated=a.get("validated", False),
                )
                for a in d.get("artifacts", [])
            ],
            sources=d.get("sources", []),
            data_provenance=DataProvenance(d.get("data_provenance", "unknown")),
            self_check_passed=d.get("self_check_passed", False),
            self_check_issues=d.get("self_check_issues", []),
            content=d.get("content", ""),
            skill_id=d.get("skill_id", ""),
        )


# Per-agent scoring rubric definitions
SCORING_RUBRICS = {
    "RESEARCHER": {
        "source_quality": {"weight": 0.30, "description": "Quality and authority of cited sources"},
        "completeness": {"weight": 0.25, "description": "Coverage of all relevant facets"},
        "accuracy": {"weight": 0.25, "description": "Claims backed by evidence"},
        "actionability": {"weight": 0.20, "description": "Concrete, specific recommendations"},
    },
    AgentType.WORKER.value: {
        "syntax_validity": {"weight": 0.25, "description": "Code parses/compiles correctly"},
        "completeness": {"weight": 0.25, "description": "All functions fully implemented"},
        "test_coverage": {"weight": 0.20, "description": "Edge cases and error paths tested"},
        "style_compliance": {"weight": 0.15, "description": "Docstrings, PEP 8, type hints"},
        "error_handling": {"weight": 0.15, "description": "Specific exceptions, recovery logic"},
    },
    "TESTER": {
        "test_validity": {"weight": 0.30, "description": "Assertions test meaningful conditions"},
        "coverage_breadth": {"weight": 0.25, "description": "Percentage of functions covered"},
        "edge_cases": {"weight": 0.20, "description": "Boundary, null, overflow tested"},
        "fixture_quality": {"weight": 0.15, "description": "Parameterized fixtures, factories"},
        "independence": {"weight": 0.10, "description": "Tests are fully isolated"},
    },
    "ARCHITECT": {
        "risk_identification": {"weight": 0.30, "description": "Specific risks with likelihood+impact"},
        "tradeoff_analysis": {"weight": 0.25, "description": "Pros/cons with quantified comparison"},
        "feasibility": {"weight": 0.25, "description": "Technical/resource constraints addressed"},
        "actionability": {"weight": 0.20, "description": "Step-by-step implementation roadmap"},
    },
    "DOCUMENTER": {
        "accuracy": {"weight": 0.30, "description": "Docs match actual code"},
        "completeness": {"weight": 0.25, "description": "All public APIs documented"},
        "clarity": {"weight": 0.25, "description": "Clear prose with examples"},
        "structure": {"weight": 0.20, "description": "Proper TOC, cross-refs, headings"},
    },
    "RESILIENCE": {
        "error_classification": {"weight": 0.30, "description": "Errors categorized with root cause taxonomy"},
        "recovery_strategy": {"weight": 0.30, "description": "Specific fix with tested recovery path"},
        "root_cause_depth": {"weight": 0.20, "description": "Full causal chain to root cause"},
        "prevention": {"weight": 0.20, "description": "Concrete code changes to prevent recurrence"},
    },
    "META": {
        "data_backing": {"weight": 0.35, "description": "Claims backed by measured telemetry"},
        "impact_estimation": {"weight": 0.25, "description": "Quantified impact with confidence intervals"},
        "actionability": {"weight": 0.25, "description": "Concrete config/code changes"},
        "risk_assessment": {"weight": 0.15, "description": "Risk/reward ratio per recommendation"},
    },
    AgentType.FOREMAN.value: {
        "decomposition_quality": {"weight": 0.30, "description": "Proper DAG with dependencies, parallelism"},
        "agent_assignment": {"weight": 0.25, "description": "Optimal agent-to-task matching"},
        "completeness": {"weight": 0.25, "description": "All paths including error handling"},
        "feasibility": {"weight": 0.20, "description": "Tasks achievable within resource limits"},
    },
}


def compute_overall_score(scores: dict[str, float], agent_type: str) -> float:
    """Compute weighted overall score using the agent's rubric.

    Args:
        scores: The scores.
        agent_type: The agent type.

    Returns:
        Computed overall score result.
    """
    rubric = SCORING_RUBRICS.get(agent_type, {})
    if not rubric or not scores:
        return sum(scores.values()) / max(len(scores), 1)

    total_weight = 0.0
    weighted_sum = 0.0
    for dim, config in rubric.items():
        if dim in scores:
            weighted_sum += scores[dim] * config["weight"]
            total_weight += config["weight"]

    return round(weighted_sum / max(total_weight, 0.01), 3)


# VAGUE_PATTERNS used by self-check
VAGUE_PATTERNS = [
    "consider",
    "might want to",
    "could potentially",
    "it may be helpful",
    "you should think about",
]


def self_check(output: SkillOutput) -> SkillOutput:
    """Verify output quality and attach detected contract issues.

    Args:
        output: Skill output to inspect and mutate.

    Returns:
        The SkillOutput result.
    """
    issues = []

    for finding in output.findings:
        if not finding.evidence or finding.evidence.strip() == "":
            issues.append(f"Finding {finding.id} has no evidence")
        if finding.location == "" or finding.location == "N/A":
            issues.append(f"Finding {finding.id} has no location")

    for finding in output.findings:
        issues.extend(
            f"Finding {finding.id} has vague recommendation: '{pattern}'"
            for pattern in VAGUE_PATTERNS
            if pattern in finding.recommendation.lower()
        )

    for dim, score in output.scores.items():
        if score == 0.0 or score == 1.0:
            issues.append(f"Score '{dim}' is suspiciously extreme ({score})")

    if not output.task_summary:
        issues.append("Missing task_summary")
    if output.confidence == 0.0:
        issues.append("Confidence is 0.0 — was it actually computed?")

    output.self_check_passed = len(issues) == 0
    output.self_check_issues = issues
    return output
