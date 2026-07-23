"""Plan Mode — generates, evaluates, and approves agent execution plans.

This is the planning step of the request pipeline:
Intake → **Planning** → Execution → Quality Gate → Assembly.

``PlanModeEngine`` implements the plan-first orchestration pattern:
1. Generate plan candidates from goals (LLM-powered, falling back to templates).
2. Evaluate and rank candidates by risk score.
3. Auto-approve low-risk plans in dry-run mode.
4. Support manual approval for high-risk plans.
5. Execute approved plans with subtask tracking.

Template data (domain/agent subtask skeletons) lives in ``plan_templates``
to keep this module under the 550-line ceiling.
"""

from __future__ import annotations

import logging
import os
import threading

from vetinari.memory import MemoryStore, get_memory_store
from vetinari.planning.plan_executor import _PlanExecutor
from vetinari.planning.plan_mode_generation import _PlanGenerationMixin
from vetinari.planning.plan_mode_store import _PlanStoreMixin
from vetinari.planning.plan_templates import AGENT_TEMPLATES, DOMAIN_TEMPLATES
from vetinari.planning.plan_types import (
    DefinitionOfDone,
    DefinitionOfReady,
    Plan,
    PlanApprovalRequest,
    PlanCandidate,
    PlanGenerationRequest,
    PlanRiskLevel,
    PlanStatus,
    StatusEnum,
    Subtask,
    TaskDomain,
)

logger = logging.getLogger(__name__)


__all__ = [
    "DEPTH_CAP",
    "DRY_RUN_ENABLED",
    "DRY_RUN_RISK_THRESHOLD",
    "MAX_CANDIDATES",
    "PLAN_MODE_DEFAULT",
    "PLAN_MODE_ENABLE",
    "DefinitionOfDone",
    "DefinitionOfReady",
    "Plan",
    "PlanApprovalRequest",
    "PlanCandidate",
    "PlanGenerationRequest",
    "PlanModeEngine",
    "PlanRiskLevel",
    "PlanStatus",
    "StatusEnum",
    "Subtask",
    "TaskDomain",
    "get_plan_engine",
    "init_plan_engine",
]

# -- Module-level config flags (read once at import time from environment) --
# Who reads: PlanModeEngine.__init__, get_plan_engine()
# Who writes: environment variables (before process start)
PLAN_MODE_DEFAULT = os.environ.get("PLAN_MODE_DEFAULT", "true").lower() in ("1", "true", "yes")
PLAN_MODE_ENABLE = os.environ.get("PLAN_MODE_ENABLE", "true").lower() in ("1", "true", "yes")
DRY_RUN_ENABLED = os.environ.get("DRY_RUN_ENABLED", "false").lower() in ("1", "true", "yes")
DRY_RUN_RISK_THRESHOLD = float(os.environ.get("DRY_RUN_RISK_THRESHOLD", "0.25"))
DEPTH_CAP = int(os.environ.get("PLAN_DEPTH_CAP", "16"))
MAX_CANDIDATES = int(os.environ.get("PLAN_MAX_CANDIDATES", "3"))


class PlanModeEngine(_PlanGenerationMixin, _PlanStoreMixin, _PlanExecutor):
    """Plan Mode Engine — generates, evaluates, and approves plans.

    This engine implements the plan-first orchestration pattern:
    1. Generate plan candidates from goals
    2. Evaluate and rank candidates
    3. Allow dry-run mode with auto-approval for low-risk plans
    4. Support manual approval for high-risk plans
    5. Execute approved plans with subtask tracking
    """

    def __init__(self, memory_store: MemoryStore | None = None):
        self.memory = memory_store or get_memory_store()
        self.plan_depth_cap = DEPTH_CAP
        self.max_candidates = MAX_CANDIDATES
        self.dry_run_risk_threshold = DRY_RUN_RISK_THRESHOLD

        self._domain_templates = self._load_domain_templates()
        self._agent_templates = self._load_agent_templates()

    @staticmethod
    def _load_domain_templates() -> dict[TaskDomain, list[dict]]:
        """Return domain-specific subtask template dicts from plan_templates module."""
        return dict(DOMAIN_TEMPLATES)

    @staticmethod
    def _load_agent_templates() -> dict[str, list[dict]]:
        """Return agent-specific subtask template dicts from plan_templates module."""
        return dict(AGENT_TEMPLATES)


# -- Module-level singleton (double-checked locking pattern) --
# Who writes: get_plan_engine(), init_plan_engine()
# Who reads: get_plan_engine()
# Lock: _plan_engine_lock protects the check-then-assign in get_plan_engine()
_plan_engine: PlanModeEngine | None = None
_plan_engine_lock = threading.Lock()


def get_plan_engine() -> PlanModeEngine:
    """Get or create the global PlanModeEngine singleton.

    Uses double-checked locking so only one instance is created even
    under concurrent access.

    Resolves ``_plan_engine`` through ``sys.modules`` rather than the
    function's own ``__globals__`` dict so that test patches applied via
    ``unittest.mock.patch("vetinari.planning.plan_mode._plan_engine", ...)``
    are always visible regardless of which module-object snapshot the calling
    closure was captured from.

    Returns:
        The shared PlanModeEngine instance.
    """
    import sys as _sys

    _mod = _sys.modules[__name__]
    if _mod._plan_engine is None:
        with _plan_engine_lock:
            if _mod._plan_engine is None:
                _mod._plan_engine = PlanModeEngine()
    return _mod._plan_engine


def init_plan_engine(memory_store: MemoryStore | None = None) -> PlanModeEngine:
    """Replace the module-level PlanModeEngine singleton with a fresh instance.

    Intended for tests and startup code that needs a clean engine with a
    specific memory store.

    Args:
        memory_store: Optional MemoryStore for persisting plan data;
            uses the default if omitted.

    Returns:
        The newly created PlanModeEngine instance.
    """
    global _plan_engine
    _plan_engine = PlanModeEngine(memory_store=memory_store)
    return _plan_engine
