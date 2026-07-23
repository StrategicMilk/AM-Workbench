"""Runtime and hardware drift detection."""

from __future__ import annotations

from dataclasses import fields

from vetinari.workbench.hardware.contracts import (
    DriftChange,
    HardwareDriftReport,
    MeasurementStatus,
    RuntimeFingerprint,
)

DRIFT_FIELDS: tuple[str, ...] = (
    "driver_version",
    "firmware_version",
    "cuda_version",
    "wsl_version",
    "docker_version",
    "model_server_version",
    "gpu_device",
    "storage_signature",
    "cpu_signature",
    "ram_signature",
    "model_store_path",
)


def compare_runtime_fingerprints(
    baseline: RuntimeFingerprint | None,
    current: RuntimeFingerprint | None,
) -> HardwareDriftReport:
    """Compare fingerprints and fail closed when current state is unknown.

    Args:
        baseline: Baseline value consumed by compare_runtime_fingerprints().
        current: Current value consumed by compare_runtime_fingerprints().

    Returns:
        HardwareDriftReport value produced by compare_runtime_fingerprints().
    """
    if baseline is None or current is None:
        return HardwareDriftReport(
            status=MeasurementStatus.DEGRADED,
            changes=(),
            evidence_ids=("drift:fingerprint-unreadable",),
            rebenchmark_required=True,
            degradation_reasons=("runtime-fingerprint-unreadable",),
        )

    changes = tuple(
        DriftChange(
            field=field,
            before=str(getattr(baseline, field)),
            after=str(getattr(current, field)),
            evidence_id=f"drift:{field}",
        )
        for field in DRIFT_FIELDS
        if getattr(baseline, field) != getattr(current, field)
    )
    evidence_ids = tuple(
        dict.fromkeys((baseline.evidence_id, current.evidence_id, *(change.evidence_id for change in changes)))
    )
    return HardwareDriftReport(
        status=MeasurementStatus.READY,
        changes=changes,
        evidence_ids=evidence_ids,
        rebenchmark_required=bool(changes),
    )


def fingerprint_fields() -> tuple[str, ...]:
    """Return fingerprint fields covered by drift, excluding evidence metadata."""
    return tuple(field.name for field in fields(RuntimeFingerprint) if field.name != "evidence_id")


__all__ = ["DRIFT_FIELDS", "compare_runtime_fingerprints", "fingerprint_fields"]
