"""Immutable registry for specialist model cards."""

from __future__ import annotations

from dataclasses import dataclass

from vetinari.workbench.specialists.bindings import default_specialist_cards
from vetinari.workbench.specialists.cards import (
    SpecialistModelCard,
    SpecialistModelError,
    SpecialistTask,
    decide_specialist_call,
)


@dataclass(frozen=True, slots=True)
class SpecialistModelRegistry:
    """Lookup surface for agent specialist cards."""

    cards: tuple[SpecialistModelCard, ...]

    def __post_init__(self) -> None:
        if not self.cards:
            raise SpecialistModelError("registry cards must be non-empty")
        if len({card.card_id for card in self.cards}) != len(self.cards):
            raise SpecialistModelError("specialist card ids must be unique")
        tasks = {card.task for card in self.cards}
        if tasks != set(SpecialistTask):
            missing = sorted(task.value for task in set(SpecialistTask) - tasks)
            raise SpecialistModelError(f"specialist registry task coverage mismatch missing={missing}")

    def card_for_task(self, task: SpecialistTask | str) -> SpecialistModelCard:
        """Return the card for one specialist task.

        Returns:
            SpecialistModelCard value produced by card_for_task().

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        selected = SpecialistTask(task)
        for card in self.cards:
            if card.task == selected:
                return card
        raise SpecialistModelError(f"no specialist card for task {selected.value!r}")

    def decide_call(self, *, task: SpecialistTask | str, caller: str, confidence: float):
        """Resolve a task to a card and evaluate the call gate.

        Returns:
            Value produced by decide_call().
        """
        card = self.card_for_task(task)
        return decide_specialist_call(card, requested_task=task, caller=caller, confidence=confidence)

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-compatible registry payload."""
        return {"cards": [card.to_dict() for card in self.cards]}


def load_default_specialist_registry() -> SpecialistModelRegistry:
    """Load the built-in specialist registry."""
    return SpecialistModelRegistry(default_specialist_cards())


__all__ = ["SpecialistModelRegistry", "load_default_specialist_registry"]
