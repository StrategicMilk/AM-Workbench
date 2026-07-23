"""Helper functions for graph execution layer scheduling and receipts."""

from __future__ import annotations

import hashlib
import importlib
import json
import logging
import threading
from typing import Any

from vetinari.orchestration.plan_diff import PlanDiff
from vetinari.types import StatusEnum

logger = logging.getLogger(__name__)


# Lazy loader for VRAM manager — avoids importing heavy GPU code at module level.
# We store the module reference (not a direct function reference) so that
# unittest.mock.patch("vetinari.models.vram_manager.get_vram_manager", ...) is
# intercepted correctly in tests — patching replaces the attribute on the module
# object, not on a cached function reference.
_vram_manager_mod = None
_vram_manager_checked = False


def _lazy_get_vram_manager():
    """Return the VRAMManager singleton, or None if unavailable."""
    global _vram_manager_mod, _vram_manager_checked
    if not _vram_manager_checked:
        try:
            import vetinari.models.vram_manager as _mod

            _vram_manager_mod = _mod
        except ImportError:
            _vram_manager_mod = None
        _vram_manager_checked = True
    if _vram_manager_mod is None:
        return None
    return _vram_manager_mod.get_vram_manager()


_TERMINAL_NODE_STATUSES = {StatusEnum.COMPLETED, StatusEnum.FAILED, StatusEnum.CANCELLED, StatusEnum.SKIPPED}


def _ensure_runtime_diff_state(owner: Any) -> threading.RLock:
    if not hasattr(owner, "_graph_lock"):
        owner._graph_lock = threading.RLock()
    if not hasattr(owner, "_runtime_diff_queues"):
        owner._runtime_diff_queues = {}
    return owner._graph_lock


def _diff_task_id(diff: PlanDiff) -> str:
    if hasattr(diff, "task_id"):
        return str(diff.task_id)
    if hasattr(diff, "task"):
        return str(diff.task.id)
    if hasattr(diff, "from_task_id"):
        return str(diff.from_task_id)
    return "unknown"


def _sha16(state: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(state, sort_keys=True).encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Module-level lazy getters for optional/potentially-circular imports
# ---------------------------------------------------------------------------

# GoalTracker — optional drift-detection feature
_GoalTracker = None


def _get_goal_tracker_class():
    global _GoalTracker
    if _GoalTracker is None:
        from vetinari.drift.goal_tracker import GoalTracker

        _GoalTracker = GoalTracker
    return _GoalTracker


# MilestoneManager + MilestoneAction — optional milestone approval feature
_MilestoneManager = None
_MilestoneAction = None
_ForemanAgentGetter = None


def _get_milestone_manager_class():
    global _MilestoneManager
    if _MilestoneManager is None:
        from vetinari.orchestration.milestones import MilestoneManager

        _MilestoneManager = MilestoneManager
    return _MilestoneManager


def _get_milestone_action_class():
    global _MilestoneAction
    if _MilestoneAction is None:
        from vetinari.orchestration.milestones import MilestoneAction

        _MilestoneAction = MilestoneAction
    return _MilestoneAction


def _get_foreman_recursion_helpers():
    """Return Foreman recursion helpers from the live module.

    Some tests isolate imports by replacing ``sys.modules`` entries. Cached
    function objects can then point at a stale module-level parent map while
    callers register child plans through the reloaded module.
    """
    foreman_mod = importlib.import_module("vetinari.agents.consolidated.foreman")
    return foreman_mod._detect_plan_cycle, foreman_mod._register_child_plan


def _get_foreman_agent_with_judgment():
    """Return the Foreman singleton after installing recursive judgment."""
    global _ForemanAgentGetter
    if _ForemanAgentGetter is None:
        from vetinari.agents import get_foreman_agent
        from vetinari.agents.consolidated.foreman import install_foreman_judgment

        _ForemanAgentGetter = (get_foreman_agent, install_foreman_judgment)
    get_foreman_agent, install_foreman_judgment = _ForemanAgentGetter
    install_foreman_judgment()
    return get_foreman_agent()
