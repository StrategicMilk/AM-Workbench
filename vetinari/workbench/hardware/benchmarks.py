"""Benchmark snapshot assembly for the hardware digital twin."""

from __future__ import annotations

from collections.abc import Iterable

from vetinari.constants import MODEL_CACHE_DIR
from vetinari.workbench.hardware.contracts import (
    REQUIRED_OBSERVATION_KINDS,
    HardwareTwinSnapshot,
    MeasurementObservation,
    MeasurementStatus,
    ObservationKind,
    RuntimeFingerprint,
    utc_now_seconds,
)
from vetinari.workbench.hardware.probes import collect_lightweight_host_observations, degraded_observation


def build_hardware_twin_snapshot(
    *,
    project_id: str,
    observations: Iterable[MeasurementObservation] | None = None,
    fingerprint: RuntimeFingerprint | None = None,
    snapshot_id: str = "hardware-twin-current",
) -> HardwareTwinSnapshot:
    """Assemble a snapshot with every required benchmark category represented.

    Returns:
        Newly constructed hardware twin snapshot value.
    """
    observed = tuple(observations) if observations is not None else collect_lightweight_host_observations()
    present = {
        observation.kind if isinstance(observation.kind, ObservationKind) else ObservationKind(observation.kind)
        for observation in observed
    }
    filled = observed + tuple(
        degraded_observation(kind, "benchmark-category-missing")
        for kind in sorted(REQUIRED_OBSERVATION_KINDS - present, key=lambda item: item.value)
    )
    evidence_ids = tuple(dict.fromkeys(observation.evidence_id for observation in filled))
    if fingerprint is not None:
        evidence_ids = (*evidence_ids, fingerprint.evidence_id)
    status = (
        MeasurementStatus.READY
        if all(observation.status is MeasurementStatus.READY for observation in filled)
        else MeasurementStatus.DEGRADED
    )
    return HardwareTwinSnapshot(
        snapshot_id=snapshot_id,
        project_id=project_id,
        generated_at_utc=utc_now_seconds(),
        observations=filled,
        fingerprint=fingerprint,
        evidence_ids=evidence_ids,
        status=status,
    )


def synthetic_runtime_fingerprint(**overrides: str) -> RuntimeFingerprint:
    """Return a deterministic fingerprint for tests and advisory payload assembly.

    Returns:
        RuntimeFingerprint value produced by synthetic_runtime_fingerprint().
    """
    values = {
        "cpu_signature": "cpu:24t",
        "ram_signature": "ram:64gb",
        "gpu_device": "gpu:local",
        "storage_signature": "ssd:model-store",
        "driver_version": "driver:1",
        "firmware_version": "firmware:1",
        "cuda_version": "cuda:12",
        "wsl_version": "wsl:2",
        "docker_version": "docker:25",
        "model_server_version": "model-server:1",
        "model_store_path": str(MODEL_CACHE_DIR / "local"),
        "evidence_id": "fingerprint:synthetic",
    }
    values.update(overrides)
    return RuntimeFingerprint(**values)


def synthetic_ready_observations() -> tuple[MeasurementObservation, ...]:
    """Return one ready observation for every required category.

    Returns:
        tuple[MeasurementObservation, ...] value produced by synthetic_ready_observations().
    """
    now = utc_now_seconds()
    return tuple(
        MeasurementObservation(
            kind=kind,
            status=MeasurementStatus.READY,
            value=_default_value(kind),
            unit=_default_unit(kind),
            evidence_id=f"evidence:{kind.value}",
            measured_at_utc=now,
            details=_default_details(kind),
        )
        for kind in ObservationKind
    )


def _default_value(kind: ObservationKind) -> float:
    values = {
        ObservationKind.CPU: 18.0,
        ObservationKind.RAM: 48.0,
        ObservationKind.DISK: 800.0,
        ObservationKind.GPU_VRAM: 18.0,
        ObservationKind.MODEL_LOAD: 850.0,
        ObservationKind.EMBEDDING_VECTOR_SEARCH: 3200.0,
        ObservationKind.WINDOWS_WSL_PATH: 1.12,
        ObservationKind.SERVICE_RESIDENCY: 0.86,
        ObservationKind.THERMAL_POWER: 0.20,
        ObservationKind.RUNTIME_VERSION: 1.0,
    }
    return values[kind]


def _default_unit(kind: ObservationKind) -> str:
    units = {
        ObservationKind.CPU: "threads_available",
        ObservationKind.RAM: "gb_available",
        ObservationKind.DISK: "gb_free",
        ObservationKind.GPU_VRAM: "gb_available",
        ObservationKind.MODEL_LOAD: "ms_warm_load",
        ObservationKind.EMBEDDING_VECTOR_SEARCH: "vectors_per_second",
        ObservationKind.WINDOWS_WSL_PATH: "relative_cost",
        ObservationKind.SERVICE_RESIDENCY: "warm_ratio",
        ObservationKind.THERMAL_POWER: "pressure_ratio",
        ObservationKind.RUNTIME_VERSION: "fingerprint_present",
    }
    return units[kind]


def _default_details(kind: ObservationKind) -> dict[str, float | str]:
    if kind is ObservationKind.MODEL_LOAD:
        return {"cold_load_ms": 2500.0, "warm_load_ms": 850.0}
    if kind is ObservationKind.GPU_VRAM:
        return {"total_vram_gb": 24.0, "available_vram_gb": 18.0}
    if kind is ObservationKind.RAM:
        return {"total_ram_gb": 64.0, "available_ram_gb": 48.0}
    if kind is ObservationKind.CPU:
        return {"cpu_threads": 24.0, "available_cpu_threads": 18.0}
    return {"source": "synthetic"}


__all__ = [
    "build_hardware_twin_snapshot",
    "synthetic_ready_observations",
    "synthetic_runtime_fingerprint",
]
