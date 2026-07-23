"""Lease lifecycle behavior for protected VRAM residency.

Model leases mark a loaded or reserved model as actively used by inference so
eviction decisions cannot remove it while an agent still depends on it.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from vetinari.models.vram_types import ModelLease

logger = logging.getLogger(__name__)


class _VRAMLeaseMixin:
    """Lease operations mixed into ``VRAMManager``."""

    if TYPE_CHECKING:
        _estimates: Any
        _leases: Any
        _lock_rw: Any
        _reservations: Any

    def acquire_lease(self, model_id: str, holder_id: str, max_duration_s: float = 300.0) -> bool:
        """Claim a model for active inference, protecting it from eviction.

        Args:
            model_id: Model to lease.
            holder_id: Unique identifier for the holder, such as
                ``"worker:task-42"``.
            max_duration_s: Lease auto-expiry in seconds.

        Returns:
            True if the lease was granted because the model is tracked as
            loaded or reserved.
        """
        with self._lock_rw:
            self._reap_expired_leases()
            is_known = model_id in self._estimates or model_id in self._reservations
            if not is_known:
                logger.warning("Lease denied for %s - model not tracked as loaded", model_id)
                return False
            self._leases[holder_id] = ModelLease(
                model_id=model_id,
                holder_id=holder_id,
                max_duration_s=max_duration_s,
            )
            logger.debug("Lease granted: %s -> %s (%.0fs)", holder_id, model_id, max_duration_s)
            return True

    def release_lease(self, holder_id: str) -> None:
        """Release a model lease after inference completes.

        Args:
            holder_id: The holder identifier used when acquiring.
        """
        with self._lock_rw:
            removed = self._leases.pop(holder_id, None)
            if removed:
                logger.debug("Lease released: %s -> %s", holder_id, removed.model_id)

    def is_leased(self, model_id: str) -> bool:
        """Return True if any non-expired lease exists for this model.

        Args:
            model_id: Model to check.

        Returns:
            True if at least one active lease protects this model.
        """
        with self._lock_rw:
            self._reap_expired_leases()
            return any(lease.model_id == model_id for lease in self._leases.values())

    def active_lease_count(self) -> int:
        """Return the number of active leases after expiring stale claims.

        Returns:
            Count of leases currently held.
        """
        with self._lock_rw:
            self._reap_expired_leases()
            return len(self._leases)

    def get_leased_model_ids(self) -> set[str]:
        """Return model IDs that have at least one active lease.

        Returns:
            Set of model IDs currently referenced by at least one non-expired
            lease.
        """
        with self._lock_rw:
            self._reap_expired_leases()
            return {lease.model_id for lease in self._leases.values()}

    def _reap_expired_leases(self) -> None:
        """Remove leases that have exceeded their max duration."""
        expired = [holder_id for holder_id, lease in self._leases.items() if lease.is_expired]
        for holder_id in expired:
            lease = self._leases.pop(holder_id)
            logger.warning(
                "Lease expired: %s -> %s (held %.0fs)",
                holder_id,
                lease.model_id,
                lease.max_duration_s,
            )
