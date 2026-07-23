"""Policy loader, rate limiter, and audit logger for sandbox policy."""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from vetinari.config.sandbox_schema import SandboxPolicyConfig
from vetinari.constants import _PROJECT_ROOT
from vetinari.guards import GateError

logger = logging.getLogger(__name__)


class _SandboxPolicyLoader:
    """Loads and validates the sandbox policy from YAML configuration."""

    _DEFAULT_POLICY_PATH: Path = _PROJECT_ROOT / "config" / "sandbox_policy.yaml"

    def load(self, policy_path: Path | None = None) -> SandboxPolicyConfig:
        """Load and validate the sandbox policy from disk.

        Args:
        policy_path: Optional override path to ``sandbox_policy.yaml``.

        Returns:
        Validated sandbox policy config.

        Raises:
            GateError: Raised when no policy file is present.
            Exception: Propagated when validation, persistence, or execution fails.
        """
        resolved_path = policy_path or self._DEFAULT_POLICY_PATH
        try:
            policy = SandboxPolicyConfig.from_yaml_file(resolved_path)
            logger.info("SandboxManager loaded policy from %s", resolved_path)
            return policy
        except FileNotFoundError as exc:
            logger.error(
                "sandbox_policy.yaml not found at %s - execution denied",
                resolved_path,
            )
            raise GateError(
                "sandbox_policy",
                f"no sandbox policy found at {resolved_path} - execution denied",
                exc,
            ) from exc
        except ValueError as exc:
            logger.error(
                "sandbox_policy.yaml at %s failed validation - refusing permissive defaults: %s",
                resolved_path,
                exc,
            )
            raise
        except Exception as exc:
            logger.error(
                "sandbox_policy.yaml at %s could not be loaded - refusing permissive defaults: %s",
                resolved_path,
                exc,
            )
            raise


class _SandboxRateLimiter:
    """Per-client rate limiting for sandbox execution requests."""

    def __init__(self, max_calls: int, window_seconds: float) -> None:
        """Configure the rate limiter.

        Args:
            max_calls: Maximum number of calls allowed per client per window.
            window_seconds: Length of the sliding window in seconds.
        """
        self._max_calls = max_calls
        self._window = window_seconds
        self._log: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def check(self, client_id: str) -> bool:
        """Return True if the client is within the rate limit.

        Args:
            client_id: Identifier for the calling client.

        Returns:
            True when the client may proceed; otherwise False.
        """
        now = time.time()
        cutoff = now - self._window
        with self._lock:
            timestamps = [t for t in self._log.get(client_id, []) if t > cutoff]
            if len(timestamps) >= self._max_calls:
                self._log[client_id] = timestamps
                return False
            timestamps.append(now)
            self._log[client_id] = timestamps
            return True


class _SandboxAuditLogger:
    """Structured audit logging for sandbox execution events."""

    _MAX_BUFFER = 1000

    def __init__(self) -> None:
        """Initialise the in-memory ring buffer for audit records."""
        self._buffer: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    def record(
        self,
        sandbox_type: str,
        execution_id: str,
        status: str,
        duration_ms: int,
        code_length: int,
    ) -> None:
        """Persist a sandbox execution audit record.

        Args:
            sandbox_type: Execution strategy used.
            execution_id: Unique identifier for this execution run.
            status: Outcome string.
            duration_ms: Wall-clock execution time in milliseconds.
            code_length: Number of characters in the submitted code.

        Raises:
            GateError: Raised when the durable audit logger rejects or cannot
                persist the sandbox execution record.
        """
        entry = {
            "sandbox_type": sandbox_type,
            "execution_id": execution_id,
            "status": status,
            "duration_ms": duration_ms,
            "code_length": code_length,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        with self._lock:
            self._buffer.append(entry)
            if len(self._buffer) > self._MAX_BUFFER:
                self._buffer = self._buffer[-self._MAX_BUFFER :]
        try:
            from vetinari.audit import get_audit_logger

            get_audit_logger().log_sandbox_execution(
                sandbox_type=sandbox_type,
                execution_id=execution_id,
                status=status,
                duration_ms=float(duration_ms),
                code_length=code_length,
            )
        except Exception as exc:
            raise GateError(
                "sandbox_audit",
                "sandbox audit logging failed; execution result cannot be accepted without audit evidence",
                exc,
            ) from exc

    def get_records(self, limit: int = 100) -> list[dict[str, Any]]:
        """Return recent audit records from the in-memory buffer.

        Args:
            limit: Maximum number of records to return.

        Returns:
            List of audit record dictionaries, newest first.

        Raises:
            GateError: Raised when ``limit`` is less than one.
        """
        if limit < 1:
            raise GateError("sandbox_audit", "audit record limit must be positive")
        with self._lock:
            return list(self._buffer[-limit:])


__all__ = ["_SandboxAuditLogger", "_SandboxPolicyLoader", "_SandboxRateLimiter"]
