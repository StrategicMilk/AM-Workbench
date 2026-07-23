"""WIP Limits — kanban-style work-in-progress tracking per agent type.

Provides :class:`WIPConfig` for per-agent capacity limits and
:class:`WIPTracker` for enforcing those limits with a pull-based queue.

Per-pool limits extend the per-agent-type system to allow named resource
pools (e.g. GPU slots, API quota buckets) to have their own independent
WIP ceilings managed alongside agent-type limits.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from vetinari.types import AgentType

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class WIPConfig:
    """Work-in-progress limits per agent type.

    Default limits mirror a typical kanban board:

    * Bottleneck roles (FOREMAN) get 1 slot.
    * Inspection roles (INSPECTOR) get 2 slots.
    * Worker roles get 3 slots to handle the consolidated workload.
    * Generic fallback is 4 concurrent tasks.
    """

    limits: dict[str, int] = field(
        default_factory=lambda: {
            AgentType.FOREMAN.value: 1,
            AgentType.WORKER.value: 3,
            AgentType.INSPECTOR.value: 2,
            "default": 4,
        },
    )
    max_queue_depth: int = 1000

    def get_limit(self, agent_type: str) -> int:
        """Return the WIP limit for *agent_type*."""
        return self.limits.get(agent_type, self.limits.get("default", 4))


class WIPTracker:
    """Track and enforce WIP limits (kanban-style pull system).

    Usage::

        tracker = WIPTracker()
        if tracker.can_start("WORKER"):
            tracker.start_task("WORKER", "task-42")
        else:
            tracker.enqueue("WORKER", "task-42")
    """

    def __init__(self, config: WIPConfig | None = None) -> None:
        self._config = config or WIPConfig()
        if self._config.max_queue_depth < 1:
            raise ValueError(f"max_queue_depth must be >= 1, got {self._config.max_queue_depth!r}")
        self._active: dict[str, list[str]] = {}  # agent_type -> [task_ids]
        self._queued: list[dict[str, Any]] = []  # waiting for capacity
        self._state_lock = threading.Lock()

        # Per-pool state: written by set_pool_limit/start_pool_task,
        # read by get_pool_utilization/get_all_pool_utilizations.
        # Protected by _pool_lock for thread safety.
        self._pool_limits: dict[str, int] = {}  # pool_id -> WIP ceiling
        self._pool_active: dict[str, list[str]] = {}  # pool_id -> [task_ids]
        self._pool_lock = threading.Lock()

    # -- queries ------------------------------------------------------------

    def can_start(self, agent_type: str) -> bool:
        """Return ``True`` if the agent type has capacity for another task.

        Returns:
            True if successful, False otherwise.
        """
        with self._state_lock:
            limit = self._config.get_limit(agent_type)
            current = len(self._active.get(agent_type, []))
            return current < limit

    def get_active_count(self, agent_type: str) -> int:
        """Return the number of currently active tasks for *agent_type*.

        Returns:
            Active task count for the requested agent type.
        """
        with self._state_lock:
            return len(self._active.get(agent_type, []))

    def get_queue_depth(self) -> int:
        """Return the total number of queued (waiting) tasks.

        Returns:
            Number of queued tasks across all agent types.
        """
        with self._state_lock:
            return len(self._queued)

    def get_utilization(self) -> dict[str, dict[str, Any]]:
        """Return utilization info for every known agent type.

        Returns:
            Dict mapping agent type to utilization details.
        """
        with self._state_lock:
            types = set(self._config.limits.keys()) | set(self._active.keys())
            types.discard("default")
            result: dict[str, dict[str, Any]] = {}
            for agent_type in sorted(types):
                limit = self._config.get_limit(agent_type)
                active = len(self._active.get(agent_type, []))
                result[agent_type] = {
                    "active": active,
                    "limit": limit,
                    "utilization": active / limit if limit > 0 else 0.0,
                    "available": max(0, limit - active),
                }
            return result

    # -- mutations ----------------------------------------------------------

    def start_task(self, agent_type: str, task_id: str) -> bool:
        """Start a task if capacity allows.  Returns ``True`` on success.

        Args:
            agent_type: The agent type.
            task_id: The task id.

        Returns:
            True if successful, False otherwise.
        """
        with self._state_lock:
            limit = self._config.get_limit(agent_type)
            active = self._active.setdefault(agent_type, [])
            if len(active) >= limit:
                logger.warning(
                    "WIP limit reached for %s (limit=%d) -- cannot start %s",
                    agent_type,
                    limit,
                    task_id,
                )
                return False
            active += [task_id]
        logger.debug("WIP started %s for %s", task_id, agent_type)
        return True

    def complete_task(self, agent_type: str, task_id: str) -> dict[str, Any] | None:
        """Mark a task as complete and pull the next queued task (if any).

        Returns the dequeued task dict, or ``None`` if the queue is empty.

        Args:
            agent_type: The agent type.
            task_id: The task id.

        Returns:
            The dequeued task dict, or None if queue is empty.
        """
        with self._state_lock:
            tasks = self._active.get(agent_type, [])
            if task_id not in tasks:
                logger.warning("WIP completion ignored for non-active task %s/%s", agent_type, task_id)
                return None
            tasks.remove(task_id)
            logger.debug("WIP completed %s for %s", task_id, agent_type)

            # Pull from the queue (first task matching this agent type)
            for i, queued in enumerate(self._queued):
                if queued.get("agent_type") == agent_type:
                    pulled = self._queued.pop(i)
                    # Auto-start the pulled task
                    self._active.setdefault(agent_type, []).append(pulled["task_id"])
                    logger.info(
                        "WIP pulled %s from queue for %s",
                        pulled["task_id"],
                        agent_type,
                    )
                    return pulled

        return None

    def release_task(self, agent_type: str, task_id: str) -> bool:
        """Release an active task without pulling queued work.

        Use this for pre-execution deferrals, cancellations, or policy blocks
        where the task did not actually complete and queued work should remain
        queued.

        Args:
            agent_type: Agent bucket that owns the active task.
            task_id: Task identifier to release.

        Returns:
            True when the task was active and has been released.
        """
        with self._state_lock:
            tasks = self._active.get(agent_type, [])
            if task_id not in tasks:
                logger.warning("WIP release ignored for non-active task %s/%s", agent_type, task_id)
                return False
            tasks.remove(task_id)
        logger.debug("WIP released %s for %s", task_id, agent_type)
        return True

    def enqueue(self, agent_type: str, task_id: str, **extra: Any) -> bool:
        """Place a task in the waiting queue.

        Args:
            agent_type: The agent type.
            task_id: The task id.
            **extra: Additional key-value pairs to store with the entry.

        Returns:
            True when the task was queued, False when the queue rejected it.
        """
        if not agent_type or not task_id:
            logger.warning("WIP enqueue rejected invalid task %r/%r", agent_type, task_id)
            return False
        reserved_keys = {"agent_type", "task_id", "enqueued_at"}
        if reserved_keys.intersection(extra):
            logger.warning("WIP enqueue rejected reserved metadata keys for %s/%s", agent_type, task_id)
            return False
        entry: dict[str, Any] = {
            "agent_type": agent_type,
            "task_id": task_id,
            "enqueued_at": datetime.now(timezone.utc).isoformat(),
        }
        entry.update(extra)
        with self._state_lock:
            if len(self._queued) >= self._config.max_queue_depth:
                logger.warning(
                    "WIP queue at capacity (%d/%d) -- cannot enqueue %s for %s",
                    len(self._queued),
                    self._config.max_queue_depth,
                    task_id,
                    agent_type,
                )
                return False
            self._queued += [entry]
        logger.debug("WIP enqueued %s for %s", task_id, agent_type)
        return True

    # -- per-pool API -------------------------------------------------------

    def set_pool_limit(self, pool_id: str, limit: int) -> None:
        """Set or update the WIP ceiling for a named resource pool.

        Pool limits are independent of agent-type limits.  A pool might
        represent a shared hardware resource (e.g. ``"gpu-pool"``) or an
        external API quota bucket.

        Args:
            pool_id: A unique name for the resource pool.
            limit: Maximum concurrent tasks allowed in this pool.

        Raises:
            ValueError: If *limit* is negative.
        """
        if limit < 0:
            raise ValueError(f"Pool limit must be >= 0, got {limit!r} for pool {pool_id!r}")
        with self._pool_lock:
            self._pool_limits[pool_id] = limit
            self._pool_active.setdefault(pool_id, [])
        logger.debug("WIP pool %r limit set to %d", pool_id, limit)

    def start_pool_task(self, pool_id: str, task_id: str) -> bool:
        """Claim a slot in a named pool for a task.

        Returns ``False`` if the pool is at capacity or unknown.

        Args:
            pool_id: The resource pool identifier.
            task_id: The task claiming the slot.

        Returns:
            ``True`` if the slot was granted, ``False`` if the pool is full
            or has not been configured.
        """
        with self._pool_lock:
            if pool_id not in self._pool_limits:
                logger.warning(
                    "WIP pool %r is not configured -- cannot start task %s; call set_pool_limit first",
                    pool_id,
                    task_id,
                )
                return False
            limit = self._pool_limits[pool_id]
            active = self._pool_active.setdefault(pool_id, [])
            if len(active) >= limit:
                logger.warning(
                    "WIP pool %r at capacity (%d/%d) -- cannot start task %s",
                    pool_id,
                    len(active),
                    limit,
                    task_id,
                )
                return False
            active += [task_id]

        logger.debug("WIP pool %r started task %s", pool_id, task_id)
        return True

    def complete_pool_task(self, pool_id: str, task_id: str) -> bool:
        """Release a pool slot when a task finishes.

        Args:
            pool_id: The resource pool identifier.
            task_id: The task releasing its slot.

        Returns:
            ``True`` if the task was found and removed, ``False`` otherwise.
        """
        with self._pool_lock:
            active = self._pool_active.get(pool_id, [])
            if task_id not in active:
                return False
            active.remove(task_id)

        logger.debug("WIP pool %r completed task %s", pool_id, task_id)
        return True

    def get_pool_utilization(self, pool_id: str) -> dict[str, Any]:
        """Return current utilization details for a single named pool.

        Args:
            pool_id: The resource pool identifier to query.

        Returns:
            A dict with keys ``current``, ``limit``, and ``available``.
            If the pool has not been configured, all values are ``0``.
        """
        with self._pool_lock:
            limit = self._pool_limits.get(pool_id, 0)
            current = len(self._pool_active.get(pool_id, []))

        return {
            "current": current,
            "limit": limit,
            "available": max(0, limit - current),
        }

    def get_all_pool_utilizations(self) -> dict[str, dict[str, Any]]:
        """Return utilization details for every configured pool.

        Suitable for a dashboard view of all named resource pools.

        Returns:
            Dict mapping pool_id to a utilization dict with keys
            ``current``, ``limit``, and ``available``.
        """
        with self._pool_lock:
            pool_ids = list(self._pool_limits.keys())

        return {pool_id: self.get_pool_utilization(pool_id) for pool_id in sorted(pool_ids)}
