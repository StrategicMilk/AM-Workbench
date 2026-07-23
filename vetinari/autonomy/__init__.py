"""Autonomy subsystem — trust framework and approval workflows.

Provides the AutonomyGovernor (policy engine controlling what actions
proceed without human approval) and the ApprovalQueue (SQLite-backed
queue for actions that need human sign-off).

This is the gate that makes Vetinari a *trustworthy* autonomous agent,
not just one that runs.
"""

from __future__ import annotations

from vetinari.autonomy.approval_queue import ApprovalQueue, get_approval_queue
from vetinari.autonomy.governor import (
    AutonomyGovernor,
    PermissionResult,
    PromotionSuggestion,
    get_governor,
)
from vetinari.autonomy.governor_permissions import enforce_scope_boundary
from vetinari.autonomy.wiring import wire_autonomy_and_notifications

__all__ = [
    "ApprovalQueue",
    "AutonomyGovernor",
    "PermissionResult",
    "PromotionSuggestion",
    "enforce_scope_boundary",
    "get_approval_queue",
    "get_governor",
    "wire_autonomy_and_notifications",
]
