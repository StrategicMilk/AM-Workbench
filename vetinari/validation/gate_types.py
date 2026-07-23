"""Shared data types for the quality gate subsystem.

Contains enum and dataclass definitions used by both ``quality_gates.py``
(the runner and threshold data) and ``gate_checks.py`` (the check
implementations). Extracted to a separate module to avoid circular imports.

Public names:
- ``VerificationMode`` — modes for the TESTER agent
- ``GateResult`` — gate outcome (PASSED / FAILED / WARNING)
- ``QualityGateConfig`` — frozen config for a single gate
- ``GateCheckResult`` — result produced by one gate check
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from vetinari.utils.serialization import dataclass_to_dict


class VerificationMode(Enum):
    """Modes for the TESTER agent verification.

    Each mode represents a different verification strategy that the
    TESTER (Evaluator) agent can operate in during quality gate checks.
    """

    VERIFY_QUALITY = "verify_quality"  # Style, complexity, best practices
    SECURITY = "security"  # Security checks
    VERIFY_COVERAGE = "verify_coverage"  # Test existence and pass rate
    VERIFY_ARCHITECTURE = "verify_architecture"  # Consistency with project arch
    PRE_EXECUTION = "pre_execution"  # Poka-Yoke prevention checks before Builder runs


class GateResult(Enum):
    """Outcome of a quality gate check."""

    PASSED = "passed"
    FAILED = "failed"
    WARNING = "warning"


@dataclass(frozen=True, slots=True)
class QualityGateConfig:
    """Configuration for a quality gate.

    Attributes:
        name: Human-readable gate name.
        mode: The VerificationMode to use for this gate.
        required: If True, failure blocks the pipeline.
        min_score: Minimum score to pass (0.0-1.0).
        timeout_seconds: Maximum time allowed for the check.
        auto_fix: If True, attempt auto-remediation on failure.
    """

    name: str
    mode: VerificationMode
    required: bool = True
    min_score: float = 0.6
    timeout_seconds: int = 60
    auto_fix: bool = False

    def __repr__(self) -> str:
        """Show key identifying fields for debugging."""
        return f"QualityGateConfig(name={self.name!r}, mode={self.mode!r}, required={self.required!r})"


@dataclass
class GateCheckResult:
    """Result of a quality gate check.

    Attributes:
        gate_name: Name of the gate that produced this result.
        mode: The verification mode used.
        result: PASSED, FAILED, or WARNING.
        score: Numeric score (0.0-1.0).
        issues: List of issue dictionaries found during the check.
        suggestions: List of improvement suggestions.
        metadata: Additional metadata about the check.
    """

    gate_name: str
    mode: VerificationMode
    result: GateResult
    score: float
    issues: list[dict[str, Any]] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __repr__(self) -> str:
        """Show key identifying fields for debugging."""
        return f"GateCheckResult(gate_name={self.gate_name!r}, result={self.result!r}, score={self.score!r})"

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-compatible dictionary."""
        return dataclass_to_dict(self)
