"""Habit-health tracker service for routines, check-ins, review, and export."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from vetinari.workbench.habit_health.contracts import (
    HabitCheckIn,
    HabitHealthScope,
    HabitHealthSignal,
    HabitHealthSignalKind,
    HabitRoutine,
)
from vetinari.workbench.habit_health.privacy import HabitHealthScopePolicy, HabitHealthUse, evaluate_habit_health_scope
from vetinari.workbench.habit_health.rhythm import compute_rhythm_snapshot
from vetinari.workbench.habit_health.store import HabitHealthStore

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class HabitHealthTracker:
    """Service boundary for local habit-health data and downstream previews."""

    store: HabitHealthStore
    _tempdir: TemporaryDirectory[str] | None = None

    @classmethod
    def in_memory(cls) -> HabitHealthTracker:
        """Execute the in memory operation.

        Returns:
            HabitHealthTracker value produced by in_memory().
        """
        tempdir = TemporaryDirectory()
        return cls(HabitHealthStore(Path(tempdir.name)), tempdir)

    @classmethod
    def file_backed(cls, root: str | Path) -> HabitHealthTracker:
        return cls(HabitHealthStore(Path(root)))

    def close(self) -> None:
        """Release the temporary store backing an in-memory tracker."""
        if self._tempdir is not None:
            self._tempdir.cleanup()
            self._tempdir = None

    def __enter__(self) -> HabitHealthTracker:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def create_routine(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Execute the create routine operation.

        Returns:
            Newly constructed routine value.
        """
        routine_payload = dict(payload)
        routine_payload.setdefault("created_at_utc", _now_iso())
        routine = HabitRoutine.from_mapping(routine_payload)
        result = self.store.upsert_routine(routine)
        return result.to_dict()

    def record_check_in(self, payload: dict[str, Any], policy: HabitHealthScopePolicy | None = None) -> dict[str, Any]:
        """Execute the record check in operation.

        Args:
            payload: Payload data validated or transformed by the operation.
            policy: Policy value consumed by record_check_in().

        Returns:
            Outcome produced by record_check_in().
        """
        check_in_payload = dict(payload)
        check_in_payload.setdefault("checked_at_utc", _now_iso())
        try:
            check_in = HabitCheckIn.from_mapping(check_in_payload)
        except Exception as exc:
            logger.warning("Handled recoverable failure before fallback.", exc_info=True)
            return {"accepted": False, "status": "denied", "reasons": [f"contract-invalid:{type(exc).__name__}"]}
        store_policy = policy or _policy_from_check_in(check_in)
        verdict = evaluate_habit_health_scope(
            store_policy,
            HabitHealthUse.STORE,
            requested_scope=check_in.scope,
            source_context=check_in.source_context,
            consent_refs=check_in.consent_refs,
            provenance_ref=check_in.provenance_ref,
        )
        if not verdict.allowed:
            return {
                "accepted": False,
                "status": "denied",
                "reasons": list(verdict.reasons),
                "privacy_verdict": verdict.to_dict(),
            }
        result = self.store.append_check_in(check_in)
        payload = result.to_dict()
        payload["privacy_verdict"] = verdict.to_dict()
        return payload

    def summary_for_user(self, user_id: str, *, now_utc: str | None = None) -> dict[str, Any]:
        """Execute the summary for user operation.

        Returns:
            dict[str, Any] value produced by summary_for_user().
        """
        snapshot = self.store.load()
        if snapshot.recovery_needed:
            return {"status": "recovery_needed", "reasons": list(snapshot.recovery_reasons)}
        routines = tuple(item for item in snapshot.routines if item.user_id == user_id)
        check_ins = tuple(item for item in snapshot.check_ins if item.user_id == user_id)
        rhythm = compute_rhythm_snapshot(check_ins, routines=routines, now_utc=now_utc, user_id=user_id)
        return {
            "status": "ok",
            "user_id": user_id,
            "routine_count": len(routines),
            "check_in_count": len(check_ins),
            "rhythm": rhythm.to_dict(),
            "missed_routine_ids": list(rhythm.stale_routine_ids),
            "non_medical_boundary": "informational-not-medical",
        }

    def review_user_data(self, user_id: str, policy: HabitHealthScopePolicy | None = None) -> dict[str, Any]:
        """Execute the review user data operation.

        Args:
            user_id: User id whose data should be reviewed.
            policy: Optional privacy policy override.

        Returns:
            dict[str, Any] value produced by review_user_data().
        """
        verdict = evaluate_habit_health_scope(policy, HabitHealthUse.REVIEW)
        if not verdict.allowed:
            return {"status": "denied", "privacy_verdict": verdict.to_dict(), "review_visible": True}
        exported = self.store.export_user_data(user_id)
        return exported | {
            "privacy_verdict": verdict.to_dict(),
            "review_visible": True,
            "delete_visible": True,
            "export_visible": True,
        }

    def export_user_data(self, user_id: str, policy: HabitHealthScopePolicy | None = None) -> dict[str, Any]:
        """Execute the export user data operation.

        Args:
            user_id: User id value consumed by export_user_data().
            policy: Policy value consumed by export_user_data().

        Returns:
            dict[str, Any] value produced by export_user_data().
        """
        verdict = evaluate_habit_health_scope(policy, HabitHealthUse.EXPORT)
        if not verdict.allowed:
            return {"status": "denied", "privacy_verdict": verdict.to_dict(), "export_visible": True}
        return self.store.export_user_data(user_id) | {"privacy_verdict": verdict.to_dict()}

    def delete_user_data(self, user_id: str, *, reason: str = "user-request") -> dict[str, Any]:
        return self.store.delete_user_data(user_id, reason=reason).to_dict()

    def preview_downstream_signal(
        self,
        payload: dict[str, Any],
        policy: HabitHealthScopePolicy | None,
        requested_use: HabitHealthUse | str,
    ) -> HabitHealthSignal:
        """Execute the preview downstream signal operation.

        Args:
            payload: Payload data validated or transformed by the operation.
            policy: Policy value consumed by preview_downstream_signal().
            requested_use: Request object sent through the operation.

        Returns:
            HabitHealthSignal value produced by preview_downstream_signal().
        """
        scope = _scope(payload.get("scope"))
        verdict = evaluate_habit_health_scope(
            policy,
            requested_use,
            requested_scope=scope,
            source_context=str(payload.get("source_context", "")),
            consent_refs=tuple(str(item) for item in payload.get("consent_refs", ()) if str(item).strip()),
            provenance_ref=str(payload.get("provenance_ref", "")),
            downstream_contract_ref=str(payload.get("downstream_contract_ref", "")),
        )
        return HabitHealthSignal(
            signal_id=str(payload.get("signal_id") or "habit-signal-preview"),
            user_id=str(payload.get("user_id", policy.user_id if policy else "")),
            signal_kind=HabitHealthSignalKind.DOWNSTREAM_PREVIEW,
            scope=scope,
            source_context=str(payload.get("source_context", "")),
            consent_refs=tuple(str(item) for item in payload.get("consent_refs", ()) if str(item).strip()),
            provenance_ref=str(payload.get("provenance_ref", "")),
            downstream_use=verdict.use.value,
            allowed=verdict.allowed,
            reasons=verdict.reasons,
            payload=dict(payload),
        )


def _policy_from_check_in(check_in: HabitCheckIn) -> HabitHealthScopePolicy:
    return HabitHealthScopePolicy(
        user_id=check_in.user_id,
        allowed_scopes=(check_in.scope,),
        consent_refs=check_in.consent_refs,
        provenance_ref=check_in.provenance_ref,
        source_context=check_in.source_context,
        allowed_downstream_uses=(HabitHealthUse.STORE, HabitHealthUse.REVIEW, HabitHealthUse.EXPORT),
    )


def _scope(value: object) -> HabitHealthScope:
    raw_value = value.value if isinstance(value, Enum) else value
    try:
        return HabitHealthScope(raw_value)
    except ValueError:
        logger.warning("Handled recoverable failure before fallback.", exc_info=True)
        return HabitHealthScope.UNKNOWN


def _now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


__all__ = ["HabitHealthTracker"]
