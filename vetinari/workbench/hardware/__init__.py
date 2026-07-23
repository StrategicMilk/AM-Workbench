"""Workbench hardware digital twin library exports."""

from __future__ import annotations

from vetinari.workbench.hardware.adapters import (
    snapshot_to_cost_planner_options,
    snapshot_to_machine_profile_payload,
    snapshot_to_monitoring_signals,
    snapshot_to_run_evidence_payload,
)
from vetinari.workbench.hardware.benchmarks import build_hardware_twin_snapshot
from vetinari.workbench.hardware.contracts import (
    DriftChange,
    HardwareDriftReport,
    HardwareTwinError,
    HardwareTwinSnapshot,
    MeasurementObservation,
    MeasurementStatus,
    ObservationKind,
    OptimizationProposal,
    OptimizationScope,
    ProposalRisk,
    RuntimeFingerprint,
)
from vetinari.workbench.hardware.drift import compare_runtime_fingerprints
from vetinari.workbench.hardware.optimizer import propose_hardware_optimizations
from vetinari.workbench.hardware.profiles import (
    BenchmarkCategoryPolicy,
    HardwareProfileError,
    HardwareProfilePolicy,
    ProposalRiskPolicy,
    load_hardware_profiles,
)
from vetinari.workbench.hardware.state import HardwareTwinStateStore

__all__ = [
    "BenchmarkCategoryPolicy",
    "DriftChange",
    "HardwareDriftReport",
    "HardwareProfileError",
    "HardwareProfilePolicy",
    "HardwareTwinError",
    "HardwareTwinSnapshot",
    "HardwareTwinStateStore",
    "MeasurementObservation",
    "MeasurementStatus",
    "ObservationKind",
    "OptimizationProposal",
    "OptimizationScope",
    "ProposalRisk",
    "ProposalRiskPolicy",
    "RuntimeFingerprint",
    "build_hardware_twin_snapshot",
    "compare_runtime_fingerprints",
    "load_hardware_profiles",
    "propose_hardware_optimizations",
    "snapshot_to_cost_planner_options",
    "snapshot_to_machine_profile_payload",
    "snapshot_to_monitoring_signals",
    "snapshot_to_run_evidence_payload",
]
