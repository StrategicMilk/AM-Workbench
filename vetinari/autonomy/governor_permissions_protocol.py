"""Protocol contract for hosts composing the governor permission mixin."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

from vetinari.typing_support import MixinProtocol

if TYPE_CHECKING:
    from vetinari.autonomy.governor_models import ActionPolicy
    from vetinari.types import AutonomyLevel, AutonomyMode, DomainCareLevel, PermissionDecision


@runtime_checkable
class GovernorPermissionHost(MixinProtocol, Protocol):
    """Host attributes required by ``_GovernorPermissionMixin``."""

    _policies: dict[str, ActionPolicy]
    _autonomy_mode: AutonomyMode
    _domain_care_levels: dict[str, DomainCareLevel]

    def get_policy(self, action_type: str) -> ActionPolicy:
        """Return the configured policy for ``action_type``."""
        ...

    def _level_to_decision(self, level: AutonomyLevel) -> PermissionDecision: ...


__all__ = ["GovernorPermissionHost"]
