"""Resource accounting aggregation consumed by scheduler and cockpit paths."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from vetinari.workbench.cost.token_cost_split import (
    DEFAULT_PRICING_PATH,
    PricingConfigError,
    calculate_token_cost,
    load_pricing,
)
from vetinari.workbench.resource_cockpit.gpu_hours_to_usd import gpu_hours_to_usd
from vetinari.workbench.resource_cockpit.lease_registry import LeaseRegistryError, PersistentLeaseRegistry


class ResourceAccountingError(RuntimeError):
    """Raised when resource accounting state cannot be trusted."""


@dataclass(frozen=True, slots=True)
class ResourceCostBreakdown:
    """Monetized cost for one scheduler or training usage event."""

    provider: str
    model: str
    target_compute: str
    token_cost_usd: float
    gpu_cost_usd: float
    gpu_hours: float

    def __repr__(self) -> str:
        return (
            "ResourceCostBreakdown("
            f"provider={self.provider!r}, model={self.model!r}, "
            f"target_compute={self.target_compute!r}, total_cost_usd={self.total_cost_usd!r})"
        )

    @property
    def total_cost_usd(self) -> float:
        return round(self.token_cost_usd + self.gpu_cost_usd, 8)

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "target_compute": self.target_compute,
            "token_cost_usd": self.token_cost_usd,
            "gpu_cost_usd": self.gpu_cost_usd,
            "gpu_hours": self.gpu_hours,
            "total_cost_usd": self.total_cost_usd,
        }


def calculate_resource_cost(
    *,
    provider: str = "local",
    model: str,
    target_compute: str,
    tokens_in: int,
    tokens_out: int,
    duration_s: float,
    pricing_path: str | Path = DEFAULT_PRICING_PATH,
) -> ResourceCostBreakdown:
    """Calculate token and GPU costs, failing closed on missing pricing.

    Returns:
        The monetized token, GPU, and total cost breakdown.

    Raises:
        ResourceAccountingError: if pricing configuration is missing or invalid.
    """
    try:
        token_cost = calculate_token_cost(
            provider=provider,
            model=model,
            input_tokens=tokens_in,
            output_tokens=tokens_out,
            pricing_path=pricing_path,
        )
        gpu_hours = round(max(0.0, float(duration_s)) / 3600.0, 8) if target_compute == "gpu" else 0.0
        gpu_cost = (
            gpu_hours_to_usd(gpu_model=model, gpu_hours=gpu_hours, pricing_path=pricing_path) if gpu_hours else 0.0
        )
    except PricingConfigError as exc:
        raise ResourceAccountingError(str(exc)) from exc
    return ResourceCostBreakdown(
        provider=provider,
        model=model,
        target_compute=target_compute,
        token_cost_usd=token_cost.total_cost_usd,
        gpu_cost_usd=gpu_cost,
        gpu_hours=gpu_hours,
    )


def default_resource_accounting_snapshot(
    *,
    pricing_path: str | Path = DEFAULT_PRICING_PATH,
    lease_registry_path: str | Path | None = None,
) -> dict[str, Any]:
    """Return cockpit-visible accounting state from pricing and lease registry.

    Returns:
        A compact accounting snapshot for resource cockpit consumers.

    Raises:
        ResourceAccountingError: if pricing or lease-registry state cannot be
            trusted.
    """
    try:
        pricing = load_pricing(pricing_path)
        configured_registry = lease_registry_path or pricing.get("accounting", {}).get("lease_registry_path")
        registry_state: dict[str, Any]
        if configured_registry:
            registry_state = PersistentLeaseRegistry(configured_registry).snapshot()
        else:
            registry_state = {"schema_version": "1.0", "projects": {}, "updated_at_utc": ""}
    except (PricingConfigError, LeaseRegistryError) as exc:
        raise ResourceAccountingError(str(exc)) from exc
    projects = registry_state.get("projects", {})
    active_count = sum(len(project.get("active_leases", {})) for project in projects.values())
    return {
        "schema_version": "1.0",
        "currency": pricing["currency"],
        "pricing_configured": True,
        "lease_registry_path": str(configured_registry or ""),
        "active_lease_count": active_count,
        "project_ids": sorted(projects),
        "rotation": pricing.get("rotation", {}),
    }


__all__ = [
    "ResourceAccountingError",
    "ResourceCostBreakdown",
    "calculate_resource_cost",
    "default_resource_accounting_snapshot",
]
