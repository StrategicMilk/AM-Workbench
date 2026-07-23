"""Agent specialist model tooling for AM Workbench."""

from __future__ import annotations

from vetinari.workbench.specialists.bindings import default_specialist_cards
from vetinari.workbench.specialists.cards import (
    BLOCKER_CALLER_NOT_ALLOWED,
    BLOCKER_CONFIDENCE_BELOW_ABSTAIN,
    BLOCKER_TASK_SCOPE_MISMATCH,
    SpecialistCalibrationEvent,
    SpecialistCallDecision,
    SpecialistCallOutcome,
    SpecialistModelCard,
    SpecialistModelError,
    SpecialistTask,
    decide_specialist_call,
    record_specialist_feedback,
)
from vetinari.workbench.specialists.fleet import (
    AgentRole,
    FleetRouteDecision,
    SpecialistFleet,
    SpecialistFleetError,
    SpecialistFleetMember,
    load_specialist_fleet,
)
from vetinari.workbench.specialists.registry import SpecialistModelRegistry, load_default_specialist_registry

__all__ = [
    "BLOCKER_CALLER_NOT_ALLOWED",
    "BLOCKER_CONFIDENCE_BELOW_ABSTAIN",
    "BLOCKER_TASK_SCOPE_MISMATCH",
    "AgentRole",
    "FleetRouteDecision",
    "SpecialistCalibrationEvent",
    "SpecialistCallDecision",
    "SpecialistCallOutcome",
    "SpecialistFleet",
    "SpecialistFleetError",
    "SpecialistFleetMember",
    "SpecialistModelCard",
    "SpecialistModelError",
    "SpecialistModelRegistry",
    "SpecialistTask",
    "decide_specialist_call",
    "default_specialist_cards",
    "load_default_specialist_registry",
    "load_specialist_fleet",
    "record_specialist_feedback",
]
