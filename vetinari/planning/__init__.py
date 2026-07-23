"""Planning, decomposition, and task management subsystem.

Canonical module ownership:
  - ``vetinari.planning.plan_mode``   — plan generation (PlanModeEngine); use this for
    creating new plans from a goal string.
  - ``vetinari.planning.plan_types``  — all planning-domain types: Plan, Subtask,
    PlanRiskLevel, DefinitionOfDone, etc.
  - ``vetinari.planning.plan_api``    — REST layer; Flask Blueprint for all
    /api/v1/plans/* endpoints.
  - ``vetinari.planning.decomposition`` — task decomposition from a high-level plan into
    typed Subtask objects.
  - ``vetinari.planning.plan_validator`` — structural and semantic plan validation;
    runs after goal decomposition and before execution plan creation.

The ``planning.planning`` submodule is a deprecated compatibility shim for
legacy wave-based plan metadata. It is loaded lazily only when callers request
legacy symbols such as ``PlanManager`` or ``get_plan_manager``.
"""

from __future__ import annotations

from vetinari.agents.contracts import AttestedArtifact
from vetinari.planning.context_bundle import (
    ContextBundleItem,
    ContextBundleResolver,
    get_default_resolver,
    resolve_context_bundles,
)
from vetinari.planning.decision_tree import (
    DecisionNode,
    DecisionTreeExtractor,
    DecisionTreeResult,
    Option,
    extract_decisions,
)
from vetinari.planning.decomposition import DecompositionEngine, SubtaskSpec
from vetinari.planning.delegation_budget import DelegationBudget, DelegationBudgetExceededError
from vetinari.planning.plan_graph import CyclicDependencyError, PlanDepthError, PlanGraph, PlanWidthError
from vetinari.planning.plan_reviewer import PlanReviewer, parse_outcome
from vetinari.planning.plan_types import (
    DefinitionOfDone,
    DefinitionOfReady,
    Plan,
    PlanApprovalRequest,
    PlanGenerationRequest,
    PlanRiskLevel,
    TaskDomain,
    TaskRationale,
)
from vetinari.planning.plan_validator import (
    ValidationResult,
    validate_plan,
)
from vetinari.planning.review_outcome import (
    OverrideAppeal,
    PlanDecision,
    PlanReviewOutcome,
    RefusalReason,
)
from vetinari.planning.spec_frame import SpecFrame
from vetinari.planning.subtask_tree import SubtaskTree

_LEGACY_EXPORTS = frozenset({
    "PlanManager",
    "PlanningExecutionPlan",
    "Wave",
    "WaveStatus",
    "get_plan_manager",
})
_NON_GOAL_EXPORTS = frozenset({
    "MatchEvidence",
    "MatchRule",
    "NonGoal",
    "NonGoalStore",
    "check_non_goals",
})

__all__ = [
    "AttestedArtifact",
    "ContextBundleItem",
    "ContextBundleResolver",
    "CyclicDependencyError",
    "DecisionNode",
    "DecisionTreeExtractor",
    "DecisionTreeResult",
    "DecompositionEngine",
    "DefinitionOfDone",
    "DefinitionOfReady",
    "DelegationBudget",
    "DelegationBudgetExceededError",
    "MatchEvidence",
    "MatchRule",
    "NonGoal",
    "NonGoalStore",
    "Option",
    "OverrideAppeal",
    "Plan",
    "PlanApprovalRequest",
    "PlanDecision",
    "PlanDepthError",
    "PlanGenerationRequest",
    "PlanGraph",
    "PlanManager",
    "PlanReviewOutcome",
    "PlanReviewer",
    "PlanRiskLevel",
    "PlanWidthError",
    "PlanningExecutionPlan",
    "RefusalReason",
    "SpecFrame",
    "SubtaskSpec",
    "SubtaskTree",
    "TaskDomain",
    "TaskRationale",
    "ValidationResult",
    "Wave",
    "WaveStatus",
    "check_non_goals",
    "extract_decisions",
    "get_default_resolver",
    "get_plan_manager",
    "parse_outcome",
    "resolve_context_bundles",
    "validate_plan",
]


def __getattr__(name: str):
    """Resolve deprecated wave-plan exports only when legacy callers ask for them."""
    if name in _LEGACY_EXPORTS:
        import importlib
        import warnings

        warnings.warn(
            f"vetinari.planning.{name} is a deprecated legacy wave-plan export",
            DeprecationWarning,
            stacklevel=2,
        )
        _legacy_planning = importlib.import_module("vetinari.planning.planning")

        value = getattr(_legacy_planning, name)
        globals()[name] = value
        return value
    if name in _NON_GOAL_EXPORTS:
        import importlib

        non_goals = importlib.import_module("vetinari.planning.non_goals")
        value = getattr(non_goals, name)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
