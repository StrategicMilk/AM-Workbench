"""Execution Feedback — parse sandbox output into structured agent retry prompts.

Parses pytest results, Python tracebacks, and type/lint error output from
code execution sandboxes into a compact, structured form that agents can
use to self-correct on the next attempt.

Tail truncation is applied at TAIL_LINES=50 to avoid overwhelming agent
context windows with large stdout streams.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from vetinari.security.redaction import redact_text

logger = logging.getLogger(__name__)


TAIL_LINES = 50  # Maximum lines retained from sandbox output per stream

# -- Regex patterns for common failure types --------------------------------

# pytest summary line: "FAILED tests/foo.py::test_bar - AssertionError: ..."
_PYTEST_FAILED_RE = re.compile(
    r"^(FAILED|ERROR)\s+([\w/.\-:]+)\s*(?:-\s*(.*))?$",
    re.MULTILINE,
)

# Traceback: "Traceback (most recent call last):" followed by indented lines
_TRACEBACK_RE = re.compile(
    r"Traceback \(most recent call last\):\s*\n((?:[ \t]+.+\n)+)([\w.]+Error[^\n]*)",
    re.MULTILINE,
)

# mypy / pyright type error: "path.py:42: error: ..."
_TYPE_ERROR_RE = re.compile(
    r"^([\w/.\-]+\.py):(\d+):\s*(error|warning):\s*(.+)$",
    re.MULTILINE,
)

# ruff / flake8 lint error: "path.py:10:5: E501 ..."
_LINT_ERROR_RE = re.compile(
    r"^([\w/.\-]+\.py):(\d+):\d+:\s*([A-Z]\d{3,4})\s+(.+)$",
    re.MULTILINE,
)

# pytest short-result header: "5 failed, 12 passed in 3.21s"
_PYTEST_SUMMARY_RE = re.compile(
    r"(\d+)\s+failed.*?(\d+)\s+passed.*?in\s+([\d.]+)s",
    re.IGNORECASE,
)


def _redact_feedback_text(value: object) -> str:
    """Redact sandbox feedback before it is stored or returned to retry prompts."""
    return redact_text(str(value))


@dataclass(frozen=True, slots=True)
class TestResult:
    """A single failing test or error extracted from sandbox output.

    Args:
        test_id: Pytest node ID (e.g. ``tests/foo.py::test_bar``).
        kind: ``"FAILED"`` or ``"ERROR"``.
        message: Short error message or assertion failure text.
    """

    test_id: str
    kind: str
    message: str = ""

    def __repr__(self) -> str:
        return f"TestResult(kind={self.kind!r}, test_id={self.test_id!r})"


@dataclass
class ExecutionFeedback:
    """Structured feedback extracted from a sandbox execution result.

    Provides all information an agent needs to understand what went wrong
    and attempt a targeted correction.

    Args:
        success: True if the execution completed without errors.
        return_code: Process exit code (0 = success).
        failed_tests: List of failing pytest test results.
        tracebacks: Extracted Python traceback strings.
        type_errors: Type checker error lines (mypy/pyright).
        lint_errors: Linter error lines (ruff/flake8).
        raw_stdout: Tail-truncated stdout.
        raw_stderr: Tail-truncated stderr.
        summary: Human-readable summary line (e.g. from pytest header).
    """

    success: bool
    return_code: int = 0
    failed_tests: list[TestResult] = field(default_factory=list)
    tracebacks: list[str] = field(default_factory=list)
    type_errors: list[str] = field(default_factory=list)
    lint_errors: list[str] = field(default_factory=list)
    raw_stdout: str = ""
    raw_stderr: str = ""
    summary: str = ""

    def __repr__(self) -> str:
        return (
            f"ExecutionFeedback(success={self.success!r}, "
            f"failed_tests={len(self.failed_tests)}, "
            f"tracebacks={len(self.tracebacks)}, "
            f"type_errors={len(self.type_errors)}, "
            f"lint_errors={len(self.lint_errors)})"
        )

    def to_prompt(self) -> str:
        """Format the feedback into a terse retry prompt for an agent.

        Produces a compact text block an agent can prepend to its next
        attempt, covering: summary line, failing tests (up to 10), first 3
        tracebacks, first 10 type/lint errors each.

        Returns:
            Multi-line string suitable for injection into an agent prompt.
        """
        parts: list[str] = ["## Execution Feedback\n"]

        if self.summary:
            parts.append(f"**Summary**: {_redact_feedback_text(self.summary)}\n")

        if self.failed_tests:
            parts.append(f"**Failed tests ({len(self.failed_tests)})**:")
            for tr in self.failed_tests[:10]:
                msg = f" — {_redact_feedback_text(tr.message)}" if tr.message else ""
                parts.append(f"  - [{tr.kind}] {tr.test_id}{msg}")
            if len(self.failed_tests) > 10:
                parts.append(f"  ... and {len(self.failed_tests) - 10} more")

        if self.tracebacks:
            parts.append(f"\n**Tracebacks ({len(self.tracebacks)})**:")
            parts.extend(f"```\n{_redact_feedback_text(tb.strip())}\n```" for tb in self.tracebacks[:3])

        if self.type_errors:
            parts.append(f"\n**Type errors ({len(self.type_errors)})**:")
            parts.extend(f"  - {_redact_feedback_text(err)}" for err in self.type_errors[:10])

        if self.lint_errors:
            parts.append(f"\n**Lint errors ({len(self.lint_errors)})**:")
            parts.extend(f"  - {_redact_feedback_text(err)}" for err in self.lint_errors[:10])

        if not self.success and not any([self.failed_tests, self.tracebacks, self.type_errors, self.lint_errors]):
            # Generic failure with no parsed detail
            if self.raw_stderr:
                parts.append(f"\n**Stderr**:\n```\n{_redact_feedback_text(self.raw_stderr[:500])}\n```")
            elif self.raw_stdout:
                parts.append(f"\n**Stdout**:\n```\n{_redact_feedback_text(self.raw_stdout[:500])}\n```")

        return "\n".join(parts)


def _tail(text: str, max_lines: int = TAIL_LINES) -> str:
    """Return the last *max_lines* lines of *text*.

    Args:
        text: Multi-line string to truncate.
        max_lines: Maximum number of lines to retain from the end.

    Returns:
        Truncated string (last N lines joined with newlines).
    """
    if not text:
        return ""
    lines = text.splitlines()
    if len(lines) <= max_lines:
        return text
    omitted = len(lines) - max_lines
    return f"[... {omitted} lines omitted ...]\n" + "\n".join(lines[-max_lines:])


def parse_sandbox_output(
    stdout: str,
    stderr: str,
    return_code: int,
) -> ExecutionFeedback:
    """Parse raw sandbox stdout/stderr into structured ExecutionFeedback.

    Applies tail truncation (TAIL_LINES) before analysis so large outputs
    don't bloat agent context.  Parsing is best-effort — unknown output
    formats are captured as raw text rather than raising errors.

    Args:
        stdout: Standard output from the sandbox execution.
        stderr: Standard error from the sandbox execution.
        return_code: Exit code from the sandbox process.

    Returns:
        ExecutionFeedback populated from the parsed output.
    """
    success = return_code == 0
    stdout_tail = _redact_feedback_text(_tail(stdout))
    stderr_tail = _redact_feedback_text(_tail(stderr))

    # Combine streams for pattern matching (many tools write to stderr)
    combined = stdout + "\n" + stderr

    # -- Parse pytest failures -----------------------------------------------
    failed_tests: list[TestResult] = []
    for m in _PYTEST_FAILED_RE.finditer(combined):
        kind = m.group(1)
        test_id = m.group(2).strip()
        message = _redact_feedback_text((m.group(3) or "").strip())
        failed_tests.append(TestResult(test_id=test_id, kind=kind, message=message))

    # -- Parse tracebacks -----------------------------------------------------
    tracebacks: list[str] = []
    for m in _TRACEBACK_RE.finditer(combined):
        tb = f"Traceback (most recent call last):\n{m.group(1)}{m.group(2)}"
        tracebacks.append(_redact_feedback_text(tb))

    # -- Parse type errors (mypy / pyright) -----------------------------------
    type_errors = [
        _redact_feedback_text(f"{m.group(1)}:{m.group(2)}: {m.group(4)}")
        for m in _TYPE_ERROR_RE.finditer(combined)
        if m.group(3) == "error"
    ]

    # -- Parse lint errors (ruff / flake8) ------------------------------------
    lint_errors = [
        _redact_feedback_text(f"{m.group(1)}:{m.group(2)}: {m.group(3)} {m.group(4)}")
        for m in _LINT_ERROR_RE.finditer(combined)
    ]

    # -- Extract pytest summary line ------------------------------------------
    summary = ""
    m_sum = _PYTEST_SUMMARY_RE.search(combined)
    if m_sum:
        summary = _redact_feedback_text(f"{m_sum.group(1)} failed, {m_sum.group(2)} passed in {m_sum.group(3)}s")

    logger.debug(
        "[ExecutionFeedback] parsed: rc=%d tests=%d tracebacks=%d type=%d lint=%d",
        return_code,
        len(failed_tests),
        len(tracebacks),
        len(type_errors),
        len(lint_errors),
    )

    return ExecutionFeedback(
        success=success,
        return_code=return_code,
        failed_tests=failed_tests,
        tracebacks=tracebacks,
        type_errors=type_errors,
        lint_errors=lint_errors,
        raw_stdout=stdout_tail,
        raw_stderr=stderr_tail,
        summary=summary,
    )
