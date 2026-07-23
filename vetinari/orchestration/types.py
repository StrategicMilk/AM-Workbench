"""Re-export shim for shared orchestration data types.

Provides a single import location for the core orchestration dataclasses
and enums used across execution_graph, durable_execution, plan_generator,
and two_layer modules.  New code MAY import directly from the defining
module; this shim exists so that callers with broad needs can do::

    from vetinari.orchestration.types import TaskNode, Checkpoint, ExecutionEvent
"""

from __future__ import annotations

# Durable-execution layer — import from durable_db (the defining module) to
# avoid a circular import: durable_execution imports orchestration/__init__,
# which imports this module, which tried to import from durable_execution.
from vetinari.orchestration.checkpoint_store import Checkpoint, ExecutionEvent
from vetinari.orchestration.durable_db import CheckpointSnapshot, ExecutionEventRecord

# Execution-graph layer
from vetinari.orchestration.execution_graph import ExecutionGraph, ExecutionTaskNode

# Canonical enum re-exports from vetinari.types
from vetinari.types import PlanStatus, StatusEnum

__all__ = [
    "Checkpoint",
    "CheckpointSnapshot",
    "ExecutionEvent",
    "ExecutionEventRecord",
    "ExecutionGraph",
    "ExecutionTaskNode",
    "PlanStatus",
    "StatusEnum",
]
