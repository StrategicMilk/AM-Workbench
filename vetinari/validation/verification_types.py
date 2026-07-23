"""Shared verification result and status types."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class VerificationLevel(Enum):
    """Levels of verification strictness."""

    NONE = "none"
    BASIC = "basic"
    STANDARD = "standard"
    STRICT = "strict"
    PARANOID = "paranoid"


class VerificationStatus(Enum):
    """Status of verification."""

    PASSED = "passed"
    WARNING = "warning"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass(frozen=True, slots=True)
class VerificationIssue:
    """Represents a single verification issue found."""

    severity: str
    category: str
    message: str
    location: str | None = None
    suggestion: str | None = None

    def __repr__(self) -> str:
        """Return a compact debug representation."""
        return f"VerificationIssue(severity={self.severity!r}, category={self.category!r}, location={self.location!r})"


@dataclass
class ValidationVerificationResult:
    """Result of a verification check."""

    status: VerificationStatus
    check_name: str
    issues: list[VerificationIssue] = field(default_factory=list)
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    execution_time_ms: int = 0

    @property
    def error_count(self) -> int:
        """Number of error-level findings in this verification result."""
        return sum(1 for i in self.issues if i.severity == "error")

    @property
    def warning_count(self) -> int:
        """Number of warning-level findings in this verification result."""
        return sum(1 for i in self.issues if i.severity == "warning")

    @property
    def info_count(self) -> int:
        """Number of info-level findings in this verification result."""
        return sum(1 for i in self.issues if i.severity == "info")

    def __repr__(self) -> str:
        """Return a compact debug representation."""
        return (
            f"VerificationResult(check_name={self.check_name!r}, status={self.status.value!r}, "
            f"issues={len(self.issues)}, execution_time_ms={self.execution_time_ms!r})"
        )

    def to_dict(self) -> dict[str, Any]:
        """Convert result to dictionary."""
        return {
            "status": self.status.value,
            "check_name": self.check_name,
            "issues": [
                {
                    "severity": i.severity,
                    "category": i.category,
                    "message": i.message,
                    "location": i.location,
                    "suggestion": i.suggestion,
                }
                for i in self.issues
            ],
            "error_count": self.error_count,
            "warning_count": self.warning_count,
            "info_count": self.info_count,
            "execution_time_ms": self.execution_time_ms,
        }
