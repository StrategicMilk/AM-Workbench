"""Shared data types for safety guardrails."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from vetinari.utils.serialization import dataclass_to_dict


@dataclass(frozen=True, slots=True)
class Violation:
    """A single guardrail violation."""

    rail: str
    severity: str
    description: str
    matched_pattern: str = ""

    def __repr__(self) -> str:
        """Return a compact debug representation."""
        return f"Violation(rail={self.rail!r}, severity={self.severity!r})"

    def to_dict(self) -> dict[str, Any]:
        """Converts violation fields to a JSON-serializable dict."""
        return dataclass_to_dict(self)


@dataclass
class GuardrailResult:
    """Result of a guardrail check."""

    allowed: bool
    content: str
    violations: list[Violation] = field(default_factory=list)
    latency_ms: float = 0.0
    activated_rails: list[str] = field(default_factory=list)  # Rail names that fired during NeMo evaluation

    def __repr__(self) -> str:
        """Return a compact debug representation."""
        return (
            f"GuardrailResult(allowed={self.allowed!r}, violations={len(self.violations)}, "
            f"latency_ms={self.latency_ms!r})"
        )

    def to_dict(self) -> dict[str, Any]:
        """Converts guardrail result fields to a JSON-serializable dict."""
        return dataclass_to_dict(self)


class RailContext:
    """Determines which rails to apply based on context."""

    USER_FACING = "user_facing"
    INTERNAL_AGENT = "internal_agent"
    CODE_EXECUTION = "code_execution"
