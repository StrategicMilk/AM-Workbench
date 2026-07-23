"""Protocol contract for hosts composing the governor trust mixin."""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from vetinari.typing_support import MixinProtocol

if TYPE_CHECKING:
    from vetinari.autonomy.governor_models import ActionPolicy, PendingPromotion, TrustRecord
    from vetinari.types import AutonomyLevel


@runtime_checkable
class GovernorTrustHost(MixinProtocol, Protocol):
    """Host attributes required by ``_GovernorTrustMixin``."""

    _lock: threading.Lock
    _policies: dict[str, ActionPolicy]
    _trust_records: dict[str, TrustRecord]
    _pending_promotions: dict[str, PendingPromotion]
    _vetoed_actions: set[str]
    _default_level: AutonomyLevel

    def get_policy(self, action_type: str) -> ActionPolicy:
        """Return the configured policy for ``action_type``."""
        ...


__all__ = ["GovernorTrustHost"]
