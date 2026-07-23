"""Route drift-governance findings into kaizen aggregation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from vetinari.security.redaction import redact_value


@dataclass(frozen=True, slots=True)
class DriftSignalRouter:
    """Forward drift audit findings to the kaizen aggregator."""

    aggregator: Any

    def route(self, finding: Any) -> Any:
        """Run route.

        Returns:
            Receipt returned by the aggregator method that accepted the drift signal.

        Raises:
            TypeError: If the aggregator exposes no drift-signal receiver.
        """
        if hasattr(self.aggregator, "route_drift_signal"):
            return self.aggregator.route_drift_signal(redact_value(finding))
        if hasattr(self.aggregator, "record_external_signal"):
            return self.aggregator.record_external_signal({"kind": "drift", "finding": redact_value(finding)})
        raise TypeError("aggregator cannot receive drift signals")


def route_drift_signal(finding: Any, aggregator: Any) -> Any:
    """Forward one drift finding through the kaizen drift router.

    Args:
        finding: Finding value consumed by route_drift_signal().
        aggregator: Aggregator value consumed by route_drift_signal().

    Returns:
        Outcome produced by route_drift_signal().
    """
    return DriftSignalRouter(aggregator).route(finding)


__all__ = ["DriftSignalRouter", "route_drift_signal"]
