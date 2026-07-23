"""Workbench automation builder contracts and simulation runtime."""

from __future__ import annotations

from vetinari.workbench.automation.builder import (
    ALLOWED_TRIGGER_SOURCES,
    AutomationAction,
    AutomationApproval,
    AutomationBudget,
    AutomationCondition,
    AutomationDefinition,
    AutomationFailurePolicy,
    AutomationLease,
    AutomationQuietHours,
    AutomationRateLimit,
    AutomationRollback,
    AutomationRunReceipt,
    AutomationSimulation,
    AutomationValidationError,
    SimulationContext,
    build_automation_definition,
    simulate_automation,
)

__all__ = [
    "ALLOWED_TRIGGER_SOURCES",
    "AutomationAction",
    "AutomationApproval",
    "AutomationBudget",
    "AutomationCondition",
    "AutomationDefinition",
    "AutomationFailurePolicy",
    "AutomationLease",
    "AutomationQuietHours",
    "AutomationRateLimit",
    "AutomationRollback",
    "AutomationRunReceipt",
    "AutomationSimulation",
    "AutomationValidationError",
    "SimulationContext",
    "build_automation_definition",
    "simulate_automation",
]
