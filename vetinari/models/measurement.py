"""Measurement models for comparable resource accounting baselines."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class ResourceBaseline:
    """Comparable resource baseline record."""

    workload_id: str
    metric: str
    value: float
    unit: str
    methodology: str
    comparable_with_versions: tuple[str, ...]
    sample_count: int
    metadata: dict[str, Any] = field(default_factory=dict)

    def __repr__(self) -> str:
        return (
            "ResourceBaseline("
            f"workload_id={self.workload_id!r}, metric={self.metric!r}, "
            f"value={self.value!r}, unit={self.unit!r}, sample_count={self.sample_count})"
        )

    def __post_init__(self) -> None:
        required_strings = {
            "workload_id": self.workload_id,
            "metric": self.metric,
            "unit": self.unit,
            "methodology": self.methodology,
        }
        for field_name, value in required_strings.items():
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} must be a non-empty string")
        if not self.comparable_with_versions:
            raise ValueError("comparable_with_versions must not be empty")
        if self.sample_count < 1:
            raise ValueError("sample_count must be at least 1")

    @classmethod
    def from_workload_metric(
        cls,
        workload_id: str,
        metric: str,
        payload: dict[str, Any],
    ) -> ResourceBaseline:
        """Build a baseline from one workload metric in ``resource_baselines.json``.

        Args:
            workload_id: Workload identifier.
            metric: Metric name within the workload.
            payload: Metric payload from config.

        Returns:
            A validated resource baseline.

        Raises:
            ValueError: if required metric fields are missing or invalid.
        """
        missing = [
            key
            for key in ("unit", "methodology", "comparable_with_versions", "sample_count", "value")
            if key not in payload
        ]
        if missing:
            raise ValueError(f"resource baseline {workload_id}.{metric} missing fields: {', '.join(missing)}")
        return cls(
            workload_id=workload_id,
            metric=metric,
            value=float(payload["value"]),
            unit=str(payload["unit"]),
            methodology=str(payload["methodology"]),
            comparable_with_versions=tuple(str(version) for version in payload["comparable_with_versions"]),
            sample_count=int(payload["sample_count"]),
            metadata=dict(payload.get("metadata", {})),
        )


def load_resource_baselines(payload: dict[str, Any]) -> list[ResourceBaseline]:
    """Load all resource baselines from the repository config shape.

    Returns:
        Validated baseline rows.
    """
    baselines: list[ResourceBaseline] = []
    for workload_id, workload in dict(payload.get("workloads", {})).items():
        sample_count = int(workload.get("sample_count", 0))
        methodology = str(workload.get("methodology", ""))
        comparable = tuple(str(version) for version in workload.get("comparable_with_versions", ()))
        metric_units = dict(workload.get("units", {}))
        for metric, value in workload.items():
            if metric in {"sample_count", "methodology", "comparable_with_versions", "units", "metadata"}:
                continue
            if not isinstance(value, int | float):
                continue
            baselines.append(
                ResourceBaseline.from_workload_metric(
                    str(workload_id),
                    str(metric),
                    {
                        "value": value,
                        "unit": metric_units.get(metric),
                        "methodology": methodology,
                        "comparable_with_versions": comparable,
                        "sample_count": sample_count,
                        "metadata": workload.get("metadata", {}),
                    },
                )
            )
    return baselines


__all__ = ["ResourceBaseline", "load_resource_baselines"]
