"""Sandbox data types — enums, result dataclasses, and audit entry.

Defines the value types shared by all sandbox modules so that individual
modules can import lightweight types without pulling in the full execution
machinery.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from importlib import import_module
from typing import Any, cast

logger = logging.getLogger(__name__)


# ── Structured logging shim ──────────────────────────────────────────────────
# Import structured logging if available; fall back to stdlib silently.

try:
    _structured_logging: Any = import_module("vetinari.structured_logging")
except ImportError:
    _structured_logging = None

if _structured_logging is not None:
    _STRUCTURED_LOGGING = True
    _get_structured_logger = cast(Callable[[str], logging.Logger], _structured_logging.get_logger)
    log_sandbox_execution = cast(Callable[..., None], _structured_logging.log_sandbox_execution)
else:
    _STRUCTURED_LOGGING = False

    def _get_stdlib_logger(name: str) -> logging.Logger:
        """Return a stdlib logger when structured logging is unavailable."""
        return logging.getLogger(name)

    def _log_sandbox_execution_noop(*_args: object, **_kwargs: object) -> None:
        """No-op fallback when structured logging is unavailable."""

    _get_structured_logger = _get_stdlib_logger
    log_sandbox_execution = _log_sandbox_execution_noop


# ── Enums ────────────────────────────────────────────────────────────────────


class SandboxType(Enum):
    """Sandbox execution strategy."""

    IN_PROCESS = "in_process"
    EXTERNAL = "external"


class SandboxStatus(Enum):
    """Runtime status of a sandbox backend."""

    AVAILABLE = "available"
    BUSY = "busy"
    ERROR = "error"


# ── Result dataclasses ───────────────────────────────────────────────────────


@dataclass
class ExecutionResult:
    """Result of code execution inside a CodeSandbox subprocess.

    Attributes:
        success: Whether execution completed without unhandled exception.
        output: Captured standard output from the sandboxed code.
        error: Error message or empty string when execution succeeded.
        execution_time_ms: Wall-clock time in milliseconds.
        return_code: Subprocess exit code (0 = success).
        stdout: Raw standard output captured by the wrapper.
        stderr: Raw standard error captured by the wrapper.
        files_created: Paths of files written by the sandboxed code.
        metadata: Supplementary diagnostic information.
    """

    success: bool
    output: str
    error: str = ""
    execution_time_ms: int = 0
    return_code: int = 0
    stdout: str = ""
    stderr: str = ""
    files_created: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __repr__(self) -> str:
        return (
            f"ExecutionResult(success={self.success!r}, return_code={self.return_code!r}, "
            f"execution_time_ms={self.execution_time_ms!r})"
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-compatible dictionary.

        Returns:
            Dict representation of this result, safe for JSON serialisation.
        """
        from vetinari.utils.serialization import dataclass_to_dict

        return cast(dict[str, Any], dataclass_to_dict(self))

    def to_feedback(self) -> Any:
        """Convert this execution result to a structured ExecutionFeedback object.

        Parses stdout and stderr for test failures, tracebacks, and lint
        errors so agents can use structured feedback in retry prompts.

        Returns:
            ExecutionFeedback populated from this result's output streams.
        """
        from vetinari.agents.execution_feedback import parse_sandbox_output

        return parse_sandbox_output(
            stdout=self.stdout or self.output or "",
            stderr=self.stderr or self.error or "",
            return_code=self.return_code,
        )


@dataclass(frozen=True, slots=True)
class SandboxResult:
    """Result of an in-process or manager-level sandbox execution.

    Attributes:
        execution_id: Unique identifier for this execution.
        success: Whether the execution completed without error.
        result: The captured return value (in-process mode only).
        error: Error message if the execution failed.
        execution_time_ms: Wall-clock execution time in milliseconds.
        memory_used_mb: Peak memory usage in megabytes (in-process only).
    """

    execution_id: str
    success: bool
    result: Any = None
    error: str = ""
    execution_time_ms: int = 0
    memory_used_mb: float = 0.0

    def __repr__(self) -> str:
        return (
            f"SandboxResult(execution_id={self.execution_id!r}, success={self.success!r}, "
            f"execution_time_ms={self.execution_time_ms!r})"
        )


@dataclass(frozen=True, slots=True)
class SandboxAuditEntry:
    """Audit log entry for plugin sandbox executions.

    Attributes:
        timestamp: ISO-8601 timestamp string.
        execution_id: Unique execution identifier.
        operation: The hook/operation name.
        sandbox_type: Which sandbox backend handled the execution.
        status: Outcome status string (executing, success, error).
        duration_ms: Execution duration in milliseconds.
        details: Additional context about the execution.
    """

    timestamp: str
    execution_id: str
    operation: str
    sandbox_type: str
    status: str
    duration_ms: int
    details: dict = field(default_factory=dict)

    def __repr__(self) -> str:
        return (
            f"AuditEntry(execution_id={self.execution_id!r}, operation={self.operation!r}, "
            f"status={self.status!r}, duration_ms={self.duration_ms!r})"
        )


AuditEntry = SandboxAuditEntry
