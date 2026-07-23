"""Protocol contract for hosts composing the workbench scheduler policy mixin."""

from __future__ import annotations

import threading
from datetime import time as dt_time
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from vetinari.typing_support import MixinProtocol

if TYPE_CHECKING:
    from vetinari.runtime.workbench_scheduler_bridge import RustSchedulerBridge
    from vetinari.runtime.workbench_scheduler_types import Lane


@runtime_checkable
class WorkbenchSchedulerPolicyHost(MixinProtocol, Protocol):
    """Host attributes required by ``WorkbenchSchedulerPolicyMixin``."""

    _active_count: dict[Lane, int]
    _config: dict[str, Any]
    _lane_capacity: dict[Lane, int]
    _rust_bridge: RustSchedulerBridge
    _state_lock: threading.Lock
    _training_windows_parsed: list[tuple[dt_time, dt_time]]


__all__ = ["WorkbenchSchedulerPolicyHost"]
