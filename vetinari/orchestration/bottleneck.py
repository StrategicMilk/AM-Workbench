"""Bottleneck Identification — Theory of Constraints for Vetinari pipeline.

Identifies the constraint (bottleneck) in the agent pipeline using TOC
principles. The constraint determines the maximum throughput of the
entire system — everything else subordinates to it.

Implements Drum-Buffer-Rope:
  - Drum: dispatch rate matches constraint throughput
  - Buffer: keep tasks ready for the constraint agent
  - Rope: throttle intake to prevent upstream WIP buildup
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, replace
from typing import Any

from vetinari.utils.serialization import dataclass_to_dict

logger = logging.getLogger(__name__)


ROLLING_WINDOW = 50  # Rolling average over last N tasks


@dataclass(frozen=True, slots=True)
class BottleneckAgentMetrics:
    """Per-agent performance metrics for bottleneck identification.

    Args:
        agent_type: The agent type identifier.
        avg_execution_time_ms: Rolling average execution time in milliseconds.
        queue_depth: Number of tasks waiting to be dispatched.
        utilization: Time busy / time available (0.0-1.0).
        failure_rate: Rejections / total (0.0-1.0).
        throughput: Tasks completed per minute.
    """

    agent_type: str = ""
    avg_execution_time_ms: float = 0.0
    queue_depth: int = 0
    utilization: float = 0.0
    failure_rate: float = 0.0
    throughput: float = 0.0

    def __repr__(self) -> str:
        """Show key identifying fields for debugging."""
        return (
            f"AgentMetrics(agent_type={self.agent_type!r},"
            f" utilization={self.utilization!r},"
            f" throughput={self.throughput!r})"
        )

    def to_dict(self) -> dict[str, Any]:
        """Converts agent metrics to a JSON-serializable dictionary for API responses."""
        return dataclass_to_dict(self)


class BottleneckIdentifier:
    """Identifies the constraint in the pipeline using Theory of Constraints.

    Tracks per-agent metrics and identifies which agent is the bottleneck
    based on utilization and queue depth.
    """

    def __init__(self) -> None:
        self._metrics: dict[str, BottleneckAgentMetrics] = {}
        self._execution_times: dict[str, deque[float]] = {}
        self._completion_times: dict[str, deque[float]] = {}
        self._success_counts: dict[str, int] = {}
        self._failure_counts: dict[str, int] = {}
        self._total_busy_time: dict[str, float] = {}
        self._start_time: float = time.monotonic()
        self._lock = threading.Lock()

    def update_metrics(
        self,
        agent_type: str,
        execution_time_ms: float,
        success: bool,
    ) -> None:
        """Called after each task completion to update rolling metrics.

        Args:
            agent_type: The agent type that completed a task.
            execution_time_ms: How long the task took in milliseconds.
            success: Whether the task succeeded.
        """
        with self._lock:
            # Initialize if needed
            if agent_type not in self._execution_times:
                self._execution_times[agent_type] = deque(maxlen=ROLLING_WINDOW)
                self._completion_times[agent_type] = deque(maxlen=ROLLING_WINDOW)
                self._success_counts[agent_type] = 0
                self._failure_counts[agent_type] = 0
                self._total_busy_time[agent_type] = 0.0
                self._metrics[agent_type] = BottleneckAgentMetrics(agent_type=agent_type)

            now = time.monotonic()
            self._execution_times[agent_type].append(execution_time_ms)
            self._completion_times[agent_type].append(now)

            if success:
                self._success_counts[agent_type] += 1
            else:
                self._failure_counts[agent_type] += 1

            # Update busy time
            self._total_busy_time[agent_type] += execution_time_ms / 1000.0

            # Recompute metrics
            times = list(self._execution_times[agent_type])
            total = self._success_counts[agent_type] + self._failure_counts[agent_type]

            avg_execution_time_ms = sum(times) / len(times) if times else 0.0
            failure_rate = self._failure_counts[agent_type] / total if total > 0 else 0.0

            # Throughput: completions per minute over rolling window
            completions = list(self._completion_times[agent_type])
            if len(completions) >= 2:
                window_duration = completions[-1] - completions[0]
                if window_duration > 0:
                    throughput = (len(completions) - 1) / (window_duration / 60.0)
                else:
                    throughput = 0.0
            else:
                throughput = 0.0

            # Utilization: busy time / elapsed time
            elapsed = now - self._start_time
            if elapsed > 0:
                utilization = min(
                    1.0,
                    self._total_busy_time[agent_type] / elapsed,
                )
            else:
                utilization = 0.0
            self._metrics[agent_type] = replace(
                self._metrics[agent_type],
                avg_execution_time_ms=avg_execution_time_ms,
                failure_rate=failure_rate,
                throughput=throughput,
                utilization=utilization,
            )

    def update_queue_depth(self, agent_type: str, depth: int) -> None:
        """Update the queue depth for an agent.

        Args:
            agent_type: The agent type.
            depth: Number of tasks waiting in queue.
        """
        with self._lock:
            if agent_type not in self._metrics:
                self._metrics[agent_type] = BottleneckAgentMetrics(agent_type=agent_type)
            self._metrics[agent_type] = replace(self._metrics[agent_type], queue_depth=depth)

    # Backward compatibility alias for callers still using the old setter verb.
    set_queue_depth = update_queue_depth

    def identify_constraint(self) -> str | None:
        """Identify the bottleneck agent.

        The constraint is the agent with the highest combined score of
        utilization * (1 + queue_depth). This captures both how busy
        the agent is and how much work is waiting for it.

        Returns:
            Agent type string of the bottleneck, or None if no metrics exist.
        """
        with self._lock:
            if not self._metrics:
                return None

            scores = {agent: m.utilization * (1 + m.queue_depth) for agent, m in self._metrics.items()}
            return max(scores, key=scores.__getitem__)

    def get_drum_rate(self) -> float:
        """Return max dispatch rate (tasks/minute) based on constraint throughput.

        Returns:
            Tasks per minute the system should accept.
        """
        constraint = self.identify_constraint()
        if constraint is None:
            return float("inf")
        with self._lock:
            return self._metrics[constraint].throughput

    def get_buffer_size(self, agent_type: str) -> int:
        """How many tasks should be queued before this agent.

        The constraint always gets a buffer of 2 tasks to prevent starvation.
        Non-constraint agents don't need buffers.

        Args:
            agent_type: The agent type to check.

        Returns:
            Recommended buffer size (2 for constraint, 0 for others).
        """
        constraint = self.identify_constraint()
        if agent_type == constraint:
            return 2  # Always keep 2 tasks ready for the bottleneck
        return 0

    def get_all_metrics(self) -> dict[str, BottleneckAgentMetrics]:
        """Return all tracked agent metrics.

        Returns:
            Dictionary mapping agent types to their metrics.
        """
        with self._lock:
            return dict(self._metrics)

    def get_status(self) -> dict[str, Any]:
        """Return a summary for API responses.

        Returns:
            Dictionary with constraint, drum_rate, and per-agent metrics.
        """
        constraint = self.identify_constraint()
        return {
            "constraint": constraint,
            "drum_rate_per_minute": round(self.get_drum_rate(), 2) if constraint else None,
            "agents": {agent: m.to_dict() for agent, m in self._metrics.items()},
        }


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_bottleneck_identifier: BottleneckIdentifier | None = None
_bottleneck_lock = threading.Lock()


def get_bottleneck_identifier() -> BottleneckIdentifier:
    """Return the singleton BottleneckIdentifier instance.

    Returns:
        The shared BottleneckIdentifier instance.
    """
    global _bottleneck_identifier
    if _bottleneck_identifier is None:
        with _bottleneck_lock:
            if _bottleneck_identifier is None:
                _bottleneck_identifier = BottleneckIdentifier()
    return _bottleneck_identifier


def reset_bottleneck_identifier() -> None:
    """Reset the singleton for testing."""
    global _bottleneck_identifier
    with _bottleneck_lock:
        _bottleneck_identifier = None
