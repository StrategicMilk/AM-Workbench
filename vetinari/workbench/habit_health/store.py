"""Single-writer file store for Workbench habit-health records."""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

from vetinari.learning.atomic_writers import append_jsonl_atomic
from vetinari.workbench.habit_health.contracts import HabitCheckIn, HabitRoutine
from vetinari.workbench.spine_consumers import record_asset_written, record_run_completed

logger = logging.getLogger(__name__)


_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


@dataclass(frozen=True, slots=True)
class HabitStoreSnapshot:
    """In-memory store snapshot plus recovery state."""

    status: str
    routines: tuple[HabitRoutine, ...] = ()
    check_ins: tuple[HabitCheckIn, ...] = ()
    tombstones: tuple[dict[str, Any], ...] = ()
    recovery_reasons: tuple[str, ...] = ()

    @property
    def recovery_needed(self) -> bool:
        return self.status == "recovery_needed"

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"HabitStoreSnapshot(status={self.status!r}, routines={self.routines!r}, check_ins={self.check_ins!r})"


@dataclass(frozen=True, slots=True)
class HabitStoreWriteResult:
    """Typed result for store writes."""

    accepted: bool
    status: str
    reasons: tuple[str, ...]
    snapshot: HabitStoreSnapshot | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "status": self.status,
            "reasons": list(self.reasons),
            "snapshot": snapshot_to_dict(self.snapshot) if self.snapshot else None,
        }

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"HabitStoreWriteResult(accepted={self.accepted!r}, status={self.status!r}, reasons={self.reasons!r})"


@dataclass(slots=True)
class HabitHealthStore:
    """Atomic local store with per-root single-writer locking."""

    root: Path
    snapshot_name: str = "snapshot.json"
    audit_name: str = "audit.jsonl"
    _lock: threading.RLock = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.root = Path(self.root)
        key = str(self.root.resolve())
        with _LOCKS_GUARD:
            self._lock = _LOCKS.setdefault(key, threading.RLock())

    @property
    def snapshot_path(self) -> Path:
        return self.root / self.snapshot_name

    @property
    def audit_path(self) -> Path:
        return self.root / self.audit_name

    def load(self) -> HabitStoreSnapshot:
        """Execute the load operation.

        Returns:
            HabitStoreSnapshot value produced by load().
        """
        with self._lock:
            if not self.snapshot_path.exists():
                return HabitStoreSnapshot(status="ok")
            try:
                payload = json.loads(self.snapshot_path.read_text(encoding="utf-8"))
                if int(payload.get("schema_version", 0)) != 1:
                    return HabitStoreSnapshot(
                        status="recovery_needed", recovery_reasons=("schema-version-unsupported",)
                    )
                routines = tuple(HabitRoutine.from_mapping(item) for item in payload.get("routines", ()))
                check_ins = tuple(HabitCheckIn.from_mapping(item) for item in payload.get("check_ins", ()))
                tombstones = tuple(dict(item) for item in payload.get("tombstones", ()))
                return HabitStoreSnapshot(status="ok", routines=routines, check_ins=check_ins, tombstones=tombstones)
            except Exception as exc:
                logger.warning("Handled recoverable failure before fallback.", exc_info=True)
                return HabitStoreSnapshot(
                    status="recovery_needed", recovery_reasons=(f"snapshot-unreadable:{type(exc).__name__}",)
                )

    def upsert_routine(self, routine: HabitRoutine) -> HabitStoreWriteResult:
        """Execute the upsert routine operation.

        Returns:
            HabitStoreWriteResult value produced by upsert_routine().
        """
        with self._lock:
            snapshot = self.load()
            if snapshot.recovery_needed:
                return HabitStoreWriteResult(False, "recovery_needed", snapshot.recovery_reasons, snapshot)
            routines = {item.routine_id: item for item in snapshot.routines}
            routines[routine.routine_id] = routine
            updated = HabitStoreSnapshot(
                status="ok",
                routines=tuple(routines.values()),
                check_ins=snapshot.check_ins,
                tombstones=snapshot.tombstones,
            )
            self._write_snapshot(updated)
            self._append_audit("routine_upserted", {"routine_id": routine.routine_id, "user_id": routine.user_id})
            return HabitStoreWriteResult(True, "ok", ("routine-upserted",), updated)

    def append_check_in(self, check_in: HabitCheckIn) -> HabitStoreWriteResult:
        """Execute the append check in operation.

        Returns:
            HabitStoreWriteResult value produced by append_check_in().
        """
        with self._lock:
            snapshot = self.load()
            if snapshot.recovery_needed:
                return HabitStoreWriteResult(False, "recovery_needed", snapshot.recovery_reasons, snapshot)
            updated = HabitStoreSnapshot(
                status="ok",
                routines=snapshot.routines,
                check_ins=(*snapshot.check_ins, check_in),
                tombstones=snapshot.tombstones,
            )
            self._write_snapshot(updated)
            self._append_audit("check_in_appended", {"check_in_id": check_in.check_in_id, "user_id": check_in.user_id})
            return HabitStoreWriteResult(True, "ok", ("check-in-appended",), updated)

    def delete_user_data(self, user_id: str, *, reason: str = "user-request") -> HabitStoreWriteResult:
        """Execute the delete user data operation.

        Returns:
            HabitStoreWriteResult value produced by delete_user_data().
        """
        with self._lock:
            snapshot = self.load()
            if snapshot.recovery_needed:
                return HabitStoreWriteResult(False, "recovery_needed", snapshot.recovery_reasons, snapshot)
            tombstone = {"user_id": user_id, "reason": reason, "deleted_at_utc": _now_iso()}
            tombstoned = HabitStoreSnapshot(
                status="ok",
                routines=snapshot.routines,
                check_ins=snapshot.check_ins,
                tombstones=(*snapshot.tombstones, tombstone),
            )
            self._write_snapshot(tombstoned)
            filtered = HabitStoreSnapshot(
                status="ok",
                routines=tuple(item for item in snapshot.routines if item.user_id != user_id),
                check_ins=tuple(item for item in snapshot.check_ins if item.user_id != user_id),
                tombstones=tombstoned.tombstones,
            )
            self._write_snapshot(filtered)
            self._append_audit("user_data_deleted", tombstone)
            return HabitStoreWriteResult(True, "ok", ("tombstone-written", "user-data-deleted"), filtered)

    def export_user_data(self, user_id: str) -> dict[str, Any]:
        """Execute the export user data operation.

        Returns:
            dict[str, Any] value produced by export_user_data().
        """
        with self._lock:
            snapshot = self.load()
            if snapshot.recovery_needed:
                return {
                    "status": "recovery_needed",
                    "reasons": list(snapshot.recovery_reasons),
                    "routines": [],
                    "check_ins": [],
                }
            return {
                "status": "ok",
                "user_id": user_id,
                "routines": [item.to_dict() for item in snapshot.routines if item.user_id == user_id],
                "check_ins": [item.to_dict() for item in snapshot.check_ins if item.user_id == user_id],
                "tombstones": [item for item in snapshot.tombstones if item.get("user_id") == user_id],
            }

    def _write_snapshot(self, snapshot: HabitStoreSnapshot) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        payload = snapshot_to_dict(snapshot)
        with NamedTemporaryFile("w", encoding="utf-8", dir=self.root, delete=False) as tmp:
            json.dump(payload, tmp, indent=2, sort_keys=True)
            tmp.write("\n")
            temp_name = tmp.name
        os.replace(temp_name, self.snapshot_path)
        # spine_consumers invokes get_spine() and absorbs observability failures.
        record_asset_written(
            asset_id="habit-health-snapshot",
            kind="tool",
            project_id="default",
            path=str(self.snapshot_path),
            redact_fields=["path"],
        )

    def _append_audit(self, event: str, payload: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        row = {"event": event, "recorded_at_utc": _now_iso(), "payload": payload}
        append_jsonl_atomic(self.audit_path, row)
        # spine_consumers invokes get_spine() and absorbs observability failures.
        record_run_completed(
            run_id=f"habit-health-{event}-{row['recorded_at_utc']}",
            kind="agent_run",
            project_id="default",
        )

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"HabitHealthStore(root={self.root!r}, snapshot_name={self.snapshot_name!r}, audit_name={self.audit_name!r})"


def snapshot_to_dict(snapshot: HabitStoreSnapshot | None) -> dict[str, Any] | None:
    """Execute the snapshot to dict operation.

    Returns:
        dict[str, Any] | None value produced by snapshot_to_dict().
    """
    if snapshot is None:
        return None
    return {
        "schema_version": 1,
        "status": snapshot.status,
        "routines": [item.to_dict() for item in snapshot.routines],
        "check_ins": [item.to_dict() for item in snapshot.check_ins],
        "tombstones": [dict(item) for item in snapshot.tombstones],
        "recovery_reasons": list(snapshot.recovery_reasons),
    }


def _now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


__all__ = ["HabitHealthStore", "HabitStoreSnapshot", "HabitStoreWriteResult", "snapshot_to_dict"]
