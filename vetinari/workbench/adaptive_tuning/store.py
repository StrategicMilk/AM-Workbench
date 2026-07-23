"""Locked adaptive hypothesis store."""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from vetinari.security.temporal import DecisionTimeError, parse_decision_time
from vetinari.workbench.adaptive_tuning.contracts import (
    AdaptiveHypothesis,
    AdaptiveTuningError,
    ControlAction,
    HypothesisStatus,
    UserControlDecision,
)
from vetinari.workbench.spine_consumers import record_asset_written

DEFAULT_ADAPTIVE_TUNING_STATE_ROOT = Path("outputs") / "workbench" / "spine" / "adaptive_tuning"
logger = logging.getLogger(__name__)

_LOCKS: dict[Path, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


@dataclass(frozen=True, slots=True)
class AdaptiveTuningSnapshot:
    """Persisted adaptive tuning state."""

    project_id: str
    hypotheses: tuple[dict[str, Any], ...] = ()
    controls: tuple[dict[str, Any], ...] = ()
    schema_version: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.project_id, str) or not self.project_id.strip():
            raise AdaptiveTuningError("project-id-invalid", self.project_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "project_id": self.project_id,
            "hypotheses": list(self.hypotheses),
            "controls": list(self.controls),
        }

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"AdaptiveTuningSnapshot(project_id={self.project_id!r}, hypotheses={self.hypotheses!r}, controls={self.controls!r})"


@dataclass(slots=True)
class AdaptiveTuningStore:
    """Single-writer per project/profile store with atomic replace."""

    state_root: Path = field(default_factory=lambda: DEFAULT_ADAPTIVE_TUNING_STATE_ROOT)

    def load(self, project_id: str) -> AdaptiveTuningSnapshot:
        """Load and validate one project snapshot.

        Returns:
            AdaptiveTuningSnapshot value produced by load().

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        path = self._path(project_id)
        lock = _lock_for(path)
        with lock:
            if not path.exists():
                return AdaptiveTuningSnapshot(project_id=project_id)
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise AdaptiveTuningError("store-corrupt-or-unreadable", str(path)) from exc
            return _snapshot_from_payload(payload, project_id)

    def save_hypotheses(self, project_id: str, hypotheses: tuple[AdaptiveHypothesis, ...]) -> AdaptiveTuningSnapshot:
        """Persist hypotheses with atomic replace.

        Args:
            project_id: Project identifier that scopes the operation.
            hypotheses: Hypotheses value consumed by save_hypotheses().

        Returns:
            AdaptiveTuningSnapshot value produced by save_hypotheses().
        """
        path = self._path(project_id)
        lock = _lock_for(path)
        with lock:
            prior = self.load(project_id)
            payload = AdaptiveTuningSnapshot(
                project_id=project_id,
                hypotheses=tuple(item.to_dict() for item in hypotheses),
                controls=prior.controls,
            )
            self._atomic_write(path, payload.to_dict())
            return self.load(project_id)

    def record_control(self, project_id: str, decision: UserControlDecision) -> AdaptiveTuningSnapshot:
        """Persist a control decision and apply latest-control state.

        Args:
            project_id: Project identifier that scopes the operation.
            decision: Decision value consumed by record_control().

        Returns:
            Outcome produced by record_control().
        """
        path = self._path(project_id)
        lock = _lock_for(path)
        with lock:
            prior = self.load(project_id)
            controls = [*prior.controls, decision.to_dict()]
            latest_controls = _latest_controls_by_hypothesis(controls)
            hypotheses = [
                _apply_control(row, latest_controls.get(str(row.get("hypothesis_id")))) for row in prior.hypotheses
            ]
            payload = AdaptiveTuningSnapshot(
                project_id=project_id, hypotheses=tuple(hypotheses), controls=tuple(controls)
            )
            self._atomic_write(path, payload.to_dict())
            return self.load(project_id)

    def _path(self, project_id: str) -> Path:
        if not project_id or any(marker in project_id for marker in ("/", "\\", "..", "\x00")):
            raise AdaptiveTuningError("project-id-invalid", project_id)
        return self.state_root / f"{project_id}.json"

    def _atomic_write(self, path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.tmp")
        data = json.dumps(payload, indent=2, sort_keys=True)
        tmp.write_text(data, encoding="utf-8")
        json.loads(tmp.read_text(encoding="utf-8"))
        os.replace(tmp, path)
        # spine_consumers invokes get_spine() and absorbs observability failures.
        record_asset_written(
            asset_id=path.stem,
            kind="tool",
            project_id=str(payload.get("project_id", "default")),
            path=str(path),
            redact_fields=["path"],
        )


def _apply_control(row: dict[str, Any], decision: dict[str, Any] | None) -> dict[str, Any]:
    if decision is None:
        return row
    updated = dict(row)
    action = ControlAction(str(decision.get("action", "")))
    if action is ControlAction.FORGET:
        updated["status"] = HypothesisStatus.FORGOTTEN.value
    elif action is ControlAction.REVOKE:
        updated["status"] = HypothesisStatus.REVOKED.value
    elif action is ControlAction.REJECT:
        updated["status"] = HypothesisStatus.REJECTED.value
    elif action is ControlAction.ALLOW:
        updated["status"] = HypothesisStatus.ACTIVE.value
    updated["last_control_action"] = action.value
    updated["last_control_at_utc"] = str(decision.get("decided_at_utc", ""))
    return updated


def _latest_controls_by_hypothesis(controls: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for control in controls:
        hypothesis_id = str(control.get("hypothesis_id", ""))
        if not hypothesis_id:
            continue
        try:
            control_time = _control_time(control)
        except DecisionTimeError:
            logger.warning("Dropping malformed adaptive tuning control timestamp.", exc_info=True)
            continue
        if hypothesis_id not in latest or control_time >= _control_time(latest[hypothesis_id]):
            latest[hypothesis_id] = control
    return latest


def _control_time(control: dict[str, Any]) -> datetime:
    return parse_decision_time(control.get("decided_at_utc"))


def _snapshot_from_payload(payload: Any, project_id: str) -> AdaptiveTuningSnapshot:
    if not isinstance(payload, dict):
        raise AdaptiveTuningError("store-root-invalid")
    if payload.get("schema_version") != 1:
        raise AdaptiveTuningError("store-schema-version-invalid")
    if payload.get("project_id") != project_id:
        raise AdaptiveTuningError("store-project-mismatch")
    hypotheses = payload.get("hypotheses", [])
    controls = payload.get("controls", [])
    if not isinstance(hypotheses, list) or not isinstance(controls, list):
        raise AdaptiveTuningError("store-list-invalid")
    return AdaptiveTuningSnapshot(project_id=project_id, hypotheses=tuple(hypotheses), controls=tuple(controls))


def _lock_for(path: Path) -> threading.RLock:
    resolved = path.resolve()
    with _LOCKS_GUARD:
        if resolved not in _LOCKS:
            _LOCKS[resolved] = threading.RLock()
        return _LOCKS[resolved]
