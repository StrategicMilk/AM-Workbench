"""Package-level public exports for Vetinari orchestration.

The orchestration package is imported by deep runtime modules while adapters,
workbench exports, and runtime doctor code are still initializing. Keep this
module lightweight: public symbols are resolved lazily on first access so
``import vetinari.orchestration`` cannot recurse through the full stack.
"""

from __future__ import annotations

import importlib
import sys
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .agent_graph import AgentGraph, get_agent_graph
    from .architect_executor import ArchitectExecutorPipeline
    from .architect_executor_models import ArchitectPlan, PipelineConfig
    from .bottleneck import (
        BottleneckAgentMetrics,
        BottleneckIdentifier,
        get_bottleneck_identifier,
        reset_bottleneck_identifier,
    )
    from .checkpoint_store import Checkpoint, ExecutionEvent
    from .durable_db import CheckpointSnapshot, ExecutionEventRecord
    from .durable_execution import DurableExecutionEngine
    from .execution_graph import ExecutionGraph, ExecutionTaskNode
    from .express_path import ExpressPathExecution
    from .graph_types import (
        ConditionalEdge,
        CycleDetector,
        ExecutionDAG,
        ExecutionStrategy,
        HumanCheckpoint,
        ReplanResult,
        TaskNode,
    )
    from .graph_types import ExecutionDAG as ExecutionPlan
    from .pipeline_confidence import apply_confidence_routing
    from .plan_generator import PlanGenerator
    from .request_routing import RequestQueue, classify_goal, get_goal_routing
    from .task_context import MAX_OUTPUT_PREVIEW_CHARS, TaskContextManifest, TaskManifestContext
    from .two_layer import (
        ReworkDecision,
        TwoLayerOrchestrator,
        get_two_layer_orchestrator,
        init_two_layer_orchestrator,
    )
    from .variant_system import get_variant_manager, set_variant_level


_LAZY: dict[str, Any] = {}
_LAZY_MAP: dict[str, str] = {
    "MAX_OUTPUT_PREVIEW_CHARS": "task_context",
    "AgentGraph": "agent_graph",
    "ArchitectExecutorPipeline": "architect_executor",
    "ArchitectPlan": "architect_executor",
    "BottleneckAgentMetrics": "bottleneck",
    "BottleneckIdentifier": "bottleneck",
    "Checkpoint": "checkpoint_store",
    "CheckpointSnapshot": "durable_db",
    "ConditionalEdge": "graph_types",
    "CycleDetector": "graph_types",
    "DurableExecutionEngine": "durable_execution",
    "ExecutionDAG": "graph_types",
    "ExecutionEvent": "checkpoint_store",
    "ExecutionEventRecord": "durable_db",
    "ExecutionGraph": "execution_graph",
    "ExecutionPlan": "graph_types",
    "ExecutionStrategy": "graph_types",
    "ExecutionTaskNode": "execution_graph",
    "ExpressPathExecution": "express_path",
    "HumanCheckpoint": "graph_types",
    "PipelineConfig": "architect_executor",
    "PlanGenerator": "plan_generator",
    "ReplanResult": "graph_types",
    "RequestQueue": "request_routing",
    "ReworkDecision": "two_layer",
    "TaskContextManifest": "task_context",
    "TaskManifestContext": "task_context",
    "TaskNode": "graph_types",
    "TwoLayerOrchestrator": "two_layer",
    "apply_confidence_routing": "pipeline_confidence",
    "clarification": "clarification",
    "classify_goal": "request_routing",
    "get_agent_graph": "agent_graph",
    "get_bottleneck_identifier": "bottleneck",
    "get_goal_routing": "request_routing",
    "get_two_layer_orchestrator": "two_layer",
    "get_variant_manager": "variant_system",
    "init_two_layer_orchestrator": "two_layer",
    "reset_bottleneck_identifier": "bottleneck",
    "set_variant_level": "variant_system",
}

_MODULE_EXPORTS: dict[str, tuple[str, ...]] = {
    "agent_graph": ("AgentGraph", "get_agent_graph"),
    "architect_executor": ("ArchitectExecutorPipeline", "ArchitectPlan", "PipelineConfig"),
    "bottleneck": (
        "BottleneckAgentMetrics",
        "BottleneckIdentifier",
        "get_bottleneck_identifier",
        "reset_bottleneck_identifier",
    ),
    "checkpoint_store": ("Checkpoint", "ExecutionEvent"),
    "durable_db": ("CheckpointSnapshot", "ExecutionEventRecord"),
    "durable_execution": ("DurableExecutionEngine",),
    "execution_graph": ("ExecutionGraph", "ExecutionTaskNode"),
    "express_path": ("ExpressPathExecution",),
    "graph_types": (
        "ConditionalEdge",
        "CycleDetector",
        "ExecutionDAG",
        "ExecutionStrategy",
        "HumanCheckpoint",
        "ReplanResult",
        "TaskNode",
    ),
    "pipeline_confidence": ("apply_confidence_routing",),
    "plan_generator": ("PlanGenerator",),
    "request_routing": ("RequestQueue", "classify_goal", "get_goal_routing"),
    "task_context": ("MAX_OUTPUT_PREVIEW_CHARS", "TaskContextManifest", "TaskManifestContext"),
    "two_layer": (
        "ReworkDecision",
        "TwoLayerOrchestrator",
        "get_two_layer_orchestrator",
        "init_two_layer_orchestrator",
    ),
    "variant_system": ("get_variant_manager", "set_variant_level"),
}


def _load_exports(module_name: str) -> None:
    module = importlib.import_module(f".{module_name}", __name__)
    for symbol in _MODULE_EXPORTS[module_name]:
        _LAZY[symbol] = getattr(module, symbol)
        globals()[symbol] = _LAZY[symbol]
    if module_name == "graph_types":
        _LAZY["ExecutionPlan"] = module.ExecutionDAG
        globals()["ExecutionPlan"] = _LAZY["ExecutionPlan"]


def __getattr__(name: str) -> Any:
    """Resolve package-level exports without eager package initialization."""
    if name in _LAZY:
        return _LAZY[name]
    module_name = _LAZY_MAP.get(name)
    if module_name == "clarification":
        module = sys.modules.get(f"{__name__}.clarification")
        if module is None:
            module = importlib.import_module(".clarification", __name__)
        globals()["clarification"] = module
        _LAZY["clarification"] = module
        return module
    if module_name in _MODULE_EXPORTS:
        _load_exports(module_name)
        return _LAZY[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "MAX_OUTPUT_PREVIEW_CHARS",
    "AgentGraph",
    "ArchitectExecutorPipeline",
    "ArchitectPlan",
    "BottleneckAgentMetrics",
    "BottleneckIdentifier",
    "Checkpoint",
    "CheckpointSnapshot",
    "ConditionalEdge",
    "CycleDetector",
    "DurableExecutionEngine",
    "ExecutionDAG",
    "ExecutionEvent",
    "ExecutionEventRecord",
    "ExecutionGraph",
    "ExecutionPlan",
    "ExecutionStrategy",
    "ExecutionTaskNode",
    "ExpressPathExecution",
    "HumanCheckpoint",
    "PipelineConfig",
    "PlanGenerator",
    "ReplanResult",
    "RequestQueue",
    "ReworkDecision",
    "TaskContextManifest",
    "TaskManifestContext",
    "TaskNode",
    "TwoLayerOrchestrator",
    "apply_confidence_routing",
    "clarification",
    "classify_goal",
    "get_agent_graph",
    "get_bottleneck_identifier",
    "get_goal_routing",
    "get_two_layer_orchestrator",
    "get_variant_manager",
    "init_two_layer_orchestrator",
    "reset_bottleneck_identifier",
    "set_variant_level",
]
