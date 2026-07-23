"""Project scanner — finds code quality, security, and maintainability issues.

This module is the first step in Vetinari's autonomous improvement pipeline:
**Scan → Prioritize → Fix → Verify**.  It runs multiple static analysis tools
against a target project directory and returns a unified list of findings
sorted by risk so the most important problems are addressed first.

Pipeline role: Step 1 of 4 — issue discovery. The scanner produces a
:class:`ScanResult` that the sandbox (step 2) uses to safely apply fixes.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


# Tool timeout in seconds — long enough for large projects, short enough to
# avoid hanging the pipeline indefinitely.
_TOOL_TIMEOUT: int = 60

# Severity ranking used by prioritize_findings (lower = higher priority).
_SEVERITY_RANK: dict[str, int] = {
    "critical": 0,
    "high": 1,
    "medium": 2,
    "low": 3,
}

# Category ranking: security issues must be fixed before style issues.
_CATEGORY_RANK: dict[str, int] = {
    "security": 0,
    "bug": 1,
    "performance": 2,
    "maintainability": 3,
    "style": 4,
}


@dataclass(frozen=True, slots=True)
class ScanFinding:
    """A single issue discovered in the target project.

    Attributes:
        category: Broad issue type (security, bug, performance,
            maintainability, style).
        severity: How critical the issue is (critical, high, medium, low).
        file_path: Path to the file containing the issue, relative to the
            project root.
        line: Line number where the issue was found (1-based). Zero means
            the issue applies to the whole file.
        message: Human-readable description of the problem.
        tool: Which analysis tool reported this finding (ruff, semgrep,
            coverage, vulture, pyright).
        is_reachable: Whether the affected code is known to be executable.
            Static dead-code hints are not proof of runtime unreachability.
    """

    category: str  # security | bug | performance | maintainability | style
    severity: str  # critical | high | medium | low
    file_path: str
    line: int
    message: str
    tool: str  # ruff | semgrep | coverage | vulture | pyright
    is_reachable: bool = True

    def __repr__(self) -> str:
        return f"ScanFinding(tool={self.tool!r}, severity={self.severity!r}, file={self.file_path!r}:{self.line})"


@dataclass
class ScanResult:
    """Aggregated output from a full project scan.

    Attributes:
        findings: All issues found, sorted by priority after
            :func:`prioritize_findings` is called.
        project_path: Absolute path to the scanned project root.
        scan_time_ms: Total wall-clock time the scan took in milliseconds.
        has_tests: Whether the project contains a test suite.
        tools_used: Names of the tools that ran successfully.
    """

    findings: list[ScanFinding]
    project_path: Path
    scan_time_ms: float
    has_tests: bool
    tools_used: list[str] = field(default_factory=list)

    def __repr__(self) -> str:
        return f"ScanResult(findings={len(self.findings)}, has_tests={self.has_tests!r}, tools={self.tools_used!r})"


def scan_project(project_path: Path) -> ScanResult:
    """Scan a project directory for issues using all available tools.

    Runs ruff (style/bugs) and vulture (dead code) against the project.
    Each tool that is not installed is silently skipped so the scan
    degrades gracefully when running in a minimal environment.

    Args:
        project_path: Absolute path to the root of the target project.

    Returns:
        A :class:`ScanResult` containing all discovered findings, sorted
        by severity and category.
    """
    start = time.monotonic()
    all_findings: list[ScanFinding] = []
    tools_used: list[str] = []

    ruff_findings = _run_ruff(project_path)
    if ruff_findings is not None:
        all_findings.extend(ruff_findings)
        tools_used.append("ruff")

    dead_code_findings = _run_dead_code_check(project_path)
    if dead_code_findings is not None:
        all_findings.extend(dead_code_findings)
        tools_used.append("vulture")

    semgrep_findings = _run_semgrep(project_path)
    if semgrep_findings is not None:
        all_findings.extend(semgrep_findings)
        tools_used.append("semgrep")

    dep_vuln_findings = _run_dependency_audit(project_path)
    if dep_vuln_findings is not None:
        all_findings.extend(dep_vuln_findings)
        tools_used.append("pip-audit")

    type_findings = _run_type_check(project_path)
    if type_findings is not None:
        all_findings.extend(type_findings)
        tools_used.append("pyright")

    has_tests = _detect_test_presence(project_path)
    sorted_findings = prioritize_findings(all_findings)
    elapsed_ms = (time.monotonic() - start) * 1000.0

    logger.info(
        "Scan of %s complete: %d findings in %.0f ms (tools: %s)",
        project_path,
        len(sorted_findings),
        elapsed_ms,
        tools_used,
    )

    return ScanResult(
        findings=sorted_findings,
        project_path=project_path,
        scan_time_ms=elapsed_ms,
        has_tests=has_tests,
        tools_used=tools_used,
    )


def prioritize_findings(findings: list[ScanFinding]) -> list[ScanFinding]:
    """Sort findings so the highest-risk issues appear first.

    Primary sort key is category (security before style), secondary is
    severity (critical before low). Within the same category and severity,
    findings are ordered by file path and line number for stable output.

    Args:
        findings: Unsorted list of :class:`ScanFinding` objects.

    Returns:
        A new list sorted from most important to least important.
    """
    return sorted(
        findings,
        key=lambda f: (
            _CATEGORY_RANK.get(f.category, 99),
            _SEVERITY_RANK.get(f.severity, 99),
            f.file_path,
            f.line,
        ),
    )


def _run_ruff(project_path: Path) -> list[ScanFinding] | None:
    """Run ruff in JSON output mode and convert results to ScanFinding objects.

    Ruff covers style violations, common bugs, and some security patterns.
    Returns None when ruff is not installed so the caller can skip it.

    Args:
        project_path: Root of the project to lint.

    Returns:
        List of :class:`ScanFinding` objects, or None if ruff is unavailable.
    """
    ruff_exe = shutil.which("ruff")
    if not ruff_exe:
        logger.debug("ruff not found â€” skipping ruff scan")
        return None
    try:
        result = subprocess.run(
            [ruff_exe, "check", "--output-format=json", str(project_path)],
            capture_output=True,
            text=True,
            timeout=_TOOL_TIMEOUT,
            cwd=str(project_path),
        )
    except FileNotFoundError:
        logger.debug("ruff not found — skipping ruff scan")
        return None
    except subprocess.TimeoutExpired:
        logger.warning(
            "ruff scan of %s took longer than %ds — results discarded",
            project_path,
            _TOOL_TIMEOUT,
        )
        return []

    if not result.stdout.strip():
        return []

    try:
        raw = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        logger.warning(
            "Could not parse ruff JSON output for %s — skipping ruff results: %s",
            project_path,
            exc,
        )
        return []

    findings: list[ScanFinding] = []
    for item in raw:
        code: str = item.get("code", "")
        category, severity = _classify_ruff_code(code)
        findings.append(
            ScanFinding(
                category=category,
                severity=severity,
                file_path=item.get("filename", ""),
                line=item.get("location", {}).get("row", 0),
                message=item.get("message", ""),
                tool="ruff",
                is_reachable=True,
            )
        )
    return findings


def _run_dead_code_check(project_path: Path) -> list[ScanFinding] | None:
    """Run vulture to find unused functions, classes, and variables.

    Vulture findings are potential unused-code hints, not proof that a path is
    impossible to execute. Returns None when vulture is not installed.

    Args:
        project_path: Root of the project to analyze.

    Returns:
        List of :class:`ScanFinding` objects, or None if vulture is unavailable.
    """
    vulture_exe = shutil.which("vulture")
    if not vulture_exe:
        logger.debug("vulture not found â€” skipping dead code scan")
        return None
    try:
        result = subprocess.run(
            [vulture_exe, str(project_path), "--min-confidence=80"],
            capture_output=True,
            text=True,
            timeout=_TOOL_TIMEOUT,
            cwd=str(project_path),
        )
    except FileNotFoundError:
        logger.debug("vulture not found — skipping dead code scan")
        return None
    except subprocess.TimeoutExpired:
        logger.warning(
            "vulture scan of %s took longer than %ds — results discarded",
            project_path,
            _TOOL_TIMEOUT,
        )
        return []

    findings: list[ScanFinding] = []
    for line in result.stdout.splitlines():
        # Vulture text format: "path/file.py:42: unused variable 'foo' (confidence 80%)"
        if ":" not in line:
            continue
        parts = line.split(":", 2)
        if len(parts) < 3:
            continue
        file_part = parts[0].strip()
        try:
            line_no = int(parts[1].strip())
        except ValueError:
            line_no = 0
        message = parts[2].strip()
        findings.append(
            ScanFinding(
                category="maintainability",
                severity="low",
                file_path=file_part,
                line=line_no,
                message=message,
                tool="vulture",
                is_reachable=True,
            )
        )
    return findings


def _detect_test_presence(project_path: Path) -> bool:
    """Detect whether the project has a test suite.

    Looks for the most common test directory layouts and file naming
    conventions without actually running any tests.

    Args:
        project_path: Root of the project to inspect.

    Returns:
        True if at least one test file or test directory was found.
    """
    common_test_dirs = ["tests", "test", "spec", "specs"]
    for test_dir in common_test_dirs:
        candidate = project_path / test_dir
        if candidate.is_dir():
            return True

    # Also accept projects that put test files in the source tree.
    # next() with a default avoids building the full generator list.
    return (
        next(project_path.rglob("test_*.py"), None) is not None
        or next(project_path.rglob("*_test.py"), None) is not None
    )


def _classify_ruff_code(code: str) -> tuple[str, str]:
    """Map a ruff rule code to a (category, severity) pair.

    The mapping is intentionally coarse — exact severity per rule would
    require maintaining a full lookup table. The prefix letter is used to
    determine category, and severity is inferred from the prefix.

    Args:
        code: Ruff rule code such as ``"E501"`` or ``"S101"``.

    Returns:
        A ``(category, severity)`` tuple where both values are lowercase
        strings matching the ScanFinding field contracts.
    """
    # Extract the alphabetic prefix from the rule code (e.g. "PERF123" -> "PERF",
    # "UP001" -> "UP", "E501" -> "E"). Multi-char prefixes must be checked before
    # single-char ones so that "PERF" is not mistaken for "P".
    alpha_prefix = ""
    for ch in code:
        if ch.isalpha():
            alpha_prefix += ch.upper()
        else:
            break

    # Security rules (S = bandit via ruff-flake8-bandit)
    if alpha_prefix == "S":
        return "security", "high"

    # Bug-prone rules
    if alpha_prefix in ("F", "B", "C"):
        return "bug", "medium"

    # Performance (multi-char prefix — must come before single-char fallthrough)
    if alpha_prefix == "PERF":
        return "performance", "low"

    # Style / maintainability (UP = pyupgrade, also multi-char)
    if alpha_prefix in ("E", "W", "I", "N", "D", "UP", "T"):
        return "style", "low"

    # Everything else defaults to maintainability/low
    return "maintainability", "low"


def _scan_signal_finding(*, tool: str, category: str, severity: str, message: str) -> list[ScanFinding]:
    """Return a fail-closed scanner finding for an unavailable safety signal."""
    return [
        ScanFinding(
            category=category,
            severity=severity,
            file_path="<scanner-signal>",
            line=0,
            message=message,
            tool=tool,
            is_reachable=True,
        )
    ]


def _run_semgrep(project_path: Path) -> list[ScanFinding] | None:
    """Run semgrep for security-focused SAST analysis.

    Returns None if semgrep is not installed (graceful degradation).
    The subprocess call uses ``shutil.which()`` resolved paths (trusted).
    """
    semgrep_bin = shutil.which("semgrep")
    if not semgrep_bin:
        logger.warning("semgrep not found - recording unavailable SAST signal")
        return _scan_signal_finding(
            tool="semgrep",
            category="security",
            severity="high",
            message="semgrep unavailable: security SAST signal did not run",
        )

    # Validate project_path is a real directory (defense against path injection)
    if not project_path.is_dir():
        return _scan_signal_finding(
            tool="semgrep",
            category="security",
            severity="high",
            message=f"semgrep unavailable: project path is not a directory: {project_path}",
        )

    try:
        result = subprocess.run(
            [semgrep_bin, "scan", "--json", "--quiet", str(project_path)],
            capture_output=True,
            text=True,
            timeout=120,
            encoding="utf-8",
        )
        if result.returncode not in (0, 1):
            return _scan_signal_finding(
                tool="semgrep",
                category="security",
                severity="high",
                message=f"semgrep failed: exit code {result.returncode}",
            )

        raw = json.loads(result.stdout) if result.stdout.strip() else {}
        data = raw if isinstance(raw, dict) else {}
        findings: list[ScanFinding] = []
        for hit in data.get("results", []):
            severity = hit.get("extra", {}).get("severity", "warning").lower()
            mapped_severity = {"error": "high", "warning": "medium"}.get(severity, "low")
            findings.append(
                ScanFinding(
                    category="security",
                    severity=mapped_severity,
                    file_path=hit.get("path", ""),
                    line=hit.get("start", {}).get("line", 0),
                    message=hit.get("extra", {}).get("message", hit.get("check_id", "")),
                    tool="semgrep",
                )
            )
        return findings
    except subprocess.TimeoutExpired:
        logger.warning("semgrep scan timed out for %s", project_path)
        return _scan_signal_finding(
            tool="semgrep",
            category="security",
            severity="high",
            message="semgrep failed: scan timed out",
        )
    except json.JSONDecodeError:
        logger.warning("semgrep returned invalid JSON for %s", project_path)
        return _scan_signal_finding(
            tool="semgrep",
            category="security",
            severity="high",
            message="semgrep failed: invalid JSON output",
        )
    except OSError as exc:
        logger.warning("semgrep failed for %s: %s", project_path, type(exc).__name__)
        return _scan_signal_finding(
            tool="semgrep",
            category="security",
            severity="high",
            message=f"semgrep failed: {type(exc).__name__}",
        )


def _run_dependency_audit(project_path: Path) -> list[ScanFinding] | None:
    """Scan for vulnerable dependencies using pip-audit.

    Returns None if pip-audit is not installed (graceful degradation).
    """
    pip_audit_bin = shutil.which("pip-audit")
    if not pip_audit_bin:
        logger.warning("pip-audit not found - recording unavailable dependency audit signal")
        return _scan_signal_finding(
            tool="pip-audit",
            category="security",
            severity="high",
            message="pip-audit unavailable: dependency vulnerability signal did not run",
        )

    if not project_path.is_dir():
        return _scan_signal_finding(
            tool="pip-audit",
            category="security",
            severity="high",
            message=f"pip-audit unavailable: project path is not a directory: {project_path}",
        )

    try:
        result = subprocess.run(
            [pip_audit_bin, "--format=json", "--desc"],
            capture_output=True,
            text=True,
            timeout=120,
            encoding="utf-8",
            cwd=str(project_path),
        )
        # pip-audit exits 0 (clean) or 1 (vulnerabilities found) — anything else is a tool error
        if result.returncode not in (0, 1):
            logger.warning(
                "pip-audit exited with unexpected code %d — treating as scan failure",
                result.returncode,
            )
            return _scan_signal_finding(
                tool="pip-audit",
                category="security",
                severity="high",
                message=f"pip-audit failed: exit code {result.returncode}",
            )
        raw = json.loads(result.stdout) if result.stdout.strip() else {}
        data = raw if isinstance(raw, dict) else {}
        findings: list[ScanFinding] = [
            ScanFinding(
                category="security",
                severity="high",
                file_path="pyproject.toml",
                line=0,
                message=f"{vuln['name']}=={vuln['version']}: {v.get('id', '')} — {v.get('description', '')[:120]}",
                tool="pip-audit",
            )
            for vuln in data.get("dependencies", [])
            for v in vuln.get("vulns", [])
        ]
        return findings
    except subprocess.TimeoutExpired:
        logger.warning("pip-audit scan timed out for %s", project_path)
        return _scan_signal_finding(
            tool="pip-audit",
            category="security",
            severity="high",
            message="pip-audit failed: scan timed out",
        )
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("pip-audit failed for %s: %s", project_path, type(exc).__name__)
        return _scan_signal_finding(
            tool="pip-audit",
            category="security",
            severity="high",
            message=f"pip-audit failed: {type(exc).__name__}",
        )


def _run_type_check(project_path: Path) -> list[ScanFinding] | None:
    """Run pyright for static type checking.

    Returns None if pyright is not installed (graceful degradation).
    """
    pyright_bin = shutil.which("pyright")
    if not pyright_bin:
        logger.warning("pyright not found - recording unavailable type-check signal")
        return _scan_signal_finding(
            tool="pyright",
            category="bug",
            severity="medium",
            message="pyright unavailable: type-check signal did not run",
        )

    if not project_path.is_dir():
        return _scan_signal_finding(
            tool="pyright",
            category="bug",
            severity="medium",
            message=f"pyright unavailable: project path is not a directory: {project_path}",
        )

    try:
        result = subprocess.run(
            [pyright_bin, "--outputjson", str(project_path)],
            capture_output=True,
            text=True,
            timeout=120,
            encoding="utf-8",
        )
        # pyright exits 0 (no errors) or 1 (type errors found) — anything else is a tool error
        if result.returncode not in (0, 1):
            logger.warning(
                "pyright exited with unexpected code %d — treating as type-check failure",
                result.returncode,
            )
            return _scan_signal_finding(
                tool="pyright",
                category="bug",
                severity="medium",
                message=f"pyright failed: exit code {result.returncode}",
            )
        raw = json.loads(result.stdout) if result.stdout.strip() else {}
        data = raw if isinstance(raw, dict) else {}
        findings: list[ScanFinding] = []
        for diag in data.get("generalDiagnostics", []):
            severity_map = {"error": "high", "warning": "medium", "information": "low"}
            findings.append(
                ScanFinding(
                    category="bug",
                    severity=severity_map.get(diag.get("severity", ""), "low"),
                    file_path=diag.get("file", ""),
                    line=diag.get("range", {}).get("start", {}).get("line", 0),
                    message=diag.get("message", ""),
                    tool="pyright",
                )
            )
        return findings
    except subprocess.TimeoutExpired:
        logger.warning("pyright scan timed out for %s", project_path)
        return _scan_signal_finding(
            tool="pyright",
            category="bug",
            severity="medium",
            message="pyright failed: type check timed out",
        )
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("pyright failed for %s: %s", project_path, type(exc).__name__)
        return _scan_signal_finding(
            tool="pyright",
            category="bug",
            severity="medium",
            message=f"pyright failed: {type(exc).__name__}",
        )
