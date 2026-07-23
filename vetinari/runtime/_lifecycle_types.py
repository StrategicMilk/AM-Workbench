"""Module-level types and constants for the AppLifecycleController.

Separated from app_lifecycle.py to keep that module within the project's
550-LOC ceiling. All public symbols are re-exported from app_lifecycle.py.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from enum import Enum
from typing import Any

from vetinari.constants import OUTPUTS_DIR
from vetinari.desktop.contracts import LifecycleAction

# -- State file location --
_DEFAULT_STATE_DIR = OUTPUTS_DIR / "workbench" / "launcher"
_STATE_FILENAME = "app_lifecycle_state.jsonl"

# -- Singleton guards (owned by app_lifecycle.py; defined here for import convenience) --
_INSTANCE_LOCK = threading.Lock()

# -- Allowlisted lifecycle action sets --
_ALLOWLISTED_LIFECYCLE_ACTIONS = frozenset(action.value for action in LifecycleAction)
_ADMIN_ACTIONS = frozenset({
    LifecycleAction.STOP.value,
    LifecycleAction.RESTART.value,
    LifecycleAction.QUIT_COMPLETELY.value,
    LifecycleAction.FORCE_QUIT.value,
    LifecycleAction.SUPPORT_BUNDLE.value,
})
_TRANSITIONAL_BROWSER_ACTIONS = frozenset({
    LifecycleAction.OPEN.value,
    LifecycleAction.CLOSE_WINDOW.value,
    LifecycleAction.KEEP_IN_BACKGROUND.value,
    LifecycleAction.CRASH_RECOVER.value,
})


class LifecycleState(str, Enum):
    """Runtime contract for LifecycleState."""

    STOPPED = "stopped"
    STARTING = "starting"
    WAITING_FOR_HEALTH = "waiting_for_health"
    RUNNING_HEALTHY = "running_healthy"
    RUNNING_DEGRADED = "running_degraded"
    STOPPING_GRACEFUL = "stopping_graceful"
    STOPPING_FORCED = "stopping_forced"
    CRASHED_RECOVERING = "crashed_recovering"


@dataclass(frozen=True, slots=True)
class LifecycleShutdownProtocol:
    """Default timing parameters for the graceful-shutdown sequence."""

    grace_window_seconds: float = 30.0  # Seconds to wait for each resource releaser before declaring it timed out.
    force_after_seconds: float = 60.0  # Hard deadline after which remaining releasers are skipped and escalated.


ShutdownProtocol = LifecycleShutdownProtocol


@dataclass(frozen=True, slots=True)
class ShutdownReport:
    """Runtime contract for ShutdownReport."""

    state_before: LifecycleState
    state_after: LifecycleState
    released_resources: tuple[str, ...]
    failed_releasers: tuple[str, ...]
    escalated_to_force: bool
    checkpointed: bool
    receipt_id: str

    def to_dict(self) -> dict[str, Any]:
        """Serialize shutdown report to a plain dict for JSONL persistence and dashboards."""
        return {
            "state_before": self.state_before.value,
            "state_after": self.state_after.value,
            "released_resources": list(self.released_resources),
            "failed_releasers": list(self.failed_releasers),
            "escalated_to_force": self.escalated_to_force,
            "checkpointed": self.checkpointed,
            "receipt_id": self.receipt_id,
        }

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return (
            f"ShutdownReport(state_before={self.state_before!r}, "
            f"state_after={self.state_after!r}, "
            f"released_resources={self.released_resources!r})"
        )


__all__ = [
    "_ADMIN_ACTIONS",
    "_ALLOWLISTED_LIFECYCLE_ACTIONS",
    "_DEFAULT_STATE_DIR",
    "_INSTANCE_LOCK",
    "_STATE_FILENAME",
    "_TRANSITIONAL_BROWSER_ACTIONS",
    "LifecycleShutdownProtocol",
    "LifecycleState",
    "ShutdownProtocol",
    "ShutdownReport",
]
