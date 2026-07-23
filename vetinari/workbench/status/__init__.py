"""Workbench status health console public API."""

from __future__ import annotations

from vetinari.workbench.status.contracts import (
    ProbeResult,
    WorkbenchHealthDomain,
    WorkbenchHealthResult,
    WorkbenchHealthState,
    WorkbenchStatusConfig,
    WorkbenchStatusSeverity,
    WorkbenchStatusSnapshot,
)
from vetinari.workbench.status.cost_overrun_probe import cost_overrun_probe
from vetinari.workbench.status.credential_expiry_probe import credential_expiry_probe
from vetinari.workbench.status.model_availability_probe import model_availability_probe
from vetinari.workbench.status.scheduler_lag_probe import scheduler_lag_probe
from vetinari.workbench.status.service import (
    build_assistant_status_context,
    build_workbench_status_snapshot,
    load_workbench_status_config,
)
from vetinari.workbench.status.settings_actions import run_workbench_status_action

__all__ = [
    "ProbeResult",
    "WorkbenchHealthDomain",
    "WorkbenchHealthResult",
    "WorkbenchHealthState",
    "WorkbenchStatusConfig",
    "WorkbenchStatusSeverity",
    "WorkbenchStatusSnapshot",
    "build_assistant_status_context",
    "build_workbench_status_snapshot",
    "cost_overrun_probe",
    "credential_expiry_probe",
    "load_workbench_status_config",
    "model_availability_probe",
    "run_workbench_status_action",
    "scheduler_lag_probe",
]
