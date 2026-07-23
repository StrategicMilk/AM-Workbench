"""Safe repinning helpers for Workbench model quick choices."""

from __future__ import annotations

from dataclasses import dataclass

from vetinari.workbench.model_choices.config_loader import QuickChoicesConfig
from vetinari.workbench.model_choices.contracts import InactiveReason, ModelQuickChoice, Surface
from vetinari.workbench.model_registry import DeprecationState, ModelStage, RegistrySnapshot


@dataclass(frozen=True, slots=True)
class RepinDecision:
    """Result of evaluating whether a pinned model version should move."""

    repinned: bool
    previous_version_id: str | None
    new_version_id: str | None
    reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-serializable representation."""
        return {
            "repinned": self.repinned,
            "previous_version_id": self.previous_version_id,
            "new_version_id": self.new_version_id,
            "reasons": list(self.reasons),
        }

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"RepinDecision(repinned={self.repinned!r}, previous_version_id={self.previous_version_id!r}, new_version_id={self.new_version_id!r})"


def safe_repin(
    choice: ModelQuickChoice,
    snapshot: RegistrySnapshot,
    config: QuickChoicesConfig | None = None,
    surface: Surface | None = None,
) -> RepinDecision:
    """Decide whether ``choice`` can move to a safer serving version.

    Args:
        choice: Choice value consumed by safe_repin().
        snapshot: Snapshot value consumed by safe_repin().
        config: Config value consumed by safe_repin().
        surface: Surface value consumed by safe_repin().

    Returns:
        RepinDecision value produced by safe_repin().
    """
    target_surface = surface or choice.surface
    previous_id = choice.pinned_version_id
    versions = tuple(version for version in snapshot.versions if version.model_id == choice.model_ref.model_id)
    cards = {card.card_id: card for card in snapshot.cards}
    previous = next((version for version in versions if version.version_id == previous_id), None)
    previous_card = cards.get(previous.card_id) if previous is not None else None
    if previous is not None and previous_card is not None and previous_card.provider != choice.model_ref.provider:
        return RepinDecision(False, previous_id, previous_id, (InactiveReason.BLOCKED_BY_POLICY.value,))

    if _is_surface_eligible(previous, previous_card, config, target_surface):
        return RepinDecision(False, previous_id, previous_id, ("current_pin_still_serving",))

    candidates = []
    for version in versions:
        card = cards.get(version.card_id)
        if card is None or card.provider != choice.model_ref.provider:
            continue
        if _is_surface_eligible(version, card, config, target_surface):
            candidates.append(version)
    if not candidates:
        return RepinDecision(False, previous_id, previous_id, ("no surface-eligible serving successor",))
    successor = max(candidates, key=lambda version: version.created_at_utc)
    if successor.version_id == previous_id:
        return RepinDecision(False, previous_id, previous_id, ("current_pin_still_serving",))
    successor_card = cards.get(successor.card_id)
    if successor_card is None or not getattr(successor_card, "artifact_sha256", None):
        return RepinDecision(
            False,
            previous_id,
            previous_id,
            ("repin_blocked_no_artifact_integrity_evidence",),
        )
    return RepinDecision(True, previous_id, successor.version_id, ("repinned_to_serving_successor",))


def _is_surface_eligible(version: object, card: object, config: QuickChoicesConfig | None, surface: Surface) -> bool:
    if version is None or card is None:
        return False
    if getattr(version.stage, "value", version.stage) != ModelStage.SERVING.value:
        return False
    if getattr(version.deprecation_state, "value", version.deprecation_state) in {
        DeprecationState.SCHEDULED.value,
        DeprecationState.DEPRECATED.value,
    }:
        return False
    if config is None:
        return True
    surface_config = config.surface_config(surface)
    capabilities = set(card.capabilities)
    return set(surface_config.required_capabilities).issubset(capabilities) and not (
        capabilities & set(surface_config.disallowed_capabilities)
    )


__all__ = ["RepinDecision", "safe_repin"]
