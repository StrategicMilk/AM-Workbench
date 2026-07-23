"""Pipeline helper composition point.

This module preserves the public ``PipelineSupportServices`` import path while
the implementation lives in focused helper modules. The class is mixed into
``TwoLayerOrchestrator`` and accesses attributes set by
``TwoLayerOrchestrator.__init__``.
"""

from __future__ import annotations

from typing import Any

from vetinari.orchestration.pipeline_helpers_contracts import (
    ModelRouterLike,
    PipelineExecutionEngineLike,
    PipelineVariantManagerLike,
)
from vetinari.orchestration.pipeline_helpers_core import PipelineCoreServicesMixin
from vetinari.orchestration.pipeline_helpers_goal import PipelineGoalServicesMixin
from vetinari.orchestration.pipeline_helpers_task_handler import PipelineDefaultHandlerMixin
from vetinari.types import AgentType

__all__ = [
    "PipelineHelpersMixin",
    "PipelineSupportServices",
]


class PipelineSupportServices(
    PipelineCoreServicesMixin,
    PipelineGoalServicesMixin,
    PipelineDefaultHandlerMixin,
):
    """Compatibility class composing pipeline support helper mixins.

    The composed methods provide agent access, model routing, goal enrichment,
    memory retrieval, variant config, and the default inference-backed task
    handler. All methods access ``self`` attributes defined by
    ``TwoLayerOrchestrator.__init__``: ``agent_context``, ``_agents``,
    ``model_router``, ``execution_engine``, and ``_variant_manager``.
    """

    # v0.5.0: 3 factory-pipeline agents + string aliases redirected
    _AGENT_MODULE_MAP = {
        AgentType.FOREMAN.value: ("vetinari.agents", "get_foreman_agent"),
        AgentType.WORKER.value: ("vetinari.agents", "get_worker_agent"),
        AgentType.INSPECTOR.value: ("vetinari.agents", "get_inspector_agent"),
    }
    _variant_manager: PipelineVariantManagerLike
    execution_engine: PipelineExecutionEngineLike
    model_router: ModelRouterLike | None
    agent_context: dict[str, Any]
    _agents: dict[str, Any]


PipelineHelpersMixin = PipelineSupportServices
