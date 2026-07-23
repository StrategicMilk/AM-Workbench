"""Package-level public exports for Vetinari agents.

Contract imports must stay lightweight because orchestration, planning, and
adapter modules import ``vetinari.agents.contracts`` during initialization.
Concrete agent classes are resolved lazily when callers request them.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

from vetinari.types import AgentType, ExecutionMode, StatusEnum

from .contracts import AgentResult, AgentSpec, AgentTask, ExecutionPlan, Task, VerificationResult, get_agent_spec

if TYPE_CHECKING:
    from .base_agent import BaseAgent
    from .consolidated.quality_agent import InspectorAgent, get_inspector_agent
    from .consolidated.worker_agent import WorkerAgent, get_worker_agent
    from .multi_mode_agent import MultiModeAgent
    from .planner_agent import ForemanAgent, get_foreman_agent
    from .training_agent import TrainingAgent, get_training_agent
    from .workbench_agent import WorkbenchAgent, get_workbench_agent

_LAZY: dict[str, Any] = {}
_LAZY_MAP: dict[str, str] = {
    "BaseAgent": "base_agent",
    "ForemanAgent": "planner_agent",
    "InspectorAgent": "consolidated.quality_agent",
    "MultiModeAgent": "multi_mode_agent",
    "TrainingAgent": "training_agent",
    "WorkbenchAgent": "workbench_agent",
    "WorkerAgent": "consolidated.worker_agent",
    "_self_critique": "_self_critique",
    "get_foreman_agent": "planner_agent",
    "get_inspector_agent": "consolidated.quality_agent",
    "get_training_agent": "training_agent",
    "get_workbench_agent": "workbench_agent",
    "get_worker_agent": "consolidated.worker_agent",
}

_MODULE_EXPORTS: dict[str, tuple[str, ...]] = {
    "base_agent": ("BaseAgent",),
    "planner_agent": ("ForemanAgent", "get_foreman_agent"),
    "consolidated.quality_agent": ("InspectorAgent", "get_inspector_agent"),
    "consolidated.worker_agent": ("WorkerAgent", "get_worker_agent"),
    "multi_mode_agent": ("MultiModeAgent",),
    "training_agent": ("TrainingAgent", "get_training_agent"),
    "workbench_agent": ("WorkbenchAgent", "get_workbench_agent"),
}


def _load_exports(module_name: str) -> None:
    module = importlib.import_module(f".{module_name}", __name__)
    for symbol in _MODULE_EXPORTS[module_name]:
        value = getattr(module, symbol)
        _LAZY[symbol] = value
        globals()[symbol] = value


def __getattr__(name: str) -> Any:
    """Resolve concrete agent implementations only when requested."""
    if name in _LAZY:
        return _LAZY[name]
    module_name = _LAZY_MAP.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    if module_name == "_self_critique":
        module = importlib.import_module(f".{module_name}", __name__)
        _LAZY[name] = module
        globals()[name] = module
        return module
    _load_exports(module_name)
    return _LAZY[name]


__all__ = [
    "AgentResult",
    "AgentSpec",
    "AgentTask",
    "AgentType",
    "BaseAgent",
    "ExecutionMode",
    "ExecutionPlan",
    "ForemanAgent",
    "InspectorAgent",
    "MultiModeAgent",
    "StatusEnum",
    "Task",
    "TrainingAgent",
    "VerificationResult",
    "WorkbenchAgent",
    "WorkerAgent",
    "_self_critique",
    "get_agent_spec",
    "get_foreman_agent",
    "get_inspector_agent",
    "get_training_agent",
    "get_workbench_agent",
    "get_worker_agent",
]
