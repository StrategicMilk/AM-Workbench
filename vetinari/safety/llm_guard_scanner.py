"""LLM Guard stub — ML-based scanning disabled (llm-guard not installed).

The ``llm-guard`` library is not installed in this environment.  All scan
methods **fail closed**: they return ``is_safe=False`` so that callers block
the content rather than silently allowing it through.  Install ``llm-guard``
and restore a real implementation if ML-based scanning is required.

Usage::

    from vetinari.safety.llm_guard_scanner import get_llm_guard_scanner

    scanner = get_llm_guard_scanner()
    result = scanner.scan_input("user prompt here")
    # result.is_safe is always False in this stub — content is blocked
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Any

from vetinari.utils.serialization import dataclass_to_dict

logger = logging.getLogger(__name__)


# ── Data types ─────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class GuardScanFinding:
    """A single finding from an LLM Guard scanner.

    Attributes:
        scanner_name: Name of the scanner that produced this finding.
        is_safe: Whether the scanned text passed this scanner.
        score: Confidence score (0.0-1.0).
    """

    scanner_name: str
    is_safe: bool
    score: float
    unavailable: bool = False

    def __repr__(self) -> str:
        """Show key identifying fields for debugging."""
        return (
            f"GuardScanFinding(scanner_name={self.scanner_name!r}, "
            f"is_safe={self.is_safe!r}, unavailable={self.unavailable!r})"
        )

    def to_dict(self) -> dict[str, Any]:
        """Converts scanner finding fields to a JSON-serializable dict."""
        return dataclass_to_dict(self)


@dataclass
class GuardScanResult:
    """Aggregate result from multiple LLM Guard scanners.

    Attributes:
        is_safe: True when every scanner check passed.
        sanitized_text: Text after all sanitization passes.
        findings: Per-scanner findings.
        latency_ms: Total scan wall-clock time in milliseconds.
    """

    is_safe: bool
    sanitized_text: str
    findings: list[GuardScanFinding] = field(default_factory=list)
    latency_ms: float = 0.0

    def __repr__(self) -> str:
        """Show key identifying fields for debugging."""
        return f"ScanResult(is_safe={self.is_safe!r}, findings={len(self.findings)!r}, latency_ms={self.latency_ms!r})"

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-compatible dictionary."""
        return {
            "is_safe": self.is_safe,
            "findings_count": len(self.findings),
            "unsafe_findings": [f.to_dict() for f in self.findings if not f.is_safe],
            "latency_ms": round(self.latency_ms, 2),
        }


# ── LLMGuardScanner ───────────────────────────────────────────────────


class LLMGuardScanner:
    """Stub — ML-based scanning not available without llm-guard.

    All scan methods **fail closed**: they return ``is_safe=False`` so callers
    must block the content rather than allowing it through.  The ``available``
    attribute is always ``False``.  Use :func:`get_llm_guard_scanner` for the
    singleton instance.
    """

    def __init__(self, config_path: object = None) -> None:
        self.available = False

    def scan_input(self, text: str, context: str = "user_facing") -> GuardScanResult:
        """Fail closed — ML scanning not available, so content is blocked.

        Args:
            text: User input text.
            context: Trust context (ignored in stub).

        Returns:
            ScanResult with is_safe=False to ensure callers block the content.
        """
        finding = GuardScanFinding(scanner_name="llm_guard_stub", is_safe=False, score=0.0, unavailable=True)
        return GuardScanResult(is_safe=False, sanitized_text=text, findings=[finding])

    def scan_output(self, prompt: str, output: str, context: str = "user_facing") -> GuardScanResult:
        """Fail closed — ML scanning not available, so content is blocked.

        Args:
            prompt: Original input prompt (ignored in stub).
            output: LLM output text to scan.
            context: Trust context (ignored in stub).

        Returns:
            ScanResult with is_safe=False to ensure callers block the content.
        """
        finding = GuardScanFinding(scanner_name="llm_guard_stub", is_safe=False, score=0.0, unavailable=True)
        return GuardScanResult(is_safe=False, sanitized_text=output, findings=[finding])

    def get_stats(self) -> dict[str, Any]:
        """Return scanner statistics for dashboard display.

        Returns:
            Dictionary with available=False and zero scanner counts.
        """
        return {
            "available": False,
            "input_scanners": 0,
            "output_scanners": 0,
        }


# ── Singleton ──────────────────────────────────────────────────────────

_scanner_instance: LLMGuardScanner | None = None
_scanner_lock = threading.Lock()


def get_llm_guard_scanner() -> LLMGuardScanner:
    """Return the process-global LLMGuardScanner singleton.

    Returns:
        The LLMGuardScanner stub instance.
    """
    global _scanner_instance
    if _scanner_instance is None:
        with _scanner_lock:
            if _scanner_instance is None:
                _scanner_instance = LLMGuardScanner()
    return _scanner_instance


def reset_llm_guard_scanner() -> None:
    """Reset the singleton (intended for testing)."""
    global _scanner_instance
    with _scanner_lock:
        _scanner_instance = None
