"""Persistent Audit Logging for Vetinari.

Provides a thread-safe, file-based audit logger that records all security-relevant
events: permission checks, guardrail violations, sandbox executions, and agent actions.

Usage:
    from vetinari.audit import get_audit_logger

    audit = get_audit_logger()
    audit.log_event(
        event_type="permission_check",
        agent_type=AgentType.WORKER.value,
        action="file_write",
        outcome="allowed",
    )
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from vetinari.constants import AUDIT_LOG_DIR
from vetinari.privacy import PRIVACY_ENVELOPE_KEY, privacy_receipt
from vetinari.security.redaction import redact_text, redact_value

logger = logging.getLogger(__name__)


# Default audit log directory — resolved via constants to avoid relative path coupling.
DEFAULT_AUDIT_DIR: str = str(AUDIT_LOG_DIR)
# Default audit log file name — JSONL format for easy streaming and parsing.
DEFAULT_AUDIT_FILE = "vetinari_audit.jsonl"
DEFAULT_AUDIT_MAX_BYTES = 10 * 1024 * 1024
DEFAULT_AUDIT_BACKUP_COUNT = 5
AUDIT_LOG_PRIVACY_RETENTION_DAYS = 30
AUDIT_LOG_PRIVACY_CLASS = "operational"


@dataclass(frozen=True, slots=True)
class AuditEntry:
    """Represents a single audit log entry.

    Args:
        timestamp: ISO 8601 UTC timestamp of the event.
        event_type: Category of the event (permission_check, guardrail_violation,
            sandbox_execution, agent_action).
        agent_type: The agent type involved, if applicable.
        task_id: The task ID involved, if applicable.
        action: The specific action taken or attempted.
        resource: The resource being accessed or modified.
        outcome: Result of the action (allowed, denied, error).
        details: Additional context-specific details.
        trace_id: Correlation trace ID for linking related events.
    """

    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    event_type: str = ""
    agent_type: str = ""
    task_id: str = ""
    action: str = ""
    resource: str = ""
    outcome: str = ""  # allowed, denied, error
    details: dict[str, Any] = field(default_factory=dict)
    trace_id: str = ""

    def __repr__(self) -> str:
        return (
            f"AuditEntry(event_type={self.event_type!r}, agent_type={self.agent_type!r}, "
            f"task_id={self.task_id!r}, action={self.action!r}, outcome={self.outcome!r})"
        )

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe dictionary representation."""
        return _make_json_safe(asdict(self))


def _make_json_safe(obj: Any) -> Any:
    """Recursively convert non-JSON-serialisable types to safe equivalents.

    Specifically converts ``set`` instances to sorted lists so that audit
    entries containing sets (e.g. permission sets, tag sets) serialise to
    valid, deterministic JSON rather than to opaque string reprs.

    Args:
        obj: Any Python object.

    Returns:
        A JSON-serialisable equivalent of *obj*.
    """
    if isinstance(obj, dict):
        return {k: _make_json_safe(v) for k, v in obj.items()}
    if isinstance(obj, set):
        return sorted(_make_json_safe(v) for v in obj)
    if isinstance(obj, (list, tuple)):
        return [_make_json_safe(v) for v in obj]
    return obj


class AuditLogger:
    """Thread-safe persistent audit logger.

    Writes structured JSON audit entries to a JSONL file. Each line is
    a self-contained JSON object for easy parsing and streaming.

    Args:
        audit_dir: Directory for audit log files.
        audit_file: Name of the audit log file.
    """

    def __init__(
        self,
        audit_dir: str = DEFAULT_AUDIT_DIR,
        audit_file: str = DEFAULT_AUDIT_FILE,
        max_bytes: int = DEFAULT_AUDIT_MAX_BYTES,
        backup_count: int = DEFAULT_AUDIT_BACKUP_COUNT,
    ) -> None:
        self._audit_dir = Path(audit_dir).resolve()
        self._audit_file = audit_file
        self._max_bytes = max_bytes
        self._backup_count = backup_count
        self._lock = threading.Lock()
        self.file_path = self._audit_dir / self._audit_file
        self._ensure_dir()

    def _ensure_dir(self) -> None:
        """Create the audit log directory if it doesn't exist."""
        try:
            self._audit_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            logger.warning("Could not create audit log directory %s: %s", self._audit_dir, e)

    def log_event(
        self,
        event_type: str,
        action: str,
        outcome: str,
        agent_type: str = "",
        task_id: str = "",
        resource: str = "",
        details: dict[str, Any] | None = None,
        trace_id: str = "",
    ) -> None:
        """Write an audit entry to the log file.

        Args:
            event_type: Category of event (permission_check, guardrail_violation, etc.).
            action: The specific action taken or attempted.
            outcome: Result (allowed, denied, error).
            agent_type: The agent type involved.
            task_id: The task ID involved.
            resource: The resource being accessed.
            details: Additional context.
            trace_id: Correlation trace ID.
        """
        entry = AuditEntry(
            event_type=event_type,
            agent_type=agent_type,
            task_id=task_id,
            action=action,
            resource=redact_text(resource),
            outcome=outcome,
            details={
                **redact_value(details or {}),
                PRIVACY_ENVELOPE_KEY: privacy_receipt(
                    privacy_class=AUDIT_LOG_PRIVACY_CLASS,
                    source=f"audit:{event_type}",
                    retention_days=AUDIT_LOG_PRIVACY_RETENTION_DAYS,
                    redaction_applied=True,
                ),
            },
            trace_id=trace_id,
        )
        self._write_entry(entry)

    def log_permission_check(
        self,
        agent_type: str,
        permission: str,
        outcome: str,
        task_id: str = "",
        trace_id: str = "",
    ) -> None:
        """Log a permission check event.

        Args:
            agent_type: The agent type being checked.
            permission: The permission being requested.
            outcome: Result (allowed or denied).
            task_id: Associated task ID.
            trace_id: Correlation trace ID.
        """
        self.log_event(
            event_type="permission_check",
            action=permission,
            outcome=outcome,
            agent_type=agent_type,
            task_id=task_id,
            trace_id=trace_id,
        )

    def log_guardrail_check(
        self,
        check_type: str,
        outcome: str,
        violations: list[str] | None = None,
        agent_type: str = "",
        task_id: str = "",
        trace_id: str = "",
    ) -> None:
        """Log a guardrail check event.

        Args:
            check_type: Type of check (input or output).
            outcome: Result (allowed or denied).
            violations: List of violation descriptions.
            agent_type: The agent type involved.
            task_id: Associated task ID.
            trace_id: Correlation trace ID.
        """
        self.log_event(
            event_type="guardrail_violation" if outcome == "denied" else "guardrail_check",
            action=f"guardrail_{check_type}",
            outcome=outcome,
            agent_type=agent_type,
            task_id=task_id,
            details={"violations": violations or []},
            trace_id=trace_id,
        )

    def log_decision(
        self,
        decision_type: str,
        choice: str,
        reasoning: str,
        alternatives: list[str] | None = None,
        context: dict[str, Any] | None = None,
    ) -> None:
        """Record an automated decision made by Vetinari during execution.

        Captures the decision type, the option that was chosen, the reasoning
        behind it, and any alternatives that were considered. This gives users
        full visibility into model selection, tier classification, retry logic,
        and other automated choices.

        Valid decision_type values: "model_selection", "tier_classification",
        "plan_decomposition", "quality_gate", "retry_action",
        "cascade_escalation", "model_swap".

        Args:
            decision_type: Category of decision being logged.
            choice: The option that was selected.
            reasoning: Human-readable explanation of why this choice was made.
            alternatives: Other options that were considered but not chosen.
            context: Additional key/value data relevant to the decision.
        """
        self.log_event(
            event_type="decision",
            action=decision_type,
            outcome=choice,
            details={
                "decision_type": decision_type,
                "choice": choice,
                "reasoning": reasoning,
                "alternatives": alternatives or [],
                "context": context or {},
            },
        )

    def log_sandbox_execution(
        self,
        sandbox_type: str,
        execution_id: str,
        status: str,
        duration_ms: float = 0,
        code_length: int = 0,
        trace_id: str = "",
    ) -> None:
        """Log a sandbox execution event.

        Args:
            sandbox_type: Type of sandbox used.
            execution_id: Unique execution identifier.
            status: Execution result (success, error, timeout, blocked).
            duration_ms: Execution duration in milliseconds.
            code_length: Length of executed code.
            trace_id: Correlation trace ID.
        """
        self.log_event(
            event_type="sandbox_execution",
            action="code_execution",
            outcome=status,
            resource=sandbox_type,
            details={
                "execution_id": execution_id,
                "duration_ms": duration_ms,
                "code_length": code_length,
            },
            trace_id=trace_id,
        )

    def _write_entry(self, entry: AuditEntry) -> None:
        """Write an entry to the audit log file (thread-safe).

        Raises OSError to the caller rather than silently swallowing write
        failures — a write failure is a real error (disk full, permissions)
        and the caller must know the entry was not persisted.

        Args:
            entry: The audit entry to write.

        Raises:
            OSError: If the file cannot be written.
        """
        raw = asdict(entry)
        # Normalise non-JSON-safe types in nested structures before serialising.
        # json.dumps(default=str) converts sets to ugly string reprs; instead
        # we recursively convert sets to sorted lists so the output is both
        # valid JSON and deterministic.
        raw = _make_json_safe(raw)
        line = json.dumps(raw) + "\n"
        encoded_size = len(line.encode("utf-8"))
        with self._lock:
            self._rotate_if_needed(encoded_size)
            with Path(self.file_path).open("a", encoding="utf-8") as f:
                f.write(line)

    def _rotate_if_needed(self, incoming_bytes: int) -> None:
        if self._max_bytes <= 0 or self._backup_count <= 0 or not self.file_path.exists():
            return
        if self.file_path.stat().st_size + incoming_bytes <= self._max_bytes:
            return
        oldest = self.file_path.with_name(f"{self.file_path.name}.{self._backup_count}")
        if oldest.exists():
            oldest.unlink()
        for index in range(self._backup_count - 1, 0, -1):
            source = self.file_path.with_name(f"{self.file_path.name}.{index}")
            if source.exists():
                source.replace(self.file_path.with_name(f"{self.file_path.name}.{index + 1}"))
        self.file_path.replace(self.file_path.with_name(f"{self.file_path.name}.1"))

    def read_decisions(
        self,
        decision_type: str = "",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Read recent decision log entries from the audit log.

        Filters the full audit log to entries written by ``log_decision()``,
        optionally narrowed to a specific decision type, and returns them in
        chronological order (oldest first).

        Args:
            decision_type: If non-empty, only return decisions of this type.
            limit: Maximum number of entries to return (most recent first).

        Returns:
            List of decision entry dictionaries ordered oldest-to-newest,
            each containing ``timestamp``, ``decision_type``, ``choice``,
            ``reasoning``, ``alternatives``, and ``context``.
        """
        all_entries = self.read_entries(limit=0)  # 0 → read everything
        decisions: list[dict[str, Any]] = []
        for entry in all_entries:
            if entry.get("event_type") != "decision":
                continue
            details = entry.get("details", {})
            if decision_type and details.get("decision_type") != decision_type:
                continue
            decisions.append({
                "timestamp": entry.get("timestamp", ""),
                "decision_type": details.get("decision_type", ""),
                "choice": details.get("choice", ""),
                "reasoning": details.get("reasoning", ""),
                "alternatives": details.get("alternatives", []),
                "context": details.get("context", {}),
            })
        # Return the most recent entries
        return decisions[-limit:] if limit else decisions

    def read_entries(self, limit: int = 100) -> list[dict[str, Any]]:
        """Read recent audit entries from the log file.

        A malformed JSONL line is treated as a read failure — the caller
        receives an empty list and must handle the error rather than silently
        receiving a truncated result set. This prevents callers from acting
        on audit data that may be incomplete due to corruption.

        Args:
            limit: Maximum number of entries to return (most recent first).
                Pass 0 to return all entries.

        Returns:
            List of audit entry dictionaries, ordered oldest-to-newest,
            capped at the most recent ``limit`` entries.

        Raises:
            ValueError: If the audit log file contains a malformed JSON line.
            OSError: If the audit log file cannot be read.
        """
        entries: list[dict[str, Any]] = []
        if not self.file_path.exists():
            return entries
        with Path(self.file_path).open(encoding="utf-8") as f:
            for line_no, raw_line in enumerate(f, start=1):
                stripped = raw_line.strip()
                if stripped:
                    try:
                        entries.append(json.loads(stripped))
                    except json.JSONDecodeError as exc:
                        raise ValueError(
                            f"Malformed JSON on line {line_no} of audit log {self.file_path} — "
                            f"log may be corrupt: {exc}"
                        ) from exc
        # Return the most recent entries; limit=0 means return all
        return entries[-limit:] if limit else entries


# ── Global singleton ──────────────────────────────────────────────────────────

_audit_logger: AuditLogger | None = None
_audit_lock = threading.Lock()
# Override directory set by reset_audit_logger(new_dir=...) for test isolation.
# Protected by _audit_lock; written only inside reset_audit_logger().
_audit_logger_override_dir: str | None = None


def get_audit_logger() -> AuditLogger:
    """Get or create the global audit logger instance.

    Respects the override directory set by :func:`reset_audit_logger` so that
    tests can redirect log output without patching module internals.

    Returns:
        The AuditLogger singleton.
    """
    global _audit_logger
    if _audit_logger is None:
        with _audit_lock:
            if _audit_logger is None:
                kwargs = {}
                if _audit_logger_override_dir is not None:
                    kwargs["audit_dir"] = _audit_logger_override_dir
                _audit_logger = AuditLogger(**kwargs)
    return _audit_logger


def reset_audit_logger(new_dir: str | None = None) -> None:
    """Reset the global audit logger (for testing).

    Clears the singleton so the next call to :func:`get_audit_logger` creates
    a fresh instance. If *new_dir* is provided the next instance will write to
    that directory instead of the default, enabling tests to isolate log files.

    Args:
        new_dir: Optional path to use as the audit log directory for the next
            :func:`get_audit_logger` call.
    """
    global _audit_logger, _audit_logger_override_dir
    with _audit_lock:
        _audit_logger = None
        _audit_logger_override_dir = new_dir
