"""Model pool compatibility facade.

The runtime implementation lives in :mod:`vetinari.models.model_pool`.  This
module keeps the older import path alive without mutating the runtime class at
import time.
"""

from __future__ import annotations

from vetinari.models.model_pool import ModelPool as _RuntimeModelPool


class CompatibleModelPool(_RuntimeModelPool):
    """Backward-compatible model pool with explicit stale-error recovery."""

    _last_discovery_error: str

    def clear_error_state(self, model_id: str) -> None:
        """Clear discovery failure flags after a model-specific recovery.

        Older callers imported ``vetinari.adapters.pool.ModelPool`` and then
        called ``clear_error_state`` after a successful retry.  The previous
        wrapper patched that method onto the shared runtime class.  Keeping the
        method on this compatibility subclass avoids global side effects while
        still making stale fallback state observable and recoverable.

        Raises:
            KeyError: If ``model_id`` is not a known model and does not match
                the recorded discovery error.
        """
        if not model_id:
            return
        last_error = str(getattr(self, "_last_discovery_error", "") or "")
        known_ids = {
            str(model.get("id") or model.get("name") or "")
            for model in getattr(self, "models", [])
            if isinstance(model, dict)
        }
        if model_id in known_ids or model_id in last_error:
            self._last_discovery_error = ""
            self._discovery_failed = False
            self._fallback_active = False
            return
        raise KeyError(f"model_id {model_id!r} not found in pool and not in last_error")


ModelPool = CompatibleModelPool

__all__ = ["ModelPool"]
