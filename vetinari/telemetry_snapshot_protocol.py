"""Protocol contract for hosts composing the telemetry snapshot mixin."""

from __future__ import annotations

import threading
from datetime import datetime
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from vetinari.typing_support import MixinProtocol

if TYPE_CHECKING:
    from vetinari.telemetry import AdapterMetrics, MemoryMetrics, PlanMetrics


@runtime_checkable
class TelemetrySnapshotHost(MixinProtocol, Protocol):
    """Host attributes required by ``TelemetrySnapshotMixin``."""

    adapter_metrics: dict[str, AdapterMetrics]
    memory_metrics: dict[str, MemoryMetrics]
    plan_metrics: PlanMetrics
    _lock: threading.RLock
    _start_time: datetime


__all__ = ["TelemetrySnapshotHost"]
