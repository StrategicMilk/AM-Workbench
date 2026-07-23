"""Dry-run backend probe registry."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from vetinari.adapters.registry import AdapterRegistry
from vetinari.agents.contracts import OutcomeSignal
from vetinari.types import EvidenceBasis, ModelProvider


@dataclass(frozen=True, slots=True)
class BackendProbe:
    """Probe definition for a backend provider."""

    provider_type: ModelProvider
    probe_fn: Callable[[], OutcomeSignal]
    expected_pin_key: str
    timeout_s: float = 5.0

    def __repr__(self) -> str:
        return (
            "BackendProbe("
            f"provider_type={self.provider_type.value!r}, "
            f"expected_pin_key={self.expected_pin_key!r}, timeout_s={self.timeout_s!r})"
        )


def _dry_probe(provider: ModelProvider) -> OutcomeSignal:
    profile = AdapterRegistry.capabilities(provider)
    return OutcomeSignal(
        passed=False,
        score=0.0,
        basis=EvidenceBasis.TOOL_EVIDENCE,
        issues=(f"{provider.value} not installed or not probed",),
        suggestions=(f"cache_durability={profile.cache_durability.value}",),
    )


def default_probes() -> dict[ModelProvider, BackendProbe]:
    """Return one fail-closed probe per registered backend."""
    return {
        provider: BackendProbe(
            provider_type=provider,
            probe_fn=lambda p=provider: _dry_probe(p),
            expected_pin_key=provider.value,
        )
        for provider in AdapterRegistry.providers()
    }
