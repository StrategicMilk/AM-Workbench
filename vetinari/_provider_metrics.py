"""Provider health status and per-provider metrics dataclass for the Adapter Manager.

These types are separated from the main AdapterManager to keep file size within
project limits while preserving the public import surface via re-exports.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from vetinari.adapters.base import ProviderType

logger_name = __name__


class ProviderHealthStatus(Enum):
    """Health status of a provider."""

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    UNKNOWN = "unknown"


@dataclass
class ProviderMetrics:
    """Metrics for a single inference provider.

    Tracks success/failure counts, latency, token usage, and estimated cost
    for a registered provider instance. Used by AdapterManager to inform
    provider selection and operator dashboards.
    """

    name: str
    provider_type: ProviderType
    last_health_check: datetime | None = None
    health_status: ProviderHealthStatus = ProviderHealthStatus.UNKNOWN
    successful_inferences: int = 0
    failed_inferences: int = 0
    avg_latency_ms: float = 0.0
    total_tokens_used: int = 0
    estimated_cost: float = 0.0

    def __repr__(self) -> str:
        success_rate = self.success_rate
        success_rate_repr = "None" if success_rate is None else f"{success_rate:.2f}"
        return (
            f"ProviderMetrics(name={self.name!r}, total_requests={self.successful_inferences + self.failed_inferences!r}, "
            f"success_rate={success_rate_repr})"
        )

    @property
    def success_rate(self) -> float | None:
        """Calculate success rate of inferences, or ``None`` when no inference has run."""
        total = self.successful_inferences + self.failed_inferences
        if total == 0:
            return None
        return self.successful_inferences / total

    def to_dict(self) -> dict[str, Any]:
        """Serialize provider metrics to a plain dict for JSON serialization and dashboard display."""
        return {
            "name": self.name,
            "provider_type": self.provider_type.value,
            "last_health_check": self.last_health_check.isoformat() if self.last_health_check else None,
            "health_status": self.health_status.value,
            "successful_inferences": self.successful_inferences,
            "failed_inferences": self.failed_inferences,
            "success_rate": self.success_rate,
            "avg_latency_ms": self.avg_latency_ms,
            "total_tokens_used": self.total_tokens_used,
            "estimated_cost": self.estimated_cost,
        }


__all__ = [
    "ProviderHealthStatus",
    "ProviderMetrics",
]
