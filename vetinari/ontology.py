"""Ontology Epicenter — canonical vocabulary and quality types for Vetinari.

THE single source of truth for agent communication. All modules that deal
with quality assessment, gate decisions, or success signals MUST import
their types from here. This resolves:
- Quality score semantic drift (3 systems interpret 0.0-1.0 differently)
- Vocabulary inconsistency (5 names for quality, 2 incompatible issue types)
- Missing canonical work product term (ARTIFACT)

Pipeline role: Cross-cutting foundation — used by Inspector, Quality Scorer,
Thompson Sampling, and Feedback Loop.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum

logger = logging.getLogger(__name__)


# -- Canonical Vocabulary --
# ONE term for each concept. Other modules MUST use these, not synonyms.


class CanonicalTerm(str, Enum):
    """Single approved name for each domain concept.

    Prevents vocabulary drift where the same thing gets called
    'output', 'result', 'deliverable', 'response' in different modules.
    """

    ARTIFACT = "artifact"  # Work product from any agent (not "output", "result", "deliverable")
    ASSESSMENT = "assessment"  # Quality evaluation (not "score", "rating", "grade" alone)
    DECISION = "decision"  # Gate pass/fail outcome (not "verdict", "judgment", "ruling")
    SIGNAL = "signal"  # Binary success for Thompson Sampling (not "reward", "outcome")
    DEFECT = "defect"  # Problem found by Inspector (not "issue", "finding", "problem", "error")
    DIRECTIVE = "directive"  # Instruction to an agent (not "command", "request", "task")
    SPECIFICATION = "specification"  # What an agent should produce (not "requirement", "expectation")


# -- Defect Severity --


class DefectSeverity(str, Enum):
    """Severity levels for defects found during inspection.

    Each level has a defined scoring penalty used in calculate_quality_score().
    """

    CRITICAL = "critical"  # Blocks acceptance: security vuln, data loss, crash (penalty: 0.30)
    HIGH = "high"  # Major functionality broken or missing (penalty: 0.15)
    MEDIUM = "medium"  # Minor functionality issue or significant style problem (penalty: 0.05)
    LOW = "low"  # Cosmetic, style nit, minor improvement suggestion (penalty: 0.02)


# -- Quality Threshold --

QUALITY_THRESHOLD_PASS = 0.7  # Minimum score to pass a quality gate or mark output as acceptable


# -- Quality Scale --


class QualityLevel(str, Enum):
    """Human-readable quality levels derived from numeric scores.

    Distinct from QualityGrade in vetinari.types (which uses letter grades A/B/C/D/F).
    QualityLevel uses descriptive labels tied to defined thresholds:
    - EXCELLENT: >=0.90 — production-ready, no significant defects
    - GOOD: >=0.75 — acceptable with minor improvements possible
    - ADEQUATE: >=0.60 — functional but needs work
    - POOR: >=0.40 — significant defects, may need rework
    - FAILING: <0.40 — fundamental problems, reject
    """

    EXCELLENT = "excellent"
    GOOD = "good"
    ADEQUATE = "adequate"
    POOR = "poor"
    FAILING = "failing"


def score_to_level(score: float) -> QualityLevel:
    """Convert a 0.0-1.0 quality score to a human-readable level.

    Args:
        score: Quality score between 0.0 and 1.0 inclusive.

    Returns:
        Corresponding QualityLevel enum value.
    """
    if score >= 0.90:
        return QualityLevel.EXCELLENT
    if score >= 0.75:
        return QualityLevel.GOOD
    if score >= 0.60:
        return QualityLevel.ADEQUATE
    if score >= 0.40:
        return QualityLevel.POOR
    return QualityLevel.FAILING


# -- Quality Assessment (what Quality Scorer produces) --


@dataclass(frozen=True, slots=True)
class QualityDimension:
    """A single dimension of quality measurement.

    Args:
        name: Dimension name (e.g., 'correctness', 'completeness').
        score: 0.0-1.0 where 0.0 means unmeasured and 1.0 means perfect.
        measured: Whether this dimension was actually evaluated (vs default).
        method: How it was measured ('llm', 'heuristic', 'hybrid', 'unmeasured').
    """

    name: str
    score: float
    measured: bool = False
    method: str = "unmeasured"

    def __repr__(self) -> str:
        return (
            f"QualityDimension(name={self.name!r}, score={self.score:.2f}, "
            f"measured={self.measured}, method={self.method!r})"
        )


@dataclass(frozen=True, slots=True)
class QualityAssessment:
    """Multidimensional quality evaluation — what the Quality Scorer produces.

    This is the FULL assessment with per-dimension scores. It captures
    correctness, completeness, efficiency, style, and security independently.
    Different from GateDecision (pass/fail) and SuccessSignal (binary for Thompson).

    Args:
        artifact_id: ID of the artifact being assessed.
        model_id: Model that produced the artifact.
        task_type: Type of task that produced the artifact.
        overall_score: Weighted aggregate score (0.0-1.0).
        dimensions: Individual quality dimensions with per-dimension scores.
        defects: List of defect descriptions found.
        method: Scoring method used ('llm', 'heuristic', 'hybrid').
    """

    artifact_id: str
    model_id: str
    task_type: str
    overall_score: float
    dimensions: tuple[QualityDimension, ...] = ()
    defects: tuple[str, ...] = ()
    method: str = "heuristic"

    @property
    def level(self) -> QualityLevel:
        """Derive human-readable level from overall score."""
        return score_to_level(self.overall_score)

    @property
    def measured_dimensions(self) -> tuple[QualityDimension, ...]:
        """Return only dimensions that were actually measured."""
        return tuple(d for d in self.dimensions if d.measured)

    def __repr__(self) -> str:
        """Show key identifying fields for debugging."""
        return (
            f"QualityAssessment(artifact={self.artifact_id!r}, "
            f"score={self.overall_score:.2f}, level={self.level.value!r})"
        )


# -- Gate Decision (what Inspector produces) --


@dataclass(frozen=True, slots=True)
class GateDecision:
    """Inspector's pass/fail decision on an artifact.

    A gate decision combines a quality score with an explicit pass/fail
    and a human-readable level. This is what the Inspector produces after
    reviewing a Worker's output.

    Args:
        artifact_id: ID of the artifact being judged.
        passed: Whether the artifact passes the quality gate.
        score: Overall quality score (0.0-1.0).
        level: Human-readable quality level.
        defect_counts: Number of defects by severity (keys are DefectSeverity values).
        rationale: Why the decision was made.
    """

    artifact_id: str
    passed: bool
    score: float
    level: QualityLevel
    defect_counts: dict[str, int] = field(default_factory=dict)
    rationale: str = ""

    def __repr__(self) -> str:
        """Show key identifying fields for debugging."""
        return (
            f"GateDecision(artifact={self.artifact_id!r}, "
            f"passed={self.passed}, score={self.score:.2f}, "
            f"level={self.level.value!r})"
        )


# -- Success Signal (derived for Thompson Sampling) --


@dataclass(frozen=True, slots=True)
class SuccessSignal:
    """Binary success signal for Thompson Sampling bandit updates.

    Derived from GateDecision with an explicit conversion — never created
    directly from raw scores. The quality_weight lets Thompson Sampling
    distinguish between "barely passed" and "excellent" successes.

    Args:
        model_id: Model being evaluated.
        task_type: Task type context.
        success: Binary outcome (True=passed gate, False=failed).
        quality_weight: 0.0-1.0 weight from the gate score (for weighted updates).
    """

    model_id: str
    task_type: str
    success: bool
    quality_weight: float

    @classmethod
    def from_quality_score(
        cls,
        quality_score: float,
        success: bool,
        model_id: str = "",
        task_type: str = "",
    ) -> SuccessSignal:
        """Create a SuccessSignal from a raw quality score and success flag.

        Clamps quality_score to [0.0, 1.0] so that out-of-range values from
        different scoring systems do not corrupt bandit alpha/beta counts.

        Args:
            quality_score: Raw quality score from any scorer (clamped to [0, 1]).
            success: Whether the task passed its quality gate.
            model_id: Model that produced the artifact.
            task_type: Task type context for arm selection.

        Returns:
            SuccessSignal ready for ``BetaArm.update_from_signal()``.
        """
        return cls(
            model_id=model_id,
            task_type=task_type,
            success=success,
            quality_weight=max(0.0, min(1.0, float(quality_score))),
        )

    def __repr__(self) -> str:
        """Show key identifying fields for debugging."""
        return f"SuccessSignal(model={self.model_id!r}, success={self.success}, weight={self.quality_weight:.2f})"


def gate_to_signal(decision: GateDecision, model_id: str, task_type: str) -> SuccessSignal:
    """Convert a GateDecision to a SuccessSignal for Thompson Sampling.

    This is the ONLY approved way to create SuccessSignals. Direct construction
    from raw scores bypasses the quality gate and produces unreliable bandit data.

    Args:
        decision: Inspector's gate decision.
        model_id: Model that produced the artifact.
        task_type: Task type for bandit arm selection.

    Returns:
        SuccessSignal with binary outcome and quality weight.
    """
    return SuccessSignal.from_quality_score(
        quality_score=decision.score,
        success=decision.passed,
        model_id=model_id,
        task_type=task_type,
    )


# -- Scoring Formula --


def calculate_quality_score(
    critical: int = 0,
    high: int = 0,
    medium: int = 0,
    low: int = 0,
) -> float:
    """Calculate quality score from defect counts by severity.

    THE canonical scoring formula for Vetinari. Previously inlined in
    inspector_skill.py — now lives here as the single source of truth.

    Formula: score = max(0.0, 1.0 - (critical*0.3 + high*0.15 + medium*0.05 + low*0.02))

    A single critical defect drops the score to 0.70 at most. Two criticals
    drop to 0.40. This ensures critical defects always result in gate failure
    (threshold is typically 0.75).

    Args:
        critical: Number of critical severity defects.
        high: Number of high severity defects.
        medium: Number of medium severity defects.
        low: Number of low severity defects.

    Returns:
        Quality score between 0.0 and 1.0.
    """
    penalty = critical * 0.30 + high * 0.15 + medium * 0.05 + low * 0.02
    return max(0.0, 1.0 - penalty)


# -- Relationship Types --


class RelationshipType(str, Enum):
    """Types of relationships between artifacts, decisions, and knowledge entries.

    Used by the knowledge graph and context system to connect related items.
    """

    SUPERSEDES = "supersedes"  # This artifact replaces an older one
    CONTRADICTS = "contradicts"  # This finding conflicts with another
    CAUSED_BY = "caused_by"  # This defect was caused by another artifact/decision
    ELABORATES = "elaborates"  # This adds detail to a parent item
    PART_OF = "part_of"  # This is a component of a larger artifact


# -- Temporal Status --


class TemporalStatus(str, Enum):
    """Lifecycle status for knowledge entries and tracked items.

    Distinct from StatusEnum (task lifecycle) — this tracks the temporal
    validity of knowledge, decisions, and plans.
    """

    ACTIVE = "active"  # Currently valid and in use
    COMPLETED = "completed"  # Finished, historically relevant
    PLANNED = "planned"  # Scheduled for future
    ABANDONED = "abandoned"  # Intentionally stopped
    RECURRING = "recurring"  # Repeats on a schedule (e.g., weekly kaizen)


# -- Agent Capabilities --
# Keys correspond to AgentType.value strings from vetinari.types.

AGENT_CAPABILITIES: dict[str, dict[str, list[str]]] = {
    "FOREMAN": {
        "can": [
            "decompose_goals",
            "create_execution_plans",
            "assign_tasks_to_workers",
            "monitor_progress",
            "handle_escalations",
            "replan_on_failure",
        ],
        "cannot": [
            "execute_tasks_directly",
            "assess_quality",
            "make_security_decisions",
            "modify_model_selection",
        ],
    },
    "WORKER": {
        "can": [
            "execute_assigned_tasks",
            "generate_artifacts",
            "report_progress",
            "request_clarification",
            "use_tools",
        ],
        "cannot": [
            "self_assess_quality",
            "skip_assigned_tasks",
            "modify_plan",
            "override_inspector",
        ],
    },
    "INSPECTOR": {
        "can": [
            "assess_artifact_quality",
            "produce_gate_decisions",
            "identify_defects",
            "recommend_rework",
            "run_security_checks",
        ],
        "cannot": [
            "modify_artifacts",
            "execute_tasks",
            "override_foreman_plans",
            "approve_own_work",
        ],
    },
}


# -- Task-Type Quality Dimensions --
# Which dimensions are relevant per task type (for Quality Scorer alignment).

TASK_QUALITY_DIMENSIONS: dict[str, tuple[str, ...]] = {
    "coding": ("correctness", "completeness", "efficiency", "style", "test_coverage"),
    "research": ("accuracy", "completeness", "source_quality", "actionability"),
    "analysis": ("depth", "accuracy", "actionability", "clarity"),
    "documentation": ("clarity", "completeness", "accuracy", "examples"),
    "testing": ("coverage", "correctness", "clarity", "edge_cases"),
    "creative": ("coherence", "originality", "style", "completeness"),
    "classification": ("accuracy", "precision", "recall", "confidence"),
    "general": ("correctness", "completeness", "clarity", "relevance"),
}
