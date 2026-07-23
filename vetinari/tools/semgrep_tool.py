"""Semgrep Security Scanning Tool.

AST-aware security scanning using Semgrep. Catches aliased imports,
multi-line patterns, and complex code structures that regex misses.
"""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from vetinari.agents.contracts import OutcomeSignal, Provenance, ToolEvidence
from vetinari.constants import GREP_TIMEOUT
from vetinari.types import EvidenceBasis

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SemgrepFinding:
    """A single Semgrep finding."""

    rule_id: str
    file: str
    line: int
    message: str
    severity: str = "WARNING"

    def __repr__(self) -> str:
        return f"SemgrepFinding({self.rule_id}:{self.file}:{self.line})"


@dataclass
class SemgrepResult:
    """Aggregated Semgrep scan result."""

    findings: list[SemgrepFinding] = field(default_factory=list)
    error: str = ""
    is_available: bool = True

    @property
    def has_findings(self) -> bool:
        """Whether any issues were detected."""
        return len(self.findings) > 0


def run_semgrep(
    target_dir: Path | str,
    config: str = "auto",
    extra_rules: list[dict[str, Any]] | None = None,
    timeout: int = GREP_TIMEOUT * 3,
) -> SemgrepResult:
    """Run Semgrep scan on a directory.

    Args:
        target_dir: Directory or file to scan.
        config: Semgrep config.
        extra_rules: Reserved for future inline rules.
        timeout: Max execution time in seconds.

    Returns:
        SemgrepResult with findings or error information.
    """
    target = Path(target_dir)
    if not target.exists():
        return SemgrepResult(error=f"Target does not exist: {target}", is_available=False)
    cmd = ["semgrep", "--json", "--config", config, str(target)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except FileNotFoundError:
        logger.warning(
            "Semgrep not on PATH â€” security scanning unavailable. Install Vetinari's dev extras: pip install -e .[dev]"
        )
        return SemgrepResult(error="semgrep not installed", is_available=False)
    except subprocess.TimeoutExpired:
        logger.warning("Semgrep timed out after %ds â€” security scan incomplete", timeout)
        return SemgrepResult(error=f"Semgrep timed out after {timeout}s")
    if proc.returncode not in (0, 1):
        return SemgrepResult(error=f"Semgrep failed: {proc.stderr[:500]}")
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        logger.warning("Could not parse Semgrep JSON output â€” security scan results unavailable")
        return SemgrepResult(error="Could not parse Semgrep JSON output")
    findings = [
        SemgrepFinding(
            rule_id=result.get("check_id", "unknown"),
            file=result.get("path", ""),
            line=result.get("start", {}).get("line", 0),
            message=result.get("extra", {}).get("message", ""),
            severity=result.get("extra", {}).get("severity", "WARNING"),
        )
        for result in data.get("results", [])
    ]
    return SemgrepResult(findings=findings)


# -- OutcomeSignal wrapper ---------------------------------------------------


def _semgrep_utc_now() -> str:
    """Return current UTC time as ISO-8601 string."""
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _semgrep_sha256(text: str) -> tuple[str, str]:
    """Return (first-2KB snippet, SHA-256 hex) for stdout text.

    Args:
        text: Raw stdout text from semgrep.

    Returns:
        Tuple of (snippet, sha256_hex).
    """
    import hashlib

    snippet = text[:2048]
    digest = hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()
    return snippet, digest


def run_semgrep_signal(
    target_dir: Path | str,
    config: str = "auto",
    extra_rules: list[dict[str, Any]] | None = None,
    timeout: int = GREP_TIMEOUT * 3,
) -> OutcomeSignal:
    """Run Semgrep and return an evidence-backed OutcomeSignal.

    Args:
        target_dir: Target object or path updated by the operation.
        config: Config value consumed by run_semgrep_signal().
        extra_rules: Extra rules value consumed by run_semgrep_signal().
        timeout: Timeout value controlling how long the operation may wait.

    Returns:
        Value produced for the caller.
    """
    result = run_semgrep(target_dir, config=config, extra_rules=extra_rules, timeout=timeout)
    if not result.is_available:
        return _semgrep_unavailable_signal()
    if result.error:
        return _semgrep_error_signal(result.error, target_dir, config)
    return _semgrep_findings_signal(result, target_dir, config)


def _semgrep_provenance() -> Provenance:
    """Return standard Semgrep provenance."""
    return Provenance(source="vetinari.tools.semgrep_tool", timestamp_utc=_semgrep_utc_now(), tool_name="semgrep")


def _semgrep_unavailable_signal() -> OutcomeSignal:
    """Return fail-closed signal when semgrep is unavailable."""
    return OutcomeSignal(
        passed=False,
        score=0.0,
        basis=EvidenceBasis.UNSUPPORTED,
        issues=(
            "semgrep not on PATH - security scanning unavailable. Install Vetinari's dev extras: pip install -e .[dev]",
        ),
        provenance=_semgrep_provenance(),
    )


def _semgrep_error_signal(error: str, target_dir: Path | str, config: str) -> OutcomeSignal:
    """Return fail-closed signal for a Semgrep execution error."""
    snippet, sha = _semgrep_sha256(error)
    return OutcomeSignal(
        passed=False,
        score=0.0,
        basis=EvidenceBasis.TOOL_EVIDENCE,
        tool_evidence=(
            ToolEvidence("semgrep", f"semgrep --json --config {config} {target_dir}", 2, snippet, sha, False),
        ),
        issues=(f"semgrep error: {error}",),
        provenance=_semgrep_provenance(),
    )


def _semgrep_findings_signal(result: SemgrepResult, target_dir: Path | str, config: str) -> OutcomeSignal:
    """Return evidence-backed signal for Semgrep findings."""
    issues = tuple(f"{f.file}:{f.line} [{f.severity}] {f.rule_id}: {f.message}" for f in result.findings)
    snippet, sha = _semgrep_sha256("; ".join(issues) or "no findings")
    passed = not result.has_findings
    score = 1.0 if passed else round(1.0 / (1.0 + len(result.findings)), 3)
    return OutcomeSignal(
        passed=passed,
        score=score,
        basis=EvidenceBasis.TOOL_EVIDENCE,
        tool_evidence=(
            ToolEvidence("semgrep", f"semgrep --json --config {config} {target_dir}", 0, snippet, sha, passed),
        ),
        issues=issues,
        provenance=_semgrep_provenance(),
    )
