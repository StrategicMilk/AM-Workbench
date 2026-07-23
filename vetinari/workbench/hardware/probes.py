"""Bounded host probe interfaces for the hardware digital twin."""

from __future__ import annotations

import concurrent.futures
import importlib.util
import logging
import shutil
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from itertools import starmap
from pathlib import Path

from vetinari.workbench.hardware.contracts import (
    MeasurementObservation,
    MeasurementStatus,
    ObservationKind,
    utc_now_seconds,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ProbeBudget:
    """Resource ceiling for a bounded host probe."""

    timeout_seconds: float = 1.25
    max_bytes: int = 1024 * 1024

    def __post_init__(self) -> None:
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if self.max_bytes <= 0:
            raise ValueError("max_bytes must be positive")


class BoundedProbeRunner:
    """Run explicit probe callables with a small timeout."""

    def __init__(self, budget: ProbeBudget | None = None) -> None:
        self.budget = budget or ProbeBudget()

    def run(
        self,
        kind: ObservationKind | str,
        probe: Callable[[], MeasurementObservation],
    ) -> MeasurementObservation:
        """Run a probe and degrade on timeout, permissions, or unsupported counters.

        Args:
            kind: Kind discriminator used to select the operation branch.
            probe: Probe value consumed by run().

        Returns:
            MeasurementObservation value produced by run().
        """
        selected = _coerce_kind(kind)
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        future = executor.submit(probe)
        try:
            return future.result(timeout=self.budget.timeout_seconds)
        except concurrent.futures.TimeoutError:
            logger.warning("Handled recoverable failure before fallback.", exc_info=True)
            future.cancel()
            return degraded_observation(selected, "probe-timeout")
        except (OSError, PermissionError, RuntimeError, ValueError) as exc:
            logger.warning("Handled recoverable failure before fallback.", exc_info=True)
            return degraded_observation(selected, f"probe-error:{type(exc).__name__}")
        finally:
            # Single-worker hardware probes must not block on a stuck device counter.
            executor.shutdown(wait=False, cancel_futures=True)


def degraded_observation(kind: ObservationKind | str, reason: str) -> MeasurementObservation:
    """Return a degraded observation with explicit evidence.

    Args:
        kind: Kind discriminator used to select the operation branch.
        reason: Reason value consumed by degraded_observation().

    Returns:
        MeasurementObservation value produced by degraded_observation().
    """
    selected = _coerce_kind(kind)
    return MeasurementObservation(
        kind=selected,
        status=MeasurementStatus.DEGRADED,
        value=None,
        unit="unavailable",
        evidence_id=f"{selected.value}:{reason}",
        measured_at_utc=utc_now_seconds(),
        details={"reason": reason},
    )


def collect_lightweight_host_observations(
    *,
    runner: BoundedProbeRunner | None = None,
    root_path: Path | str = ".",
) -> tuple[MeasurementObservation, ...]:
    """Collect dependency-light host facts without import-time I/O.

    Returns:
        Collection of lightweight host observations values.
    """
    selected_runner = runner or BoundedProbeRunner()
    probes: tuple[tuple[ObservationKind, Callable[[], MeasurementObservation]], ...] = (
        (ObservationKind.CPU, _cpu_probe),
        (ObservationKind.RAM, _ram_probe),
        (ObservationKind.GPU_VRAM, _gpu_vram_probe),
        (ObservationKind.DISK, lambda: _disk_probe(Path(root_path))),
    )
    collected = list(starmap(selected_runner.run, probes))
    collected.extend(degraded_observation(kind, "probe-not-configured") for kind in _unconfigured_kinds(collected))
    return tuple(collected)


def observations_from_mapping(values: dict[ObservationKind | str, float]) -> tuple[MeasurementObservation, ...]:
    """Build deterministic ready observations for tests and synthetic benchmarks.

    Returns:
        tuple[MeasurementObservation, ...] value produced by observations_from_mapping().
    """
    now = utc_now_seconds()
    return tuple(
        MeasurementObservation(
            kind=_coerce_kind(kind),
            status=MeasurementStatus.READY,
            value=float(value),
            unit="score",
            evidence_id=f"measurement:{_coerce_kind(kind).value}",
            measured_at_utc=now,
            details={"source": "mapping"},
        )
        for kind, value in values.items()
    )


def _cpu_probe() -> MeasurementObservation:
    import psutil

    used_percent = float(psutil.cpu_percent(interval=1))
    cpu_count = psutil.cpu_count(logical=True) or 0
    available_threads = max(0, round(cpu_count * (1.0 - used_percent / 100.0)))
    return MeasurementObservation(
        kind=ObservationKind.CPU,
        status=MeasurementStatus.READY if cpu_count > 0 else MeasurementStatus.DEGRADED,
        value=used_percent,
        unit="percent",
        evidence_id="cpu:psutil-cpu-count",
        measured_at_utc=utc_now_seconds(),
        details={
            "cpu_threads": cpu_count,
            "available_cpu_threads": available_threads,
            "used_percent": used_percent,
        },
    )


def _ram_probe() -> MeasurementObservation:
    import psutil

    memory = psutil.virtual_memory()
    available_gb = round(memory.available / (1024**3), 3)
    total_gb = round(memory.total / (1024**3), 3)
    return MeasurementObservation(
        kind=ObservationKind.RAM,
        status=MeasurementStatus.READY,
        value=float(memory.percent),
        unit="percent",
        evidence_id="ram:psutil-virtual-memory",
        measured_at_utc=utc_now_seconds(),
        details={
            "available_ram_gb": available_gb,
            "total_ram_gb": total_gb,
            "used_percent": float(memory.percent),
        },
    )


def _gpu_vram_probe() -> MeasurementObservation:
    if importlib.util.find_spec("pynvml") is None:
        return MeasurementObservation(
            kind=ObservationKind.GPU_VRAM,
            status=MeasurementStatus.UNAVAILABLE,
            value=None,
            unit="gb",
            evidence_id="gpu-vram:pynvml-unavailable",
            measured_at_utc=utc_now_seconds(),
            details={"reason": "pynvml not installed"},
        )
    try:
        pynvml = __import__("pynvml")
        pynvml.nvmlInit()
        handle = pynvml.nvmlDeviceGetHandleByIndex(0)
        info = pynvml.nvmlDeviceGetMemoryInfo(handle)
    except Exception as exc:
        logger.warning("pynvml GPU VRAM probe failed", exc_info=True)
        return MeasurementObservation(
            kind=ObservationKind.GPU_VRAM,
            status=MeasurementStatus.DEGRADED,
            value=None,
            unit="gb",
            evidence_id="gpu-vram:pynvml-error",
            measured_at_utc=utc_now_seconds(),
            details={"reason": str(exc)},
        )
    return MeasurementObservation(
        kind=ObservationKind.GPU_VRAM,
        status=MeasurementStatus.READY,
        value=round(info.free / (1024**3), 3),
        unit="gb_free",
        evidence_id="gpu-vram:pynvml",
        measured_at_utc=utc_now_seconds(),
        details={
            "free_gb": round(info.free / (1024**3), 3),
            "total_gb": round(info.total / (1024**3), 3),
            "used_gb": round(info.used / (1024**3), 3),
        },
    )


def emit_surface_error_signal(surface_id: str, error_kind: str, message: str, project_id: str = "") -> None:
    """Emit a surface error signal to the status telemetry log.

    Args:
        surface_id: Surface emitting the error.
        error_kind: Machine-readable error category.
        message: Human-readable error detail.
        project_id: Optional project scope.
    """
    logger.warning(
        "surface error signal surface_id=%s error_kind=%s project_id=%s message=%s",
        surface_id,
        error_kind,
        project_id,
        message,
    )


def _disk_probe(root_path: Path) -> MeasurementObservation:
    usage = shutil.disk_usage(root_path)
    free_gb = usage.free / (1024**3)
    return MeasurementObservation(
        kind=ObservationKind.DISK,
        status=MeasurementStatus.READY,
        value=round(free_gb, 3),
        unit="gb_free",
        evidence_id="disk:shutil-disk-usage",
        measured_at_utc=utc_now_seconds(),
        details={"free_gb": round(free_gb, 3), "total_gb": round(usage.total / (1024**3), 3)},
    )


def _unconfigured_kinds(collected: Iterable[MeasurementObservation]) -> tuple[ObservationKind, ...]:
    present = {observation.kind for observation in collected}
    return tuple(kind for kind in ObservationKind if kind not in present)


def _coerce_kind(kind: ObservationKind | str) -> ObservationKind:
    if isinstance(kind, ObservationKind):
        return kind
    return ObservationKind(str(kind))


__all__ = [
    "BoundedProbeRunner",
    "ProbeBudget",
    "collect_lightweight_host_observations",
    "degraded_observation",
    "emit_surface_error_signal",
    "observations_from_mapping",
]
