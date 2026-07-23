"""Plan execution tracking store.

Tracks plan execution history, subtask outcomes, and model performance
metrics in SQLite with a JSON fallback. Used by ``FeedbackLoop`` and
``PlanModeEngine``.

Note: This is *not* the same system as ``UnifiedMemoryStore`` (agent
episodic/semantic memory).
"""

from __future__ import annotations

import contextlib
import os
import threading

from vetinari.constants import _PROJECT_ROOT
from vetinari.database import get_connection as get_connection
from vetinari.memory.plan_pruning import PLAN_RETENTION_DAYS, PlanPruneError
from vetinari.memory.plan_tracking_json import PlanTrackingJsonMixin
from vetinari.memory.plan_tracking_lifecycle import PlanTrackingLifecycleMixin
from vetinari.memory.plan_tracking_metrics import PlanTrackingMetricsMixin
from vetinari.memory.plan_tracking_records import PlanTrackingRecordsMixin
from vetinari.memory.plan_tracking_schema import PlanTrackingSchemaMixin
from vetinari.utils import privacy_receipt

MODEL_PERFORMANCE_LIFECYCLE = {
    "store_id": "model_performance_store",
    "retention_days": PLAN_RETENTION_DAYS,
    "redaction_applied": True,
    "deletion_mechanism": "MemoryStore.prune_old_plans",
    "owner_ref": "docs/security/data-inventory.json#model_performance_store",
    "privacy_receipt": privacy_receipt(
        privacy_class="operational",
        retention_days=PLAN_RETENTION_DAYS,
        source="plan_tracking.model_performance_lifecycle",
        redaction_applied=True,
    ),
}

__all__ = ["MODEL_PERFORMANCE_LIFECYCLE", "PLAN_ADMIN_TOKEN", "MemoryStore", "PlanPruneError", "get_connection"]


PLAN_ADMIN_TOKEN = os.environ.get("PLAN_ADMIN_TOKEN", "")
_CONTEXTLIB_FOR_DESTRUCTOR_COMPAT = contextlib


class MemoryStore(
    PlanTrackingSchemaMixin,
    PlanTrackingJsonMixin,
    PlanTrackingRecordsMixin,
    PlanTrackingMetricsMixin,
    PlanTrackingLifecycleMixin,
):
    """Plan execution tracking store.

    MemoryStore tracks plan execution history, subtask outcomes, and model
    performance metrics in SQLite/JSON. It is used by FeedbackLoop and
    PlanModeEngine and is distinct from UnifiedMemoryStore agent memory.
    """

    def __init__(self, db_path: str | None = None, use_json_fallback: bool = False):
        self.use_json_fallback = use_json_fallback
        self._lock = threading.Lock()
        self._db_path = db_path
        if db_path:
            self._json_path = db_path.replace(".db", ".json")
        else:
            self._json_path = str(_PROJECT_ROOT / ".vetinari" / "vetinari_memory.json")

        if use_json_fallback:
            self._init_json_store()
        else:
            self._init_sqlite()

    @property
    def db_path(self) -> str | None:
        """Explicit SQLite database path supplied for this store, when any."""
        return self._db_path


_memory_store: MemoryStore | None = None
_memory_store_lock = threading.Lock()


def get_memory_store() -> MemoryStore:
    """Get or create the global memory store instance.

    Returns:
        The MemoryStore singleton.
    """
    global _memory_store
    if _memory_store is None:
        with _memory_store_lock:
            if _memory_store is None:
                use_json = os.environ.get("PLAN_USE_JSON_FALLBACK", "false").lower() in ("1", "true", "yes")
                _memory_store = MemoryStore(use_json_fallback=use_json)
    return _memory_store


def init_memory_store(db_path: str | None = None, use_json_fallback: bool = False) -> MemoryStore:
    """Initialize a new memory store instance.

    Args:
        db_path: Ignored for the singleton; retained for backward compatibility.
        use_json_fallback: If True, use JSON storage instead of SQLite.

    Returns:
        The newly created MemoryStore instance.
    """
    global _memory_store
    _memory_store = MemoryStore(use_json_fallback=use_json_fallback)
    return _memory_store
