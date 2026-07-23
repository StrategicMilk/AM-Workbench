"""Runtime safety primitives — supported-matrix-driven preconditions.

Exposes the runtime doctor that reads config/runtime/supported_matrix.yaml and
fails closed when the detected runtime does not satisfy the matrix.
"""

from __future__ import annotations

from importlib import import_module

from vetinari.runtime.runtime_doctor import (
    RuntimeCheckResult,
    RuntimeDoctorReport,
    check_matrix_row,
    load_matrix,
    run_doctor,
    validate_runtime_version,
)

_SCHEDULER_EXPORTS = {
    "Lane",
    "LaneUsageReceipt",
    "Lease",
    "VRAMOverCommit",
    "WorkbenchScheduler",
    "WorkbenchSchedulerCapacityRetryExceeded",
    "WorkbenchSchedulerConfigError",
    "WorkbenchSchedulerLaneFull",
    "WorkbenchSchedulerOutsideTrainingWindow",
    "signal_handlers_installed_state",
}


def __getattr__(name: str):
    if name in _SCHEDULER_EXPORTS:
        scheduler = import_module("vetinari.runtime.workbench_scheduler")
        value = getattr(scheduler, name)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "Lane",
    "LaneUsageReceipt",
    "Lease",
    "RuntimeCheckResult",
    "RuntimeDoctorReport",
    "VRAMOverCommit",
    "WorkbenchScheduler",
    "WorkbenchSchedulerCapacityRetryExceeded",
    "WorkbenchSchedulerConfigError",
    "WorkbenchSchedulerLaneFull",
    "WorkbenchSchedulerOutsideTrainingWindow",
    "check_matrix_row",
    "load_matrix",
    "run_doctor",
    "signal_handlers_installed_state",
    "validate_runtime_version",
]
