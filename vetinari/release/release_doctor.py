"""Release proof backend-health integration."""

from __future__ import annotations

from dataclasses import dataclass

from vetinari.adapters.registry import AdapterRegistry
from vetinari.agents.contracts import OutcomeSignal
from vetinari.runtime.backend_probes import default_probes


@dataclass(frozen=True, slots=True)
class BackendHealthReport:
    """Structured backend health summary for release proof callers."""

    healthy_count: int
    failed_count: int
    signals: list[OutcomeSignal]


@dataclass(frozen=True, slots=True)
class BackendHealthProof:
    """Minimal release proof envelope used by backend health verification."""

    backends_health: tuple[OutcomeSignal, ...]
    health_report: BackendHealthReport
    dry_run: bool = False


def build_release_proof(dry_run: bool = False) -> BackendHealthProof:
    """Build a release proof with backend health signals.

    Returns:
        BackendHealthProof containing one backend health signal per registered provider.
    """
    if dry_run:
        signals = tuple(AdapterRegistry.health_probe_all().values())
    else:
        signals = tuple(probe.probe_fn() for probe in default_probes().values())
    _ = AdapterRegistry.all_capabilities()
    health_report = BackendHealthReport(
        healthy_count=sum(1 for signal in signals if signal.passed),
        failed_count=sum(1 for signal in signals if not signal.passed),
        signals=list(signals),
    )
    return BackendHealthProof(
        backends_health=signals,
        health_report=health_report,
        dry_run=dry_run,
    )
