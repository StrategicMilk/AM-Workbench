"""Express-path execution mixin for TwoLayerOrchestrator.

The express path bypasses planning entirely and routes simple goals directly
to a Builder task, then closes spans and correlation contexts.  This is the
Tier.EXPRESS fast lane from the intake classifier (Dept 4.1).

The ``ExpressPathExecution`` class is designed to be mixed into
``TwoLayerOrchestrator`` only.  It accesses instance attributes such as
``self._make_default_handler()`` and ``self._express_metrics`` that are
provided by that class.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any, Protocol, cast

from vetinari.types import StatusEnum

logger = logging.getLogger(__name__)


class _ExpressOwner(Protocol):
    """Host contract required by ExpressPathExecution."""

    def _make_default_handler(self) -> Callable[[Any], Any]:
        """Return the orchestrator's default task handler."""

    def _review_outputs(
        self,
        exec_results: dict[str, Any],
        goal: str,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Run the Inspector review stage for Express output."""


class ExpressPathExecution:
    """Express-path execution for simple goals (mixin for TwoLayerOrchestrator).

    Provides ``_execute_express`` and ``_record_express_metrics``.  Both
    methods rely on ``self._make_default_handler()`` which is defined on
    ``TwoLayerOrchestrator``.  Python resolves the attribute at call time so
    no forward reference is needed.
    """

    @staticmethod
    def _run_express_handler(goal: str, start_time: float, handler: Callable) -> Any:
        from vetinari.orchestration.execution_graph import ExecutionTaskNode

        express_task = ExecutionTaskNode(id=f"express-{int(start_time)}", description=goal, task_type="implementation")
        return handler(express_task)

    @staticmethod
    def _express_handler_success(result: Any) -> bool:
        if isinstance(result, dict) and "success" in result:
            return bool(result["success"])
        return bool(result)

    def _successful_express_result(
        self,
        goal: str,
        context: dict[str, Any],
        stages: dict[str, Any],
        start_time: float,
        result: Any,
    ) -> dict[str, Any]:
        owner = cast(_ExpressOwner, self)
        review = owner._review_outputs({"task_results": {"express_task": result}}, goal, context)
        express_success = self._express_handler_success(result)
        inspector_passed = bool(review.get("passed", False))
        pipeline_success = express_success and inspector_passed
        stages["express_execution"] = {"success": express_success}
        stages["express_inspection"] = {
            "success": inspector_passed,
            "verdict": review.get("verdict", "inconclusive"),
            "quality_score": review.get("quality_score"),
            "summary": review.get("summary", ""),
        }
        self._record_express_metrics(pipeline_success, start_time)
        return {
            "plan_id": f"express-{int(start_time)}",
            "goal": goal,
            "backend": "express",
            "tier": "express",
            StatusEnum.COMPLETED.value: 1 if pipeline_success else 0,
            StatusEnum.FAILED.value: 0 if pipeline_success else 1,
            "outputs": {"express_task": result},
            "final_output": result,
            "review": review,
            "stages": stages,
            "total_time_ms": int((time.time() - start_time) * 1000),
        }

    def _failed_express_result(
        self,
        goal: str,
        stages: dict[str, Any],
        start_time: float,
        error: Exception,
    ) -> dict[str, Any]:
        logger.warning("[Pipeline] Express execution failed: %s", error)
        stages["express_execution"] = {"success": False, "error": str(error)}
        self._record_express_metrics(False, start_time)
        return {
            "plan_id": f"express-{int(start_time)}",
            "goal": goal,
            "backend": "express",
            "tier": "express",
            StatusEnum.COMPLETED.value: 0,
            StatusEnum.FAILED.value: 1,
            "error": str(error),
            "stages": stages,
            "total_time_ms": int((time.time() - start_time) * 1000),
        }

    @staticmethod
    def _close_express_context(corr_ctx: Any | None, pipeline_span: Any | None) -> None:
        if pipeline_span is not None:
            try:
                from vetinari.observability.otel_genai import get_genai_tracer

                get_genai_tracer().end_agent_span(pipeline_span, status="ok")
            except (ImportError, AttributeError):
                logger.warning("Failed to close GenAI span", exc_info=True)
        if corr_ctx is not None:
            import contextlib as _cl

            with _cl.suppress(Exception):
                corr_ctx.__exit__(None, None, None)

    def _execute_express(
        self,
        goal: str,
        context: dict[str, Any],
        stages: dict[str, Any],
        start_time: float,
        corr_ctx: Any | None,
        pipeline_span: Any | None,
        *,
        task_handler: Callable | None = None,
    ) -> dict[str, Any]:
        """Execute Express tier: Builder -> Quality, skip planning.

        Creates a single synthetic ``TaskNode``, runs it through the provided
        (or default) handler, records metrics, and returns a standard pipeline
        result dict.  Span and correlation context are always closed in the
        ``finally`` block.

        Args:
            goal: The enriched goal string.
            context: The pipeline context.
            stages: The stages dict for recording progress.
            start_time: Pipeline start timestamp.
            corr_ctx: Optional CorrelationContext.
            pipeline_span: Optional OTel span.
            task_handler: Optional user-provided task handler.

        Returns:
            Pipeline result dict with keys ``plan_id``, ``goal``, ``backend``,
            ``tier``, ``completed``, ``failed``, ``outputs``, ``final_output``,
            ``stages``, and ``total_time_ms``.
        """
        owner = cast(_ExpressOwner, self)
        handler = task_handler or owner._make_default_handler()
        try:
            result = self._run_express_handler(goal, start_time, handler)
            return self._successful_express_result(goal, context, stages, start_time, result)
        except Exception as e:  # Broad: task handler is user-supplied; any failure mode is possible
            logger.warning("Exception handled by  execute express fallback", exc_info=True)
            return self._failed_express_result(goal, stages, start_time, e)
        finally:
            self._close_express_context(corr_ctx, pipeline_span)

    def _record_express_metrics(self, success: bool, start_time: float) -> None:
        """Record express lane metrics for success rate tracking.

        Updates an internal ``_express_metrics`` dict (created on first call)
        with total, success, and failed counts, then logs the current rates.

        Args:
            success: Whether the express execution succeeded.
            start_time: Pipeline start timestamp for latency calculation.
        """
        try:
            latency_ms = int((time.time() - start_time) * 1000)
            # Wire WO-13: use "failed" not "promoted" — the counter tracks
            # express-lane failures, not tier promotions.  These are distinct
            # events: a promotion sends the request to the full pipeline, whereas
            # this counter increments when the express handler raises an exception.
            if not hasattr(self, "_express_metrics"):
                self._express_metrics: dict[str, int] = {"total": 0, "success": 0, StatusEnum.FAILED.value: 0}
            self._express_metrics["total"] += 1
            if success:
                self._express_metrics["success"] += 1
            else:
                self._express_metrics[StatusEnum.FAILED.value] += 1
            rate = self._express_metrics["success"] / max(self._express_metrics["total"], 1)
            logger.info(
                "[Express Metrics] total=%d, success=%d, failed=%d, success_rate=%.2f, latency_ms=%d",
                self._express_metrics["total"],
                self._express_metrics["success"],
                self._express_metrics[StatusEnum.FAILED.value],
                rate,
                latency_ms,
            )
        except (ArithmeticError, AttributeError):
            logger.warning("Express metrics recording failed", exc_info=True)
