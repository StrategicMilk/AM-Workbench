"""Stage-5 execution side effects for pipeline stage orchestration.

The helpers here run immediately after task execution and before review. They
publish task events, record failure categories, update Andon scope pauses, and
run the optional AutoTuner cycle without changing the stage ordering owned by
``PipelineStageRunner``.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import vetinari.analytics.failure_taxonomy as failure_taxonomy
import vetinari.learning.auto_tuner as auto_tuner
import vetinari.structured_logging as structured_logging
import vetinari.workflow.andon as andon_workflow
from vetinari.events import (
    TaskCompleted,
    TaskStarted,
    TaskTimingRecord,
    TimingEvent,
)
from vetinari.types import StatusEnum

logger = logging.getLogger(__name__)


def _parse_iso_to_epoch(iso_str: str) -> float:
    """Parse an ISO 8601 timestamp string to a POSIX epoch float.

    Args:
        iso_str: ISO 8601 formatted timestamp.

    Returns:
        POSIX timestamp as a float.
    """
    try:
        dt = datetime.fromisoformat(iso_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except (ValueError, TypeError):
        raise ValueError(f"invalid ISO timestamp: {iso_str!r}") from None


class _PipelineStageExecutionSideEffects:
    """Provide side-effect helpers for completed execution-stage tasks."""

    @staticmethod
    def _publish_task_execution_events(
        *,
        bus: Any,
        graph: Any,
        start_time: float,
        execution_id: Any,
    ) -> None:
        """Publish EventBus task lifecycle records after execution completes.

        Args:
            bus: EventBus-like publisher from ``pipeline_stages.get_event_bus``.
            graph: Execution graph whose nodes contain task timing and status.
            start_time: Pipeline start timestamp used as a missing-start fallback.
            execution_id: Execution identifier for value-stream timing records.
        """
        for node_id, node in graph.nodes.items():
            agent_type_value = getattr(node, "agent_type", "")
            if hasattr(agent_type_value, "value"):
                agent_type_value = agent_type_value.value
            raw_start = getattr(node, "started_at", None)
            raw_end = getattr(node, "completed_at", None)
            task_start_ts: float = (
                _parse_iso_to_epoch(raw_start) if isinstance(raw_start, str) else (raw_start or start_time)
            )
            if raw_end is None:
                raise ValueError(f"completed task {node_id!r} is missing completed_at")
            task_end_ts: float = _parse_iso_to_epoch(raw_end) if isinstance(raw_end, str) else raw_end
            task_duration_ms = (task_end_ts - task_start_ts) * 1000.0
            task_success = node.status == StatusEnum.COMPLETED

            bus.publish(
                TaskStarted(
                    event_type="TaskStarted",
                    timestamp=task_start_ts,
                    task_id=node_id,
                    agent_type=str(agent_type_value),
                )
            )
            bus.publish(
                TaskCompleted(
                    event_type="TaskCompleted",
                    timestamp=task_end_ts,
                    task_id=node_id,
                    agent_type=str(agent_type_value),
                    success=task_success,
                    duration_ms=task_duration_ms,
                )
            )
            timing_event = TimingEvent.TASK_COMPLETED.value if task_success else TimingEvent.TASK_REJECTED.value
            bus.publish(
                TaskTimingRecord(
                    event_type="TaskTimingRecord",
                    timestamp=task_end_ts,
                    task_id=node_id,
                    execution_id=str(execution_id),
                    agent_type=str(agent_type_value),
                    timing_event=timing_event,
                    metadata={"duration_ms": task_duration_ms},
                )
            )

    def _record_post_execution_system_updates(self, *, graph: Any) -> None:
        """Record non-critical system updates after execution completes.

        Args:
            graph: Execution graph whose nodes contain task status and errors.
        """
        self._record_execution_failures(graph=graph)
        self._update_andon_scope_after_execution(graph=graph)
        self._run_auto_tuner_after_execution()

    @staticmethod
    def _record_execution_failures(*, graph: Any) -> None:
        """Classify failed tasks for analytics dashboards."""
        try:
            tracker = failure_taxonomy.get_failure_tracker()
            last_category: str | None = None
            for node_id, node in graph.nodes.items():
                if node.status == StatusEnum.FAILED and node.error:
                    node_agent = getattr(node, "agent_type", "")
                    if hasattr(node_agent, "value"):
                        node_agent = node_agent.value
                    error = RuntimeError(node.error)
                    record = tracker.classify_and_record(
                        error=error,
                        task_id=node_id,
                        agent_type=str(node_agent),
                        context={"stage": "pipeline_execution"},
                    )
                    last_category = record.category.value
            set_failure_category = getattr(structured_logging, "set_failure_category", None)
            if last_category is not None and set_failure_category is not None:
                set_failure_category(last_category)
        except Exception as exc:
            logger.warning(
                "Failure taxonomy recording skipped after pipeline execution - tracker unavailable: %s",
                exc,
            )

    @staticmethod
    def _update_andon_scope_after_execution(*, graph: Any) -> None:
        """Pause or acknowledge Andon scopes based on per-scope task outcomes."""
        try:
            andon = andon_workflow.get_andon_system()
            scope_failed: dict[str, list[str]] = {}
            scope_succeeded: set[str] = set()
            for node_id, node in graph.nodes.items():
                node_agent = getattr(node, "agent_type", "")
                if hasattr(node_agent, "value"):
                    node_agent = node_agent.value
                node_scope = str(node_agent)
                if node.status == StatusEnum.FAILED:
                    scope_failed.setdefault(node_scope, []).append(node_id)
                else:
                    scope_succeeded.add(node_scope)
            for failed_scope, failed_ids in scope_failed.items():
                if failed_scope not in scope_succeeded and not andon.is_scope_paused(failed_scope):
                    scope_signal = andon_workflow.AndonSignal(
                        source="pipeline_stages",
                        severity="critical",
                        message=f"All tasks in scope {failed_scope!r} failed ({len(failed_ids)} task(s))",
                        affected_tasks=failed_ids,
                        scope=failed_scope,
                    )
                    andon.pause_scope(failed_scope, scope_signal)
            for ok_scope in scope_succeeded:
                if andon.is_scope_paused(ok_scope):
                    andon.acknowledge_scope(ok_scope)
        except Exception as exc:
            logger.warning("Andon scope pause/resume skipped after pipeline execution - andon unavailable: %s", exc)

    @staticmethod
    def _run_auto_tuner_after_execution() -> None:
        """Run the optional AutoTuner cycle after execution completes."""
        try:
            tuner = auto_tuner.get_auto_tuner()
            actions = tuner.run_cycle()
            if actions:
                logger.info("[AutoTuner] Applied %d tuning actions after execution", len(actions))
        except Exception:
            logger.warning("AutoTuner cycle unavailable; skipping post-execution tuning", exc_info=True)
