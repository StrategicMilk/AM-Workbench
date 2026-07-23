"""Runtime helpers for Workbench shadow snapshots and rollback attempts."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import threading
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from vetinari.learning.atomic_writers import write_bytes_atomic
from vetinari.workbench.shadow_undo.contracts import (
    MissionControlUndoItem,
    Reversibility,
    RollbackAttempt,
    RollbackStrategy,
    ShadowOperation,
    ShadowOperationKind,
    ShadowRollbackPlan,
    ShadowRollbackStatus,
    ShadowSnapshot,
    ShadowSnapshotError,
    UndoabilityStatus,
)
from vetinari.workbench.spine_consumers import record_asset_written, record_trace_written

logger = logging.getLogger(__name__)


class ShadowSnapshotStore:
    """Append-only JSONL store for snapshots and rollback attempts."""

    def __init__(self, *, project_root: Path, store_path: str | Path = ".workbench-shadow-undo") -> None:
        self.project_root = Path(project_root).resolve()
        self.store_dir = self._resolve_store_path(store_path)
        self._lock = threading.RLock()

    def append_snapshot(self, snapshot: ShadowSnapshot) -> None:
        """Persist one immutable snapshot record."""
        self._append_jsonl(self.store_dir / "snapshots.jsonl", snapshot.to_dict())

    def append_rollback_attempt(self, attempt: RollbackAttempt) -> None:
        """Persist one rollback attempt record without rewriting snapshots."""
        self._append_jsonl(self.store_dir / "rollback_attempts.jsonl", attempt.to_dict())

    def read_snapshots(self) -> tuple[ShadowSnapshot, ...]:
        """Return raw snapshot dictionaries from the append-only store."""
        return tuple(_snapshot_from_dict(row) for row in self._read_jsonl(self.store_dir / "snapshots.jsonl"))

    def read_rollback_attempts(self) -> tuple[RollbackAttempt, ...]:
        """Return rollback attempts from the append-only store."""
        return tuple(_attempt_from_dict(row) for row in self._read_jsonl(self.store_dir / "rollback_attempts.jsonl"))

    def resolve_target_path(self, path: str | Path) -> Path:
        """Resolve a target path under the project root and reject traversal.

        Returns:
            Resolved target path value.

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        candidate = Path(path)
        if candidate.is_absolute():
            raise ShadowSnapshotError("absolute target paths are rejected")
        if any(part == ".." for part in candidate.parts):
            raise ShadowSnapshotError("target path traversal is rejected")
        resolved = (self.project_root / candidate).resolve()
        if not resolved.is_relative_to(self.project_root):
            raise ShadowSnapshotError("target path escapes project root")
        return resolved

    def _resolve_store_path(self, store_path: str | Path) -> Path:
        candidate = Path(store_path)
        if candidate.is_absolute():
            raise ShadowSnapshotError("absolute snapshot store paths are rejected")
        if any(part == ".." for part in candidate.parts):
            raise ShadowSnapshotError("snapshot store traversal is rejected")
        resolved = (self.project_root / candidate).resolve()
        if not resolved.is_relative_to(self.project_root):
            raise ShadowSnapshotError("snapshot store path escapes project root")
        return resolved

    def _append_jsonl(self, path: Path, payload: Mapping[str, Any]) -> None:
        self.store_dir.mkdir(parents=True, exist_ok=True)
        line = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        with self._lock, path.open("a", encoding="utf-8") as handle:
            handle.write(f"{line}\n")
            handle.flush()
        # spine_consumers invokes get_spine() and absorbs observability failures.
        record_trace_written(
            trace_id=str(payload.get("snapshot_id") or payload.get("attempt_id") or "shadow-undo"),
            query_hash="shadow_undo",
            project_id="default",
        )

    def _read_jsonl(self, path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        with self._lock, path.open("r", encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]


def capture_shadow_snapshot(
    store: ShadowSnapshotStore,
    *,
    run_id: str,
    operation_id: str,
    operation_kind: ShadowOperationKind,
    summary: str,
    risk_domain: str,
    policy_verdict_ref: str,
    approval_ref: str,
    dry_run_evidence_ref: str,
    original_run_record_ref: str,
    original_run_record_payload: Mapping[str, Any],
    target_path: str | Path = "",
    command_text: str = "",
    cwd_ref: str = "",
    process_ref: str = "",
    automation_shadow_plan_ref: str = "",
    automation_shadow_receipt_ref: str = "",
    shield_decision_ref: str = "",
    manual_recovery_guidance: str = "",
    captured_at_utc: str | None = None,
) -> ShadowSnapshot:
    """Capture a data-only snapshot before the represented operation runs.

    Returns:
        ShadowSnapshot value produced by capture_shadow_snapshot().

    Raises:
        Exception: Propagates validation or runtime failures from the underlying operation.
    """
    _require_store(store)
    _require_text(original_run_record_ref, "original_run_record_ref")
    if not original_run_record_payload:
        raise ShadowSnapshotError("original_run_record_payload must be non-empty")
    operation_kind = ShadowOperationKind(operation_kind)
    operation = _snapshot_operation(
        store=store,
        run_id=run_id,
        operation_id=operation_id,
        operation_kind=operation_kind,
        summary=summary,
        risk_domain=risk_domain,
        policy_verdict_ref=policy_verdict_ref,
        approval_ref=approval_ref,
        dry_run_evidence_ref=dry_run_evidence_ref,
        target_path=target_path,
        command_text=command_text,
        cwd_ref=cwd_ref,
        process_ref=process_ref,
        automation_shadow_plan_ref=automation_shadow_plan_ref,
        automation_shadow_receipt_ref=automation_shadow_receipt_ref,
        shield_decision_ref=shield_decision_ref,
    )
    reversibility = _default_reversibility(operation_kind, manual_recovery_guidance)
    rollback_plan = _rollback_plan_for(operation, reversibility, manual_recovery_guidance)
    snapshot = ShadowSnapshot(
        snapshot_id=_stable_id("shadow-snapshot", run_id, operation_id, _sha256_json(original_run_record_payload)),
        run_id=run_id,
        operation=operation,
        reversibility=reversibility,
        rollback_plan=rollback_plan,
        original_run_record_ref=original_run_record_ref,
        original_run_record_hash=_sha256_json(original_run_record_payload),
        original_run_record_payload=dict(original_run_record_payload),
        captured_at_utc=captured_at_utc or _utc_now(),
    )
    store.append_snapshot(snapshot)
    return snapshot


def _snapshot_operation(
    *,
    store: ShadowSnapshotStore,
    run_id: str,
    operation_id: str,
    operation_kind: ShadowOperationKind,
    summary: str,
    risk_domain: str,
    policy_verdict_ref: str,
    approval_ref: str,
    dry_run_evidence_ref: str,
    target_path: str | Path,
    command_text: str,
    cwd_ref: str,
    process_ref: str,
    automation_shadow_plan_ref: str,
    automation_shadow_receipt_ref: str,
    shield_decision_ref: str,
) -> ShadowOperation:
    canonical_target = ""
    before_hash = ""
    before_b64 = ""
    if operation_kind is ShadowOperationKind.FILE_EDIT:
        target = store.resolve_target_path(target_path)
        if not target.is_file():
            raise ShadowSnapshotError("reversible file edit target must exist before capture")
        before_bytes = target.read_bytes()
        canonical_target = str(Path(target_path).as_posix())
        before_hash = _sha256_bytes(before_bytes)
        before_b64 = base64.b64encode(before_bytes).decode("ascii")
    return ShadowOperation(
        operation_id=operation_id,
        run_id=run_id,
        kind=operation_kind,
        summary=summary,
        risk_domain=risk_domain,
        policy_verdict_ref=policy_verdict_ref,
        approval_ref=approval_ref,
        dry_run_evidence_ref=dry_run_evidence_ref,
        shield_decision_ref=shield_decision_ref,
        command_text=command_text,
        cwd_ref=cwd_ref,
        canonical_path=canonical_target,
        before_sha256=before_hash,
        before_content_b64=before_b64,
        process_ref=process_ref,
        automation_shadow_plan_ref=automation_shadow_plan_ref,
        automation_shadow_receipt_ref=automation_shadow_receipt_ref,
    )


def rollback_shadow_snapshot(
    store: ShadowSnapshotStore,
    snapshot: ShadowSnapshot,
    *,
    expected_current_sha256: str = "",
) -> RollbackAttempt:
    """Attempt rollback for one snapshot and append a separate attempt record.

    Args:
        store: Store value consumed by rollback_shadow_snapshot().
        snapshot: Snapshot value consumed by rollback_shadow_snapshot().
        expected_current_sha256: Expected current sha256 value consumed by rollback_shadow_snapshot().

    Returns:
        RollbackAttempt value produced by rollback_shadow_snapshot().

    Raises:
        Exception: Propagates validation or runtime failures from the underlying operation.
    """
    _require_store(store)
    if not isinstance(snapshot, ShadowSnapshot):
        raise ShadowSnapshotError("snapshot must be ShadowSnapshot")
    with store._lock:
        if snapshot.reversibility is Reversibility.REVERSIBLE:
            attempt = _restore_file_snapshot(store, snapshot, expected_current_sha256=expected_current_sha256)
        elif snapshot.reversibility is Reversibility.MANUAL_RECOVERY_REQUIRED:
            attempt = _manual_recovery_attempt(snapshot)
        else:
            attempt = _irreversible_attempt(snapshot)
        store.append_rollback_attempt(attempt)
        return attempt


def build_mission_control_undo_items(
    snapshots: Iterable[ShadowSnapshot],
    attempts: Iterable[RollbackAttempt] = (),
) -> tuple[MissionControlUndoItem, ...]:
    """Build data-only Mission Control undo rows without route or UI wiring.

    Args:
        snapshots: Snapshots value consumed by build_mission_control_undo_items().
        attempts: Attempts value consumed by build_mission_control_undo_items().

    Returns:
        Newly constructed mission control undo items value.
    """
    latest_attempts: dict[str, RollbackAttempt] = {attempt.snapshot_id: attempt for attempt in attempts}
    return tuple(_mission_control_item(snapshot, latest_attempts.get(snapshot.snapshot_id)) for snapshot in snapshots)


def _restore_file_snapshot(
    store: ShadowSnapshotStore,
    snapshot: ShadowSnapshot,
    *,
    expected_current_sha256: str = "",
) -> RollbackAttempt:
    plan = snapshot.rollback_plan
    target = store.resolve_target_path(plan.target_path)
    before_target_hash = ""
    try:
        before_target_hash = _sha256_path(target) if target.exists() else ""
        expected_hash = expected_current_sha256.strip() or plan.expected_current_sha256.strip()
        if not expected_hash:
            raise ShadowSnapshotError("expected current sha256 is required before rollback")
        if before_target_hash != expected_hash:
            raise ShadowSnapshotError("rollback target changed since expected current sha256")
        restore_bytes = plan.restore_bytes()
        if _sha256_bytes(restore_bytes) != plan.restore_sha256:
            raise ShadowSnapshotError("rollback restore bytes do not match restore_sha256")
        if snapshot.operation.before_sha256 != plan.restore_sha256:
            raise ShadowSnapshotError("snapshot before hash does not match rollback plan")
        write_bytes_atomic(target, restore_bytes)
        # spine_consumers invokes get_spine() and absorbs observability failures.
        record_asset_written(
            asset_id=f"shadow-undo-restore-{snapshot.snapshot_id}",
            kind="tool",
            project_id="default",
            path=str(target),
            redact_fields=["path"],
        )
    except Exception as exc:
        logger.warning("Handled recoverable failure before fallback.", exc_info=True)
        return RollbackAttempt(
            attempt_id=_stable_id("rollback-attempt", snapshot.snapshot_id, _utc_now(), "failed"),
            snapshot_id=snapshot.snapshot_id,
            operation_id=snapshot.operation.operation_id,
            status=ShadowRollbackStatus.FAILED,
            attempted_at_utc=_utc_now(),
            before_target_sha256=before_target_hash,
            error_code=type(exc).__name__,
            error_message=str(exc),
            recovery_guidance="Manual recovery required: restore from original snapshot before_content_b64.",
        )
    return RollbackAttempt(
        attempt_id=_stable_id("rollback-attempt", snapshot.snapshot_id, _utc_now(), "success"),
        snapshot_id=snapshot.snapshot_id,
        operation_id=snapshot.operation.operation_id,
        status=ShadowRollbackStatus.SUCCESS,
        attempted_at_utc=_utc_now(),
        restored_path=plan.target_path,
        before_target_sha256=before_target_hash,
        after_target_sha256=_sha256_path(target),
    )


def _manual_recovery_attempt(snapshot: ShadowSnapshot) -> RollbackAttempt:
    return RollbackAttempt(
        attempt_id=_stable_id("rollback-attempt", snapshot.snapshot_id, _utc_now(), "manual"),
        snapshot_id=snapshot.snapshot_id,
        operation_id=snapshot.operation.operation_id,
        status=ShadowRollbackStatus.MANUAL_RECOVERY_REQUIRED,
        attempted_at_utc=_utc_now(),
        recovery_guidance=snapshot.rollback_plan.manual_recovery_guidance,
    )


def _irreversible_attempt(snapshot: ShadowSnapshot) -> RollbackAttempt:
    guidance = (
        snapshot.rollback_plan.refusal_reason or "Operation is irreversible; inspect original record for recovery."
    )
    return RollbackAttempt(
        attempt_id=_stable_id("rollback-attempt", snapshot.snapshot_id, _utc_now(), "irreversible"),
        snapshot_id=snapshot.snapshot_id,
        operation_id=snapshot.operation.operation_id,
        status=ShadowRollbackStatus.SKIPPED_IRREVERSIBLE,
        attempted_at_utc=_utc_now(),
        recovery_guidance=guidance,
    )


def _mission_control_item(snapshot: ShadowSnapshot, attempt: RollbackAttempt | None) -> MissionControlUndoItem:
    status = _undoability_for(snapshot, attempt)
    return MissionControlUndoItem(
        snapshot_id=snapshot.snapshot_id,
        run_id=snapshot.run_id,
        operation_id=snapshot.operation.operation_id,
        operation_kind=snapshot.operation.kind,
        undoability_status=status,
        label=snapshot.operation.summary,
        risk_domain=snapshot.operation.risk_domain,
        policy_verdict_ref=snapshot.operation.policy_verdict_ref,
        approval_ref=snapshot.operation.approval_ref,
        original_run_record_ref=snapshot.original_run_record_ref,
        reason_code=_reason_code_for(snapshot, attempt),
        rollback_status=attempt.status.value if attempt else "",
        recovery_guidance=_recovery_guidance_for(snapshot, attempt, status),
    )


def _undoability_for(snapshot: ShadowSnapshot, attempt: RollbackAttempt | None) -> UndoabilityStatus:
    if attempt and attempt.status is ShadowRollbackStatus.SUCCESS:
        return UndoabilityStatus.ROLLED_BACK
    if attempt and attempt.status is ShadowRollbackStatus.FAILED:
        return UndoabilityStatus.FAILED
    if snapshot.reversibility is Reversibility.REVERSIBLE:
        return UndoabilityStatus.UNDOABLE
    if snapshot.reversibility is Reversibility.MANUAL_RECOVERY_REQUIRED:
        return UndoabilityStatus.MANUAL_RECOVERY
    if snapshot.reversibility is Reversibility.IRREVERSIBLE:
        return UndoabilityStatus.IRREVERSIBLE
    return UndoabilityStatus.UNAVAILABLE


def _reason_code_for(snapshot: ShadowSnapshot, attempt: RollbackAttempt | None) -> str:
    if attempt:
        return attempt.status.value
    return snapshot.reversibility.value


def _recovery_guidance_for(
    snapshot: ShadowSnapshot,
    attempt: RollbackAttempt | None,
    status: UndoabilityStatus,
) -> str:
    if attempt and attempt.recovery_guidance:
        return attempt.recovery_guidance
    if status is UndoabilityStatus.UNDOABLE:
        return ""
    return snapshot.rollback_plan.manual_recovery_guidance or snapshot.rollback_plan.refusal_reason


def _rollback_plan_for(
    operation: ShadowOperation,
    reversibility: Reversibility,
    manual_recovery_guidance: str,
) -> ShadowRollbackPlan:
    if reversibility is Reversibility.REVERSIBLE:
        return ShadowRollbackPlan(
            plan_id=_stable_id("rollback-plan", operation.operation_id, operation.before_sha256),
            operation_id=operation.operation_id,
            reversibility=reversibility,
            strategy=RollbackStrategy.FILE_RESTORE,
            target_path=operation.canonical_path,
            restore_sha256=operation.before_sha256,
            restore_content_b64=operation.before_content_b64,
            evidence_refs=(operation.dry_run_evidence_ref, operation.policy_verdict_ref, operation.approval_ref),
        )
    if reversibility is Reversibility.MANUAL_RECOVERY_REQUIRED:
        return ShadowRollbackPlan(
            plan_id=_stable_id("rollback-plan", operation.operation_id, "manual"),
            operation_id=operation.operation_id,
            reversibility=reversibility,
            strategy=RollbackStrategy.MANUAL_RECOVERY,
            manual_recovery_guidance=manual_recovery_guidance,
            evidence_refs=(operation.dry_run_evidence_ref, operation.policy_verdict_ref, operation.approval_ref),
        )
    return ShadowRollbackPlan(
        plan_id=_stable_id("rollback-plan", operation.operation_id, "irreversible"),
        operation_id=operation.operation_id,
        reversibility=reversibility,
        strategy=RollbackStrategy.IRREVERSIBLE_REFUSAL,
        refusal_reason=_irreversible_refusal_reason(operation, manual_recovery_guidance),
        evidence_refs=(operation.dry_run_evidence_ref, operation.policy_verdict_ref, operation.approval_ref),
    )


def _default_reversibility(kind: ShadowOperationKind, manual_recovery_guidance: str) -> Reversibility:
    if kind is ShadowOperationKind.FILE_EDIT:
        return Reversibility.REVERSIBLE
    if kind in {ShadowOperationKind.COMMAND, ShadowOperationKind.AUTOMATION}:
        if not manual_recovery_guidance:
            raise ShadowSnapshotError("manual recovery guidance is required for non-file risky operations")
        return Reversibility.MANUAL_RECOVERY_REQUIRED
    if kind in {
        ShadowOperationKind.PROCESS_START,
        ShadowOperationKind.POLICY_VERDICT,
        ShadowOperationKind.USER_APPROVAL,
    }:
        return Reversibility.IRREVERSIBLE
    return Reversibility.UNKNOWN


def _irreversible_refusal_reason(operation: ShadowOperation, manual_recovery_guidance: str) -> str:
    reason = f"{operation.kind.value} cannot be automatically undone."
    if manual_recovery_guidance:
        return f"{reason} {manual_recovery_guidance}"
    return reason


def _snapshot_from_dict(row: Mapping[str, Any]) -> ShadowSnapshot:
    operation_payload = dict(row["operation"])
    plan_payload = dict(row["rollback_plan"])
    operation = ShadowOperation(**{
        **operation_payload,
        "kind": ShadowOperationKind(operation_payload["kind"]),
        "metadata": dict(operation_payload.get("metadata", {})),
    })
    plan = ShadowRollbackPlan(**{
        **plan_payload,
        "reversibility": Reversibility(plan_payload["reversibility"]),
        "strategy": RollbackStrategy(plan_payload["strategy"]),
        "evidence_refs": tuple(plan_payload.get("evidence_refs", ())),
    })
    return ShadowSnapshot(
        snapshot_id=str(row["snapshot_id"]),
        run_id=str(row["run_id"]),
        operation=operation,
        reversibility=Reversibility(str(row["reversibility"])),
        rollback_plan=plan,
        original_run_record_ref=str(row["original_run_record_ref"]),
        original_run_record_hash=str(row["original_run_record_hash"]),
        captured_at_utc=str(row["captured_at_utc"]),
        original_run_record_payload=dict(row.get("original_run_record_payload", {})),
    )


def _attempt_from_dict(row: Mapping[str, Any]) -> RollbackAttempt:
    return RollbackAttempt(
        attempt_id=str(row["attempt_id"]),
        snapshot_id=str(row["snapshot_id"]),
        operation_id=str(row["operation_id"]),
        status=ShadowRollbackStatus(str(row["status"])),
        attempted_at_utc=str(row["attempted_at_utc"]),
        restored_path=str(row.get("restored_path", "")),
        before_target_sha256=str(row.get("before_target_sha256", "")),
        after_target_sha256=str(row.get("after_target_sha256", "")),
        error_code=str(row.get("error_code", "")),
        error_message=str(row.get("error_message", "")),
        recovery_guidance=str(row.get("recovery_guidance", "")),
    )


def _sha256_path(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_json(value: Mapping[str, Any]) -> str:
    return _sha256_bytes(json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def _stable_id(prefix: str, *parts: str) -> str:
    return f"{prefix}:{uuid5(NAMESPACE_URL, '|'.join((prefix, *parts))).hex}"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _require_store(store: ShadowSnapshotStore) -> None:
    if not isinstance(store, ShadowSnapshotStore):
        raise ShadowSnapshotError("store must be ShadowSnapshotStore")


def _require_text(value: object, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ShadowSnapshotError(f"{field_name} must be non-empty")
