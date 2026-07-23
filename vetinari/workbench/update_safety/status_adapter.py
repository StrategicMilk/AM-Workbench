"""Status dependency snapshots for update safety."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from vetinari.workbench.status.contracts import WorkbenchHealthState, WorkbenchStatusSeverity
from vetinari.workbench.update_safety.contracts import UpdateReadiness, UpdateReadinessState
from vetinari.workbench.update_safety.service import evaluate_update_readiness


def build_update_status_dependency_snapshot(readiness: UpdateReadiness | None = None) -> dict[str, dict[str, Any]]:
    """Return dependency snapshots consumed by the existing status service.

    Returns:
        Newly constructed update status dependency snapshot value.
    """
    current = readiness or evaluate_update_readiness()
    checked_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    update_state, severity = _status_for_readiness(current.state)
    support_state = (
        WorkbenchHealthState.CONFIGURED
        if current.state is not UpdateReadinessState.BLOCKED
        else WorkbenchHealthState.DEGRADED
    )
    return {
        "updates": {
            "key": "updates.readiness",
            "state": update_state.value,
            "severity": severity.value,
            "summary": f"updates {current.state.value}: {', '.join(current.reasons)}",
            "evidence_refs": ["update-readiness:evaluate_update_readiness"],
            "checked_at_utc": checked_at,
            "settings_target": "settings.workbench.updates",
            "fix_action": "workbench-update-check",
        },
        "support_bundle": {
            "key": "support_bundle.update_safety",
            "state": support_state.value,
            "severity": WorkbenchStatusSeverity.INFO.value
            if support_state is WorkbenchHealthState.CONFIGURED
            else WorkbenchStatusSeverity.WARNING.value,
            "summary": "update support bundle builder is available",
            "evidence_refs": ["update-support-bundle:builder"],
            "checked_at_utc": checked_at,
            "settings_target": "settings.workbench.support_bundle",
            "fix_action": "create-update-support-bundle",
        },
    }


def _status_for_readiness(state: UpdateReadinessState) -> tuple[WorkbenchHealthState, WorkbenchStatusSeverity]:
    if state is UpdateReadinessState.READY:
        return WorkbenchHealthState.APPROVAL_REQUIRED, WorkbenchStatusSeverity.BLOCKING
    if state in {UpdateReadinessState.CURRENT, UpdateReadinessState.SKIPPED}:
        return WorkbenchHealthState.CONFIGURED, WorkbenchStatusSeverity.INFO
    return WorkbenchHealthState.DEGRADED, WorkbenchStatusSeverity.WARNING


__all__ = ["build_update_status_dependency_snapshot"]
