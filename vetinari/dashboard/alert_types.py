"""Dashboard alert data types."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, cast

from vetinari.utils.serialization import dataclass_to_dict


class AlertSeverity(str, Enum):
    """Alert severity."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class AlertCondition(str, Enum):
    """Alert condition."""

    GREATER_THAN = "gt"
    LESS_THAN = "lt"
    EQUALS = "eq"


@dataclass
class AlertThreshold:
    """Defines a rule to evaluate against a MetricsSnapshot value."""

    name: str
    metric_key: str
    condition: AlertCondition
    threshold_value: float
    severity: AlertSeverity = AlertSeverity.MEDIUM
    channels: list[str] = field(default_factory=lambda: ["log"])
    duration_seconds: int = 0
    fail_on_missing_metric: bool = False
    runbook_url: str = ""

    def __repr__(self) -> str:
        return f"AlertThreshold(name={self.name!r}, metric_key={self.metric_key!r}, severity={self.severity!r})"

    def to_dict(self) -> dict[str, Any]:
        """Serialize this alert rule to a plain dictionary."""
        return cast(dict[str, Any], dataclass_to_dict(self))


@dataclass(frozen=True, slots=True)
class AlertRecord:
    """An alert that has been triggered."""

    threshold: AlertThreshold
    current_value: float
    trigger_time: float = field(default_factory=time.time)

    def __repr__(self) -> str:
        return f"AlertRecord(threshold={self.threshold.name!r}, current_value={self.current_value!r})"

    def to_dict(self) -> dict[str, Any]:
        """Serialize this fired alert to a plain dictionary."""
        return cast(dict[str, Any], dataclass_to_dict(self))
