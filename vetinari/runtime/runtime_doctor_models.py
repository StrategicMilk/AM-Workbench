"""Value objects for runtime doctor checks."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True, slots=True)
class RuntimeCheckResult:
    """Outcome of a single matrix-row check.

    Attributes:
        component: Matrix row component name.
        passed: True when the detected runtime satisfies the row.
        detected_version: Version string discovered at runtime, or None.
        reason: Human-readable explanation of the outcome.
        matrix_sources: URLs from the matrix row used to justify values.
        is_blocker: When True, a failed check fails the overall report.
    """

    component: str
    passed: bool
    detected_version: str | None
    reason: str
    matrix_sources: tuple[str, ...] = ()
    is_blocker: bool = True

    def __repr__(self) -> str:
        """Concise identity showing component, pass status, detected version."""
        return (
            f"RuntimeCheckResult(component={self.component!r}, "
            f"passed={self.passed!r}, detected={self.detected_version!r}, "
            f"blocker={self.is_blocker!r})"
        )


@dataclass(frozen=True, slots=True)
class RuntimeDoctorReport:
    """Aggregated doctor outcome.

    Attributes:
        passed: True when no blocker check failed.
        checks: Per-row check results, in matrix declaration order.
        blockers: Human-readable list of blocker messages.
        matrix_verified_at: Parsed ISO date of the matrix's last verification.
        matrix_staleness_warning: Populated when the matrix is stale.
    """

    passed: bool
    checks: tuple[RuntimeCheckResult, ...]
    blockers: tuple[str, ...]
    matrix_verified_at: datetime | None
    matrix_staleness_warning: str | None = None
    _advisory_failures: tuple[str, ...] = field(default_factory=tuple)

    def __repr__(self) -> str:
        """Concise identity for diagnostics."""
        return (
            f"RuntimeDoctorReport(passed={self.passed!r}, "
            f"checks={len(self.checks)}, "
            f"blockers={len(self.blockers)}, "
            f"staleness={self.matrix_staleness_warning is not None})"
        )


__all__ = ["RuntimeCheckResult", "RuntimeDoctorReport"]
