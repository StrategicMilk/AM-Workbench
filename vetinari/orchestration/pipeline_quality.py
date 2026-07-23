"""Pipeline quality controller composition point.

This module preserves the public ``PipelineQualityController`` import path
while coherent method groups live in smaller ``pipeline_quality_*`` helpers.
RCA-driven rework routing remains in ``pipeline_rework.py`` and is composed in
via ``PipelineReworkController``.
"""

from __future__ import annotations

from vetinari.ontology import QUALITY_THRESHOLD_PASS
from vetinari.types import AgentType, StatusEnum

from .pipeline_quality_contracts import _AgentLike, _AgentResultLike, _PipelineQualityOwner
from .pipeline_quality_corrections import PipelineQualityCorrectionMixin
from .pipeline_quality_review import PipelineQualityReviewMixin
from .pipeline_quality_validation import PipelineQualityValidationMixin
from .pipeline_rework import PipelineReworkController


class PipelineQualityController(
    PipelineQualityCorrectionMixin,
    PipelineQualityReviewMixin,
    PipelineQualityValidationMixin,
    PipelineReworkController,
):
    """Quality gates, output review, assembly, corrections, and rework routing.

    This compatibility composition point is mixed into ``TwoLayerOrchestrator``.
    It expects host attributes such as ``execution_engine``, ``_get_agent``,
    ``_route_model_for_task``, ``enable_correction_loop``, and
    ``correction_loop_max_rounds`` to be supplied by the orchestrator.
    """


PipelineQualityMixin = PipelineQualityController

__all__ = [
    "QUALITY_THRESHOLD_PASS",
    "AgentType",
    "PipelineQualityController",
    "PipelineQualityMixin",
    "PipelineReworkController",
    "StatusEnum",
    "_AgentLike",
    "_AgentResultLike",
    "_PipelineQualityOwner",
]
