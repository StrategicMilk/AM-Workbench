"""Typed contracts for durable Workbench run/session snapshots."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

_CANONICAL_ID_RE: re.Pattern[str] = re.compile(r"[A-Za-z0-9_.-]{1,96}")
_TRAVERSAL_MARKERS: tuple[str, ...] = ("/", "\\", "..", "\x00", " ", ";")
_WINDOWS_RESERVED_NAMES: frozenset[str] = frozenset(
    {"CON", "PRN", "AUX", "NUL"} | {f"COM{index}" for index in range(1, 10)} | {f"LPT{index}" for index in range(1, 10)}
)


class SessionKernelProjectIdRejected(ValueError):
    """Raised when a project or run identifier is not path-safe."""

    def __init__(self, value: object, *, field_name: str = "project_id") -> None:
        super().__init__(f"invalid {field_name} {value!r}; use [A-Za-z0-9_.-] up to 96 characters")
        self.value = value
        self.field_name = field_name


class RunKernelError(ValueError):
    """Raised when a run kernel record cannot be trusted."""

    def __init__(self, reason: str, message: str | None = None) -> None:
        super().__init__(f"{reason}: {message}" if message else reason)
        self.reason = reason


class RunKernelStatus(str, Enum):
    """Inspectable lifecycle states for a durable Workbench run."""

    RUNNING = "running"
    INTERRUPTED = "interrupted"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    BLOCKED = "blocked"
    RECOVERY_NEEDED = "recovery_needed"


class RunStepState(str, Enum):
    """Normalized states emitted by the Workbench run handle."""

    PLANNING = "planning"
    RUNNING_MODEL = "running_model"
    EXECUTING_TOOLS = "executing_tools"
    AWAITING_APPROVAL = "awaiting_approval"
    CHECKPOINTING = "checkpointing"
    INTERRUPTED = "interrupted"
    RESUMING = "resuming"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"


class RecoveryAction(str, Enum):
    """Explicit recovery action chosen by the kernel."""

    NONE = "none"
    RESUME = "resume"
    REAP = "reap"
    ASK = "ask"
    BLOCK = "block"


@dataclass(frozen=True, slots=True)
class RunEvidenceLinks:
    """Stable links to dependency surfaces preserved across restarts."""

    trace_refs: tuple[str, ...] = ()
    eval_refs: tuple[str, ...] = ()
    repro_capsule_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_refs(self.trace_refs, "trace_refs")
        _require_refs(self.eval_refs, "eval_refs")
        _require_refs(self.repro_capsule_refs, "repro_capsule_refs")

    @classmethod
    def from_mapping(cls, payload: dict[str, Any] | None) -> RunEvidenceLinks:
        """Execute the from mapping operation.

        Returns:
            RunEvidenceLinks value produced by from_mapping().
        """
        payload = payload or {}
        return cls(
            trace_refs=tuple(str(item) for item in payload.get("trace_refs", ())),
            eval_refs=tuple(str(item) for item in payload.get("eval_refs", ())),
            repro_capsule_refs=tuple(str(item) for item in payload.get("repro_capsule_refs", ())),
        )

    def to_dict(self) -> dict[str, list[str]]:
        return {
            "trace_refs": list(self.trace_refs),
            "eval_refs": list(self.eval_refs),
            "repro_capsule_refs": list(self.repro_capsule_refs),
        }


@dataclass(frozen=True, slots=True)
class RunCheckpoint:
    """One sealed or in-progress drain checkpoint."""

    checkpoint_id: str = ""
    sealed: bool = False
    created_at_utc: str = ""
    payload_ref: str = ""
    payload_hash: str = ""

    def __post_init__(self) -> None:
        if self.sealed:
            _require_non_empty(self.checkpoint_id, "checkpoint_id")
            _require_non_empty(self.created_at_utc, "created_at_utc")
            _require_non_empty(self.payload_ref, "payload_ref")
            _require_non_empty(self.payload_hash, "payload_hash")

    @classmethod
    def from_mapping(cls, payload: dict[str, Any] | None) -> RunCheckpoint:
        """Execute the from mapping operation.

        Returns:
            RunCheckpoint value produced by from_mapping().
        """
        payload = payload or {}
        return cls(
            checkpoint_id=str(payload.get("checkpoint_id", "")),
            sealed=bool(payload.get("sealed", False)),
            created_at_utc=str(payload.get("created_at_utc", "")),
            payload_ref=str(payload.get("payload_ref", "")),
            payload_hash=str(payload.get("payload_hash", "")),
        )

    def to_dict(self) -> dict[str, str | bool]:
        return {
            "checkpoint_id": self.checkpoint_id,
            "sealed": self.sealed,
            "created_at_utc": self.created_at_utc,
            "payload_ref": self.payload_ref,
            "payload_hash": self.payload_hash,
        }

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"RunCheckpoint(checkpoint_id={self.checkpoint_id!r}, sealed={self.sealed!r}, created_at_utc={self.created_at_utc!r})"


@dataclass(frozen=True, slots=True)
class RunSessionStart:
    """Request envelope for starting or restarting a durable run."""

    run_id: str
    project_id: str = "default"
    workload_kind: str = "agent_run"
    evidence_links: RunEvidenceLinks = field(default_factory=RunEvidenceLinks)
    lease_id: str = ""
    policy_decision_ref: str = ""
    dry_run_ref: str = ""
    shadow_run_ref: str = ""
    context_manifest_ref: str = ""
    artifacts: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "project_id", canonicalize_id(self.project_id, field_name="project_id"))
        object.__setattr__(self, "run_id", canonicalize_id(self.run_id, field_name="run_id"))
        _require_non_empty(self.workload_kind, "workload_kind")
        _require_refs(self.artifacts, "artifacts")

    @classmethod
    def from_mapping(cls, payload: dict[str, Any]) -> RunSessionStart:
        """Execute the from mapping operation.

        Returns:
            RunSessionStart value produced by from_mapping().

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        if not isinstance(payload, dict):
            raise RunKernelError("bad-start-payload", "start payload must be an object")
        return cls(
            project_id=str(payload.get("project_id", "default")),
            run_id=str(payload.get("run_id", "")),
            workload_kind=str(payload.get("workload_kind", "agent_run")),
            evidence_links=RunEvidenceLinks.from_mapping(payload.get("evidence_links")),
            lease_id=str(payload.get("lease_id", "")),
            policy_decision_ref=str(payload.get("policy_decision_ref", "")),
            dry_run_ref=str(payload.get("dry_run_ref", "")),
            shadow_run_ref=str(payload.get("shadow_run_ref", "")),
            context_manifest_ref=str(payload.get("context_manifest_ref", "")),
            artifacts=tuple(str(item) for item in payload.get("artifacts", ())),
        )

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"RunSessionStart(run_id={self.run_id!r}, project_id={self.project_id!r}, workload_kind={self.workload_kind!r})"


@dataclass(frozen=True, slots=True)
class RunHandle:
    """Rejoinable run handle exposed to Workbench callers."""

    project_id: str
    run_id: str
    stream_id: str
    status: RunKernelStatus
    step_state: RunStepState
    can_stop: bool
    can_checkpoint: bool
    can_resume: bool
    can_wait: bool
    can_read_result: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "project_id", canonicalize_id(self.project_id, field_name="project_id"))
        object.__setattr__(self, "run_id", canonicalize_id(self.run_id, field_name="run_id"))
        _require_non_empty(self.stream_id, "stream_id")
        object.__setattr__(self, "status", RunKernelStatus(self.status))
        object.__setattr__(self, "step_state", RunStepState(self.step_state))

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "run_id": self.run_id,
            "stream_id": self.stream_id,
            "status": self.status.value,
            "step_state": self.step_state.value,
            "can_stop": self.can_stop,
            "can_checkpoint": self.can_checkpoint,
            "can_resume": self.can_resume,
            "can_wait": self.can_wait,
            "can_read_result": self.can_read_result,
        }

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"RunHandle(project_id={self.project_id!r}, run_id={self.run_id!r}, stream_id={self.stream_id!r})"


@dataclass(frozen=True, slots=True)
class RunEventRecord:
    """One durable, ordered event in a run stream."""

    sequence: int
    stream_id: str
    run_id: str
    event_type: str
    status: str
    occurred_at_utc: str
    detail: str = ""

    def __post_init__(self) -> None:
        if self.sequence < 1:
            raise RunKernelError("event-sequence-invalid", "sequence must be positive")
        for field_name in ("stream_id", "run_id", "event_type", "status", "occurred_at_utc"):
            _require_non_empty(getattr(self, field_name), field_name)

    def to_dict(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "stream_id": self.stream_id,
            "run_id": self.run_id,
            "event_type": self.event_type,
            "status": self.status,
            "occurred_at_utc": self.occurred_at_utc,
            "detail": self.detail,
        }

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"RunEventRecord(sequence={self.sequence!r}, stream_id={self.stream_id!r}, run_id={self.run_id!r})"


@dataclass(frozen=True, slots=True)
class RunStreamReplay:
    """Result of subscribing or rejoining a bounded run event stream."""

    status: str
    stream_id: str
    events: tuple[RunEventRecord, ...]
    reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_non_empty(self.status, "status")
        _require_non_empty(self.stream_id, "stream_id")
        _require_refs(self.reasons, "reasons")

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "stream_id": self.stream_id,
            "events": [event.to_dict() for event in self.events],
            "reasons": list(self.reasons),
        }

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"RunStreamReplay(status={self.status!r}, stream_id={self.stream_id!r}, events={self.events!r})"


@dataclass(frozen=True, slots=True)
class RunStepReceipt:
    """Idempotent receipt reference for an agent-loop workflow step."""

    step_id: str
    attempt_id: str
    idempotency_key: str
    step_state: RunStepState
    receipt_ref: str

    def __post_init__(self) -> None:
        for field_name in ("step_id", "attempt_id", "idempotency_key", "receipt_ref"):
            _require_non_empty(getattr(self, field_name), field_name)
        object.__setattr__(self, "step_state", RunStepState(self.step_state))

    @property
    def event_token(self) -> str:
        return (
            "step-receipt:"
            f"{self.step_id}:{self.attempt_id}:{self.idempotency_key}:"
            f"{self.step_state.value}:{self.receipt_ref}"
        )

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"RunStepReceipt(step_id={self.step_id!r}, attempt_id={self.attempt_id!r})"


@dataclass(frozen=True, slots=True)
class RunSessionSnapshot:
    """Durable JSON snapshot for one Workbench run."""

    project_id: str
    run_id: str
    workload_kind: str
    status: RunKernelStatus
    created_at_utc: str
    updated_at_utc: str
    heartbeat_at_utc: str
    evidence_links: RunEvidenceLinks
    checkpoint: RunCheckpoint = field(default_factory=RunCheckpoint)
    recovery_action: RecoveryAction = RecoveryAction.NONE
    restart_count: int = 0
    lease_id: str = ""
    policy_decision_ref: str = ""
    dry_run_ref: str = ""
    shadow_run_ref: str = ""
    context_manifest_ref: str = ""
    artifacts: tuple[str, ...] = ()
    final_verdict_ref: str = ""
    events: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "project_id", canonicalize_id(self.project_id, field_name="project_id"))
        object.__setattr__(self, "run_id", canonicalize_id(self.run_id, field_name="run_id"))
        _require_non_empty(self.workload_kind, "workload_kind")
        _require_non_empty(self.created_at_utc, "created_at_utc")
        _require_non_empty(self.updated_at_utc, "updated_at_utc")
        _require_non_empty(self.heartbeat_at_utc, "heartbeat_at_utc")
        if self.restart_count < 0:
            raise RunKernelError("restart-count-invalid", "restart_count must be >= 0")
        _require_refs(self.artifacts, "artifacts")

    @classmethod
    def from_mapping(cls, payload: dict[str, Any]) -> RunSessionSnapshot:
        """Execute the from mapping operation.

        Returns:
            RunSessionSnapshot value produced by from_mapping().

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        if not isinstance(payload, dict):
            raise RunKernelError("snapshot-not-object")
        if int(payload.get("schema_version", 0)) != 1:
            raise RunKernelError("snapshot-schema-unsupported")
        return cls(
            project_id=str(payload.get("project_id", "")),
            run_id=str(payload.get("run_id", "")),
            workload_kind=str(payload.get("workload_kind", "")),
            status=RunKernelStatus(str(payload.get("status", ""))),
            created_at_utc=str(payload.get("created_at_utc", "")),
            updated_at_utc=str(payload.get("updated_at_utc", "")),
            heartbeat_at_utc=str(payload.get("heartbeat_at_utc", "")),
            recovery_action=RecoveryAction(str(payload.get("recovery_action", RecoveryAction.NONE.value))),
            restart_count=int(payload.get("restart_count", 0)),
            lease_id=str(payload.get("lease_id", "")),
            policy_decision_ref=str(payload.get("policy_decision_ref", "")),
            dry_run_ref=str(payload.get("dry_run_ref", "")),
            shadow_run_ref=str(payload.get("shadow_run_ref", "")),
            context_manifest_ref=str(payload.get("context_manifest_ref", "")),
            artifacts=tuple(str(item) for item in payload.get("artifacts", ())),
            final_verdict_ref=str(payload.get("final_verdict_ref", "")),
            evidence_links=RunEvidenceLinks.from_mapping(payload.get("evidence_links")),
            checkpoint=RunCheckpoint.from_mapping(payload.get("checkpoint")),
            events=tuple(str(item) for item in payload.get("events", ())),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "project_id": self.project_id,
            "run_id": self.run_id,
            "workload_kind": self.workload_kind,
            "status": self.status.value,
            "created_at_utc": self.created_at_utc,
            "updated_at_utc": self.updated_at_utc,
            "heartbeat_at_utc": self.heartbeat_at_utc,
            "recovery_action": self.recovery_action.value,
            "restart_count": self.restart_count,
            "lease_id": self.lease_id,
            "policy_decision_ref": self.policy_decision_ref,
            "dry_run_ref": self.dry_run_ref,
            "shadow_run_ref": self.shadow_run_ref,
            "context_manifest_ref": self.context_manifest_ref,
            "artifacts": list(self.artifacts),
            "final_verdict_ref": self.final_verdict_ref,
            "evidence_links": self.evidence_links.to_dict(),
            "checkpoint": self.checkpoint.to_dict(),
            "events": list(self.events),
        }

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"RunSessionSnapshot(project_id={self.project_id!r}, run_id={self.run_id!r}, workload_kind={self.workload_kind!r})"


@dataclass(frozen=True, slots=True)
class RunKernelResult:
    """Typed return for every run-kernel operation."""

    status: RunKernelStatus
    recovery_action: RecoveryAction
    reasons: tuple[str, ...]
    snapshot: RunSessionSnapshot | None = None

    @property
    def ok(self) -> bool:
        return self.status in {
            RunKernelStatus.RUNNING,
            RunKernelStatus.INTERRUPTED,
            RunKernelStatus.SUCCEEDED,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "recovery_action": self.recovery_action.value,
            "reasons": list(self.reasons),
            "snapshot": self.snapshot.to_dict() if self.snapshot else None,
        }

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"RunKernelResult(status={self.status!r}, recovery_action={self.recovery_action!r}, reasons={self.reasons!r})"


def canonicalize_id(value: str | None, *, field_name: str) -> str:
    """Return a path-safe id or fail closed before file access.

    Returns:
        str value produced by canonicalize_id().

    Raises:
        Exception: Propagates validation or runtime failures from the underlying operation.
    """
    if not isinstance(value, str):
        raise SessionKernelProjectIdRejected(value, field_name=field_name)
    if not value or len(value) > 96 or _CANONICAL_ID_RE.fullmatch(value) is None:
        raise SessionKernelProjectIdRejected(value, field_name=field_name)
    if any(marker in value for marker in _TRAVERSAL_MARKERS):
        raise SessionKernelProjectIdRejected(value, field_name=field_name)
    stem = value.split(".", 1)[0].upper()
    if value == "." or stem in _WINDOWS_RESERVED_NAMES:
        raise SessionKernelProjectIdRejected(value, field_name=field_name)
    return value


def _require_non_empty(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise RunKernelError(f"{field_name}-missing", f"{field_name} must be non-empty")


def _require_refs(values: tuple[str, ...], field_name: str) -> None:
    for value in values:
        if not isinstance(value, str) or not value.strip():
            raise RunKernelError(f"{field_name}-invalid", "reference values must be non-empty strings")


__all__ = [
    "RecoveryAction",
    "RunCheckpoint",
    "RunEventRecord",
    "RunEvidenceLinks",
    "RunHandle",
    "RunKernelError",
    "RunKernelResult",
    "RunKernelStatus",
    "RunSessionSnapshot",
    "RunSessionStart",
    "RunStepReceipt",
    "RunStepState",
    "RunStreamReplay",
    "SessionKernelProjectIdRejected",
    "canonicalize_id",
]
