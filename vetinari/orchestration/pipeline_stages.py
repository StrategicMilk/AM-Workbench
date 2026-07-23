"""Pipeline execution stages 5-8.

This is the second half of the assembly-line orchestration, covering:
  5. Parallel Execution
  5.5. Self-refinement (Custom tier)
  6. Output Review
  7. Final Assembly
  8. Goal Verification + Correction Loop
  Post-pipeline: telemetry, Thompson Sampling, SPC, ConversationStore

``PipelineStageRunner`` is composed into ``TwoLayerOrchestrator`` alongside
``PipelineExecutionEngine``. Methods are called from ``PipelineExecutionEngine._execute_pipeline``
via ``self._run_execution_stages``.

The AgentGraph execution backend (``execute_with_agent_graph``) lives in
``pipeline_agent_graph.py`` and is composed in via ``PipelineAgentGraphRunner``.
"""

from __future__ import annotations

import logging
from typing import Any

from vetinari.boundary_guards import require_nonempty
from vetinari.events import (
    get_event_bus,
)
from vetinari.structured_logging import log_event

from .pipeline_stages_execution import _PipelineStageExecutionSideEffects
from .pipeline_stages_runtime import _PipelineStageRuntime

logger = logging.getLogger(__name__)


# Controls whether the optional collaboration blackboard path is attempted.
# Set to False in tests (via patch) to skip the CollaborationBehavior import.
_COLLABORATION_AVAILABLE: bool = True


class PipelineStageRunner(_PipelineStageRuntime, _PipelineStageExecutionSideEffects):
    """Pipeline stages 5-8 and the AgentGraph execution backend.

    Mixed into TwoLayerOrchestrator. All methods access ``self`` attributes
    set by TwoLayerOrchestrator.__init__. Methods are called by
    PipelineExecutionEngine._execute_pipeline after stages 0-4 complete.
    """

    @staticmethod
    def _get_pipeline_event_bus() -> Any:
        """Return the public pipeline event bus used by stage runtime code.

        Kept on this compatibility class so existing tests and callers that
        patch ``vetinari.orchestration.pipeline_stages.get_event_bus`` continue
        to affect the execution-stage path after the method extraction.
        """
        return get_event_bus()

    @staticmethod
    def _run_review_gate(
        *,
        graph: Any,
        stages: dict[str, Any],
        review_result: dict[str, Any],
    ) -> None:
        """Evaluate the Inspector's review result and block assembly on failure.

        Uses a fail-closed default: if ``passed`` is absent, the gate blocks.
        Also blocks when ``quality_score`` is present and below 0.5.
        When the gate blocks, publishes issues to the collaboration blackboard
        if ``_COLLABORATION_AVAILABLE`` is True and the import succeeds.

        Args:
            graph: The active AgentGraph (used for plan_id in log messages).
            stages: Mutable stages dict; sets ``gate_blocked`` and ``gate_issues``
                on failure so downstream assembly can detect and surface the block.
            review_result: Raw dict returned by the Inspector agent, expected to
                contain optional keys ``passed`` (bool), ``issues`` (list), and
                ``quality_score`` (float).
        """
        inspector_issues = review_result.get("issues", [])
        # Fail-closed: absent "passed" remains a failure unless a legacy
        # review adapter supplied an explicit successful verdict and no issues.
        inspector_passed = review_result.get("passed")
        if inspector_passed is None:
            verdict = str(review_result.get("verdict", "")).strip().lower()
            inspector_passed = verdict in {"ok", "pass", "passed", "success"} and not inspector_issues
        _quality_score = review_result.get("quality_score", 1.0)
        # Block when quality score is present and explicitly below threshold
        _score_failed = isinstance(_quality_score, (int, float)) and _quality_score < 0.5
        if not inspector_passed or _score_failed:
            logger.warning(
                "[Pipeline] Inspector gate FAILED — blocking final assembly (passed=%s, score=%s, issues=%d)",
                inspector_passed,
                _quality_score,
                len(inspector_issues),
            )
            log_event(
                "warning",
                __name__,
                "pipeline_review_gate_failed",
                event_type="pipeline_review_gate_failed",
                plan_id=str(getattr(graph, "plan_id", "unknown")),
                inspector_passed=bool(inspector_passed),
                quality_score=_quality_score,
                issue_count=len(inspector_issues),
            )
            stages["gate_blocked"] = True
            stages["gate_issues"] = inspector_issues[:10]
            # Publish findings to the blackboard so other agents can query them
            if _COLLABORATION_AVAILABLE:
                try:
                    # CollaborationBehavior pulls in base-agent imports that can
                    # cycle during pipeline module initialization; keep this
                    # optional blackboard path lazy.
                    from vetinari.agents.collaboration import CollaborationBehavior

                    _collab = CollaborationBehavior()
                    _collab.publish_finding(
                        "inspector_issues",
                        inspector_issues,
                        finding_type="quality",
                    )
                except Exception:
                    logger.warning(
                        "Could not publish inspector findings to blackboard — findings not shared with other agents",
                        exc_info=True,
                    )
            if not inspector_passed:
                reason = require_nonempty("inspector_failed", field_name="inspector_gate_failure")
            else:
                reason = require_nonempty("quality_score_failed", field_name="inspector_review_gate_failure")
            raise RuntimeError(
                f"Inspector review gate blocked final assembly for plan "
                f"{getattr(graph, 'plan_id', 'unknown')}: {reason}"
            )
