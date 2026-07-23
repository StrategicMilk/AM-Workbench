"""Adapter health-monitor collaborator for AdapterManager."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from vetinari._provider_metrics import ProviderHealthStatus, ProviderMetrics

if TYPE_CHECKING:
    from vetinari.adapters.base import ProviderType


@dataclass(slots=True)
class AdapterHealthMonitor:
    """Small value object for adapter health observations."""

    name: str
    provider_type: ProviderType
    checked_at: datetime
    status: ProviderHealthStatus

    def __repr__(self) -> str:
        return (
            f"AdapterHealthMonitor(name={self.name!r}, provider_type={self.provider_type!r}, "
            f"status={self.status.value!r})"
        )


__all__ = ["AdapterHealthMonitor", "ProviderHealthStatus", "ProviderMetrics"]
