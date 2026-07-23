"""Data contracts for Workbench shadow snapshots and rollback attempts."""

from __future__ import annotations

import base64
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any

from vetinari.workbench.spine_consumers import record_asset_written


class ShadowSnapshotError(ValueError):
    """Fail-closed signal for invalid shadow snapshot or rollback data."""


class ShadowOperationKind(str, Enum):
    """Operation types that can be captured before execution."""

    FILE_EDIT = "file_edit"
    COMMAND = "command"
    PROCESS_START = "process_start"
    AUTOMATION = "automation"
    POLICY_VERDICT = "policy_verdict"
    USER_APPROVAL = "user_approval"


class Reversibility(str, Enum):
    """Whether an operation can be undone by this package."""

    REVERSIBLE = "reversible"
    MANUAL_RECOVERY_REQUIRED = "manual_recovery_required"
    IRREVERSIBLE = "irreversible"
    UNKNOWN = "unknown"


class RollbackStrategy(str, Enum):
    """Rollback strategy selected before the represented action executes."""

    FILE_RESTORE = "file_restore"
    MANUAL_RECOVERY = "manual_recovery"
    IRREVERSIBLE_REFUSAL = "irreversible_refusal"


class ShadowRollbackStatus(str, Enum):
    """Result of a rollback or recovery attempt."""

    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED_IRREVERSIBLE = "skipped_irreversible"
    MANUAL_RECOVERY_REQUIRED = "manual_recovery_required"


class UndoabilityStatus(str, Enum):
    """Mission Control undo posture for one captured operation."""

    UNDOABLE = "undoable"
    MANUAL_RECOVERY = "manual_recovery"
    IRREVERSIBLE = "irreversible"
    UNAVAILABLE = "unavailable"
    ROLLED_BACK = "rolled_back"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class ShadowOperation:
    """Before-execution operation data, never an executable action."""

    operation_id: str
    run_id: str
    kind: ShadowOperationKind
    summary: str
    risk_domain: str
    policy_verdict_ref: str
    approval_ref: str
    dry_run_evidence_ref: str
    shield_decision_ref: str = ""
    command_text: str = ""
    cwd_ref: str = ""
    canonical_path: str = ""
    before_sha256: str = ""
    before_content_b64: str = ""
    after_sha256: str = ""
    diff_ref: str = ""
    process_ref: str = ""
    automation_shadow_plan_ref: str = ""
    automation_shadow_receipt_ref: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_text(self.operation_id, "operation_id")
        _require_text(self.run_id, "run_id")
        _require_enum(self.kind, ShadowOperationKind, "kind")
        _require_text(self.summary, "summary")
        _require_text(self.risk_domain, "risk_domain")
        _require_text(self.policy_verdict_ref, "policy_verdict_ref")
        _require_text(self.approval_ref, "approval_ref")
        _require_text(self.dry_run_evidence_ref, "dry_run_evidence_ref")
        if self.kind is ShadowOperationKind.FILE_EDIT:
            _require_text(self.canonical_path, "canonical_path")
            _require_text(self.before_sha256, "before_sha256")
            _require_text(self.before_content_b64, "before_content_b64")
        if self.kind is ShadowOperationKind.COMMAND:
            _require_text(self.command_text, "command_text")
            _require_text(self.cwd_ref, "cwd_ref")
        if self.kind is ShadowOperationKind.PROCESS_START:
            _require_text(self.process_ref, "process_ref")
        if self.kind is ShadowOperationKind.AUTOMATION:
            _require_text(self.automation_shadow_plan_ref, "automation_shadow_plan_ref")
            _require_text(self.automation_shadow_receipt_ref, "automation_shadow_receipt_ref")

    def before_bytes(self) -> bytes:
        """Return the captured pre-execution bytes for a file edit.

        Returns:
            bytes value produced by before_bytes().

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        if self.kind is not ShadowOperationKind.FILE_EDIT:
            raise ShadowSnapshotError("before bytes are only available for file edits")
        return base64.b64decode(self.before_content_b64.encode("ascii"), validate=True)

    def to_dict(self) -> dict[str, Any]:
        """Serialize this operation for schema validation and JSONL storage."""
        return _jsonable(asdict(self))

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"ShadowOperation(operation_id={self.operation_id!r}, run_id={self.run_id!r}, kind={self.kind!r})"


@dataclass(frozen=True, slots=True)
class ShadowRollbackPlan:
    """Precomputed rollback or recovery plan for one operation."""

    plan_id: str
    operation_id: str
    reversibility: Reversibility
    strategy: RollbackStrategy
    target_path: str = ""
    restore_sha256: str = ""
    restore_content_b64: str = ""
    expected_current_sha256: str = ""
    manual_recovery_guidance: str = ""
    refusal_reason: str = ""
    evidence_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_text(self.plan_id, "plan_id")
        _require_text(self.operation_id, "operation_id")
        _require_enum(self.reversibility, Reversibility, "reversibility")
        _require_enum(self.strategy, RollbackStrategy, "strategy")
        if self.reversibility is Reversibility.UNKNOWN:
            raise ShadowSnapshotError("unknown reversibility fails closed")
        if self.strategy is RollbackStrategy.FILE_RESTORE:
            _require_text(self.target_path, "target_path")
            _require_text(self.restore_sha256, "restore_sha256")
            _require_text(self.restore_content_b64, "restore_content_b64")
        if self.strategy is RollbackStrategy.MANUAL_RECOVERY:
            _require_text(self.manual_recovery_guidance, "manual_recovery_guidance")
        if self.strategy is RollbackStrategy.IRREVERSIBLE_REFUSAL:
            _require_text(self.refusal_reason, "refusal_reason")
        _require_string_tuple(self.evidence_refs, "evidence_refs", allow_empty=True)

    def restore_bytes(self) -> bytes:
        """Return bytes that a reversible file restore writes.

        Returns:
            bytes value produced by restore_bytes().

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        if self.strategy is not RollbackStrategy.FILE_RESTORE:
            raise ShadowSnapshotError("restore bytes are only available for file restore plans")
        return base64.b64decode(self.restore_content_b64.encode("ascii"), validate=True)

    def to_dict(self) -> dict[str, Any]:
        """Serialize this rollback plan."""
        return _jsonable(asdict(self))

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"ShadowRollbackPlan(plan_id={self.plan_id!r}, operation_id={self.operation_id!r}, reversibility={self.reversibility!r})"


@dataclass(frozen=True, slots=True)
class ShadowSnapshot:
    """Immutable before-execution snapshot for one risky operation."""

    snapshot_id: str
    run_id: str
    operation: ShadowOperation
    reversibility: Reversibility
    rollback_plan: ShadowRollbackPlan
    original_run_record_ref: str
    original_run_record_hash: str
    captured_at_utc: str
    original_run_record_payload: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_text(self.snapshot_id, "snapshot_id")
        _require_text(self.run_id, "run_id")
        if not isinstance(self.operation, ShadowOperation):
            raise ShadowSnapshotError("operation must be ShadowOperation")
        _require_enum(self.reversibility, Reversibility, "reversibility")
        if self.reversibility is Reversibility.UNKNOWN:
            raise ShadowSnapshotError("unknown reversibility fails closed")
        if not isinstance(self.rollback_plan, ShadowRollbackPlan):
            raise ShadowSnapshotError("rollback_plan must be ShadowRollbackPlan")
        if self.run_id != self.operation.run_id:
            raise ShadowSnapshotError("snapshot run_id must match operation run_id")
        if self.operation.operation_id != self.rollback_plan.operation_id:
            raise ShadowSnapshotError("rollback plan operation_id must match operation")
        if self.reversibility is not self.rollback_plan.reversibility:
            raise ShadowSnapshotError("snapshot reversibility must match rollback plan")
        _require_text(self.original_run_record_ref, "original_run_record_ref")
        _require_text(self.original_run_record_hash, "original_run_record_hash")
        _require_text(self.captured_at_utc, "captured_at_utc")
        if self.reversibility is Reversibility.REVERSIBLE:
            _validate_reversible_file_snapshot(self.operation, self.rollback_plan)
        if self.reversibility is Reversibility.MANUAL_RECOVERY_REQUIRED:
            _require_text(self.rollback_plan.manual_recovery_guidance, "manual_recovery_guidance")
        if self.operation.kind is ShadowOperationKind.PROCESS_START and self.reversibility is Reversibility.REVERSIBLE:
            raise ShadowSnapshotError("process starts cannot be marked reversible")

    def to_dict(self) -> dict[str, Any]:
        """Serialize this snapshot for schema validation and JSONL storage.

        Returns:
            JSON-compatible snapshot payload.
        """
        # spine_consumers invokes get_spine() and absorbs observability failures.
        record_asset_written(
            asset_id=self.snapshot_id,
            kind="tool",
            project_id="default",
        )
        return _jsonable(asdict(self))

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"ShadowSnapshot(snapshot_id={self.snapshot_id!r}, run_id={self.run_id!r}, operation={self.operation!r})"


@dataclass(frozen=True, slots=True)
class RollbackAttempt:
    """Append-only record of one rollback or recovery attempt."""

    attempt_id: str
    snapshot_id: str
    operation_id: str
    status: ShadowRollbackStatus
    attempted_at_utc: str
    restored_path: str = ""
    before_target_sha256: str = ""
    after_target_sha256: str = ""
    error_code: str = ""
    error_message: str = ""
    recovery_guidance: str = ""

    def __post_init__(self) -> None:
        _require_text(self.attempt_id, "attempt_id")
        _require_text(self.snapshot_id, "snapshot_id")
        _require_text(self.operation_id, "operation_id")
        _require_enum(self.status, ShadowRollbackStatus, "status")
        _require_text(self.attempted_at_utc, "attempted_at_utc")
        if self.status is ShadowRollbackStatus.SUCCESS:
            _require_text(self.restored_path, "restored_path")
            _require_text(self.after_target_sha256, "after_target_sha256")
        if self.status is ShadowRollbackStatus.FAILED:
            _require_text(self.error_code, "error_code")
            _require_text(self.error_message, "error_message")
            _require_text(self.recovery_guidance, "recovery_guidance")
        if self.status in {ShadowRollbackStatus.SKIPPED_IRREVERSIBLE, ShadowRollbackStatus.MANUAL_RECOVERY_REQUIRED}:
            _require_text(self.recovery_guidance, "recovery_guidance")

    def to_dict(self) -> dict[str, Any]:
        """Serialize this rollback attempt."""
        return _jsonable(asdict(self))

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"RollbackAttempt(attempt_id={self.attempt_id!r}, snapshot_id={self.snapshot_id!r}, operation_id={self.operation_id!r})"


@dataclass(frozen=True, slots=True)
class MissionControlUndoItem:
    """Data-only Mission Control projection for shadow snapshot undo."""

    snapshot_id: str
    run_id: str
    operation_id: str
    operation_kind: ShadowOperationKind
    undoability_status: UndoabilityStatus
    label: str
    risk_domain: str
    policy_verdict_ref: str
    approval_ref: str
    original_run_record_ref: str
    reason_code: str
    rollback_status: str = ""
    recovery_guidance: str = ""

    def __post_init__(self) -> None:
        _require_text(self.snapshot_id, "snapshot_id")
        _require_text(self.run_id, "run_id")
        _require_text(self.operation_id, "operation_id")
        _require_enum(self.operation_kind, ShadowOperationKind, "operation_kind")
        _require_enum(self.undoability_status, UndoabilityStatus, "undoability_status")
        _require_text(self.label, "label")
        _require_text(self.risk_domain, "risk_domain")
        _require_text(self.policy_verdict_ref, "policy_verdict_ref")
        _require_text(self.approval_ref, "approval_ref")
        _require_text(self.original_run_record_ref, "original_run_record_ref")
        _require_text(self.reason_code, "reason_code")
        if self.undoability_status in {
            UndoabilityStatus.MANUAL_RECOVERY,
            UndoabilityStatus.IRREVERSIBLE,
            UndoabilityStatus.UNAVAILABLE,
            UndoabilityStatus.FAILED,
        }:
            _require_text(self.recovery_guidance, "recovery_guidance")

    def to_dict(self) -> dict[str, Any]:
        """Serialize this projection row."""
        return _jsonable(asdict(self))

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"MissionControlUndoItem(snapshot_id={self.snapshot_id!r}, run_id={self.run_id!r}, operation_id={self.operation_id!r})"


def _validate_reversible_file_snapshot(operation: ShadowOperation, plan: ShadowRollbackPlan) -> None:
    if operation.kind is not ShadowOperationKind.FILE_EDIT:
        raise ShadowSnapshotError("only file edits can be reversible")
    if plan.strategy is not RollbackStrategy.FILE_RESTORE:
        raise ShadowSnapshotError("reversible file edits require a file restore plan")
    if plan.target_path != operation.canonical_path:
        raise ShadowSnapshotError("rollback target_path must match operation canonical_path")
    if plan.restore_sha256 != operation.before_sha256:
        raise ShadowSnapshotError("rollback restore_sha256 must match captured before_sha256")
    if plan.restore_content_b64 != operation.before_content_b64:
        raise ShadowSnapshotError("rollback restore_content_b64 must match captured before_content_b64")


def _require_text(value: object, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ShadowSnapshotError(f"{field_name} must be non-empty")


def _require_enum(value: object, enum_type: type[Enum], field_name: str) -> None:
    if not isinstance(value, enum_type):
        raise ShadowSnapshotError(f"{field_name} must be {enum_type.__name__}")


def _require_string_tuple(values: tuple[str, ...], field_name: str, *, allow_empty: bool = False) -> None:
    if not isinstance(values, tuple) or (not allow_empty and not values):
        raise ShadowSnapshotError(f"{field_name} must be a non-empty tuple")
    if any(not isinstance(value, str) or not value.strip() for value in values):
        raise ShadowSnapshotError(f"{field_name} must contain non-empty strings")


def _jsonable(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, tuple | list):
        return [_jsonable(item) for item in value]
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    return value
