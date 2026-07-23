"""Public exports for Workbench shadow snapshot undo contracts."""

from __future__ import annotations

from vetinari.workbench.shadow_undo.contracts import (
    MissionControlUndoItem,
    Reversibility,
    RollbackAttempt,
    RollbackStrategy,
    ShadowOperation,
    ShadowOperationKind,
    ShadowRollbackPlan,
    ShadowRollbackStatus,
    ShadowSnapshot,
    ShadowSnapshotError,
    UndoabilityStatus,
)
from vetinari.workbench.shadow_undo.runtime import (
    ShadowSnapshotStore,
    build_mission_control_undo_items,
    capture_shadow_snapshot,
    rollback_shadow_snapshot,
)

__all__ = [
    "MissionControlUndoItem",
    "Reversibility",
    "RollbackAttempt",
    "RollbackStrategy",
    "ShadowOperation",
    "ShadowOperationKind",
    "ShadowRollbackPlan",
    "ShadowRollbackStatus",
    "ShadowSnapshot",
    "ShadowSnapshotError",
    "ShadowSnapshotStore",
    "UndoabilityStatus",
    "build_mission_control_undo_items",
    "capture_shadow_snapshot",
    "rollback_shadow_snapshot",
]
