"""Pure adapter projections from hardware twin payloads to Workbench surfaces."""

from __future__ import annotations

from typing import Any

from vetinari.workbench.hardware.contracts import (
    HardwareDriftReport,
    HardwareTwinSnapshot,
    MeasurementStatus,
    ObservationKind,
)


def snapshot_to_machine_profile_payload(snapshot: HardwareTwinSnapshot) -> dict[str, Any]:
    """Project measured headroom to the resource cockpit/governor machine shape.

    Returns:
        dict[str, Any] value produced by snapshot_to_machine_profile_payload().
    """
    cpu = snapshot.observation(ObservationKind.CPU)
    ram = snapshot.observation(ObservationKind.RAM)
    disk = snapshot.observation(ObservationKind.DISK)
    gpu = snapshot.observation(ObservationKind.GPU_VRAM)
    return {
        "profile_id": f"hardware-twin:{snapshot.project_id}",
        "status": snapshot.status.value,
        "total_vram_gb": float(gpu.details.get("total_vram_gb", gpu.value or 0.0)),
        "available_vram_gb": float(gpu.details.get("available_vram_gb", gpu.value or 0.0)),
        "total_ram_gb": float(ram.details.get("total_ram_gb", ram.value or 0.0)),
        "available_ram_gb": float(ram.details.get("available_ram_gb", ram.value or 0.0)),
        "cpu_threads": int(cpu.details.get("cpu_threads", cpu.value or 0)),
        "available_cpu_threads": int(cpu.details.get("available_cpu_threads", cpu.value or 0)),
        "storage_free_gb": float(disk.value or 0.0),
        "evidence_ids": list(snapshot.evidence_ids),
        "measured_at_utc": snapshot.generated_at_utc,
        "source": "hardware_twin_measured",
    }


def snapshot_to_cost_planner_options(snapshot: HardwareTwinSnapshot) -> list[dict[str, Any]]:
    """Project observed resource latency/headroom into cost planner option payloads.

    Returns:
        list[dict[str, Any]] value produced by snapshot_to_cost_planner_options().
    """
    model_load = snapshot.observation(ObservationKind.MODEL_LOAD)
    ram = snapshot.observation(ObservationKind.RAM)
    gpu = snapshot.observation(ObservationKind.GPU_VRAM)
    return [
        {
            "backend": "local-hardware-twin",
            "model_id": "measured-local",
            "input_cost_per_1k_tokens": 0.0,
            "output_cost_per_1k_tokens": 0.0,
            "latency_ms_per_request": float(model_load.details.get("warm_load_ms", model_load.value or 0.0)),
            "gpu_vram_gb": float(gpu.details.get("available_vram_gb", gpu.value or 0.0)),
            "ram_gb": float(ram.details.get("available_ram_gb", ram.value or 0.0)),
            "local_gpu": (gpu.value or 0) > 0,
            "evidence_ids": [model_load.evidence_id, gpu.evidence_id, ram.evidence_id],
        }
    ]


def snapshot_to_monitoring_signals(
    snapshot: HardwareTwinSnapshot, drift: HardwareDriftReport | None = None
) -> list[dict[str, Any]]:
    """Project snapshot/drift into monitoring-style signal dictionaries.

    Args:
        snapshot: Snapshot value consumed by snapshot_to_monitoring_signals().
        drift: Drift value consumed by snapshot_to_monitoring_signals().

    Returns:
        list[dict[str, Any]] value produced by snapshot_to_monitoring_signals().
    """
    signals = [
        {
            "signal": "hardware_twin_status",
            "severity": "info" if snapshot.ready else "warning",
            "status": snapshot.status.value,
            "evidence_ids": list(snapshot.evidence_ids),
        }
    ]
    if drift is not None:
        signals.append({
            "signal": "hardware_twin_drift",
            "severity": "warning" if drift.rebenchmark_required else "info",
            "status": drift.status.value,
            "rebenchmark_required": drift.rebenchmark_required,
            "evidence_ids": list(drift.evidence_ids),
        })
    return signals


def snapshot_to_run_evidence_payload(snapshot: HardwareTwinSnapshot) -> dict[str, Any]:
    """Return a run/session evidence-link payload without mutating upstream run code."""
    return {
        "kind": "hardware_twin_snapshot",
        "snapshot_id": snapshot.snapshot_id,
        "project_id": snapshot.project_id,
        "status": snapshot.status.value,
        "ready": snapshot.status is MeasurementStatus.READY and not snapshot.degradation_reasons,
        "evidence_ids": list(snapshot.evidence_ids),
        "generated_at_utc": snapshot.generated_at_utc,
    }


__all__ = [
    "snapshot_to_cost_planner_options",
    "snapshot_to_machine_profile_payload",
    "snapshot_to_monitoring_signals",
    "snapshot_to_run_evidence_payload",
]
