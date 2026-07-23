"""Agent Pool — work-stealing pool for agent worker slots.

Manages a bounded pool of reusable agent instances.  General-purpose
workers are leased and returned; specialist agents (spawned on demand for
specific task types) are reclaimed after use to keep pool size bounded.

Work stealing: when all workers are claimed, the pool can over-provision
up to ``max_size`` by spawning specialists; idle specialists beyond the
``idle_reclaim_threshold`` are reaped.
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


DEFAULT_MAX_SIZE = 5  # Maximum live agents in the pool
DEFAULT_IDLE_RECLAIM_THRESHOLD = 2  # Idle specialists to tolerate before reclaim


@dataclass(frozen=True, slots=True)
class PooledAgent:
    """Wrapper around an agent instance tracked by the pool.

    Args:
        agent_id: Unique identifier for this pool slot.
        agent: The underlying agent instance.
        agent_type_value: String value of the agent's AgentType.
        is_specialist: True if spawned on demand for a specific task type.
        is_claimed: True if currently assigned to a task.
        idle_since: ISO timestamp when the agent was last released.
        tasks_completed: Total tasks completed by this instance.
    """

    agent_id: str
    agent: Any
    agent_type_value: str
    is_specialist: bool = False
    is_claimed: bool = False
    idle_since: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    tasks_completed: int = 0

    def __repr__(self) -> str:
        return (
            f"PooledAgent(agent_id={self.agent_id!r}, "
            f"agent_type={self.agent_type_value!r}, "
            f"is_specialist={self.is_specialist!r}, "
            f"is_claimed={self.is_claimed!r})"
        )


class AgentPool:
    """Bounded pool of agent instances with work-stealing and specialist reclaim.

    General workers (is_specialist=False) stay in the pool permanently.
    Specialist agents (is_specialist=True) are spawned on demand and
    reclaimed when ``reclaim_idle()`` is called.

    Thread-safety: all public methods are protected by a single reentrant lock.

    Example::

        pool = AgentPool(max_size=5)
        agent = pool.claim_worker("WORKER")
        try:
            result = agent.execute(task)
        finally:
            pool.release_worker(agent.agent_id)
    """

    def __init__(
        self,
        max_size: int = DEFAULT_MAX_SIZE,
        idle_reclaim_threshold: int = DEFAULT_IDLE_RECLAIM_THRESHOLD,
    ) -> None:
        """Initialise the pool with capacity limits.

        Args:
            max_size: Maximum live agents (general + specialist) allowed.
            idle_reclaim_threshold: Number of idle specialists tolerated before
                reclaim removes the oldest.
        """
        if max_size < 1:
            raise ValueError("max_size must be >= 1")

        self._max_size = max_size
        self._idle_reclaim_threshold = idle_reclaim_threshold
        # Deque is used for O(1) FIFO work stealing
        self._idle: deque[PooledAgent] = deque()
        self._all: dict[str, PooledAgent] = {}
        self._lock = threading.RLock()
        self._next_id = 0

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _next_agent_id(self) -> str:
        """Generate a monotonically increasing pool slot identifier.

        Returns:
            Unique agent_id string.
        """
        self._next_id += 1
        return f"pool_agent_{self._next_id:04d}"

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def add_agent(self, agent: Any, agent_type_value: str, is_specialist: bool = False) -> PooledAgent:
        """Register an existing agent instance with the pool.

        Args:
            agent: The agent object to register.
            agent_type_value: String identifier for the agent type.
            is_specialist: Whether this is a on-demand specialist.

        Returns:
            The PooledAgent wrapper added to the pool.

        Raises:
            RuntimeError: If the pool is already at max_size.
        """
        with self._lock:
            if len(self._all) >= self._max_size:
                raise RuntimeError(f"AgentPool at capacity ({self._max_size}); cannot add agent")
            pa = PooledAgent(
                agent_id=self._next_agent_id(),
                agent=agent,
                agent_type_value=agent_type_value,
                is_specialist=is_specialist,
            )
            self._all[pa.agent_id] = pa
            self._idle.append(pa)
            logger.debug(
                "[AgentPool] Registered %s (specialist=%s, pool_size=%d)",
                pa.agent_id,
                is_specialist,
                len(self._all),
            )
            return pa

    def claim_worker(self, agent_type_value: str) -> PooledAgent | None:
        """Claim an idle agent of the requested type using work stealing.

        Searches the idle deque from left (oldest idle) to right.  The
        first matching agent is removed from the idle queue and marked
        claimed.

        Args:
            agent_type_value: The agent type to claim (e.g. ``"WORKER"``).

        Returns:
            A claimed PooledAgent, or None if none are available.
        """
        with self._lock:
            for pa in list(self._idle):
                if pa.agent_type_value == agent_type_value and not pa.is_claimed:
                    self._idle.remove(pa)
                    claimed = replace(pa, is_claimed=True)
                    self._all[claimed.agent_id] = claimed
                    logger.debug("[AgentPool] Claimed %s for type %s", claimed.agent_id, agent_type_value)
                    return claimed
            return None

    def release_worker(self, agent_id: str) -> None:
        """Return a claimed agent to the idle pool.

        Specialists are immediately reclaimed (removed) when released to
        enforce pool size discipline.  General workers return to the idle
        deque.

        Args:
            agent_id: The agent_id of the PooledAgent to release.
        """
        with self._lock:
            pa = self._all.get(agent_id)
            if pa is None:
                logger.warning("[AgentPool] release_worker called with unknown id=%s", agent_id)
                return

            released = replace(
                pa,
                is_claimed=False,
                tasks_completed=pa.tasks_completed + 1,
                idle_since=datetime.now(timezone.utc).isoformat(),
            )

            if pa.is_specialist:
                # Specialists reclaimed immediately — not returned to idle deque
                del self._all[agent_id]
                logger.debug("[AgentPool] Specialist %s reclaimed after release", agent_id)
            else:
                self._all[agent_id] = released
                self._idle.append(released)
                logger.debug(
                    "[AgentPool] Released %s back to idle (pool_idle=%d)",
                    agent_id,
                    len(self._idle),
                )

    def spawn_specialist(self, agent: Any, agent_type_value: str) -> PooledAgent | None:
        """Spawn a specialist agent if pool capacity allows.

        A specialist is immediately claimed after spawning — the caller
        must call release_worker to trigger reclaim.

        Args:
            agent: The specialist agent instance.
            agent_type_value: The agent type value string.

        Returns:
            The claimed PooledAgent, or None if pool is at capacity.
        """
        with self._lock:
            if len(self._all) >= self._max_size:
                logger.warning(
                    "[AgentPool] Cannot spawn specialist — pool at capacity (%d/%d)",
                    len(self._all),
                    self._max_size,
                )
                return None
            pa = PooledAgent(
                agent_id=self._next_agent_id(),
                agent=agent,
                agent_type_value=agent_type_value,
                is_specialist=True,
                is_claimed=True,
            )
            self._all[pa.agent_id] = pa
            logger.debug(
                "[AgentPool] Spawned specialist %s (pool_size=%d/%d)",
                pa.agent_id,
                len(self._all),
                self._max_size,
            )
            return pa

    def reclaim_idle(self) -> int:
        """Remove idle specialists beyond the idle_reclaim_threshold.

        Reclaims oldest-idle specialists first (FIFO from deque left).

        Returns:
            Number of specialists reclaimed.
        """
        with self._lock:
            idle_specialist_count = sum(1 for pa in self._idle if pa.is_specialist)
            reclaim_target = max(0, idle_specialist_count - self._idle_reclaim_threshold)
            if reclaim_target == 0:
                return 0

            reclaimed = 0
            retained_idle: deque[PooledAgent] = deque()
            for pooled_agent in self._idle:
                if pooled_agent.is_specialist and reclaimed < reclaim_target:
                    del self._all[pooled_agent.agent_id]
                    reclaimed += 1
                    logger.debug("[AgentPool] Reclaimed idle specialist %s", pooled_agent.agent_id)
                    continue
                retained_idle.append(pooled_agent)
            self._idle = retained_idle
            if reclaimed:
                logger.info("[AgentPool] Reclaimed %d idle specialists", reclaimed)
            return reclaimed

    def pool_size(self) -> int:
        """Return the total number of registered agents (claimed + idle).

        Returns:
            Integer count of all agents in the pool.
        """
        with self._lock:
            return len(self._all)

    def idle_count(self) -> int:
        """Return the number of currently idle (unclaimed) agents.

        Returns:
            Integer count of idle agents.
        """
        with self._lock:
            return len(self._idle)

    def status(self) -> dict[str, Any]:
        """Return a JSON-safe status snapshot of the pool.

        Returns:
            Dictionary with pool_size, idle_count, max_size, agents list.
        """
        with self._lock:
            return {
                "pool_size": len(self._all),
                "idle_count": len(self._idle),
                "max_size": self._max_size,
                "agents": [
                    {
                        "agent_id": pa.agent_id,
                        "agent_type": pa.agent_type_value,
                        "is_specialist": pa.is_specialist,
                        "is_claimed": pa.is_claimed,
                        "tasks_completed": pa.tasks_completed,
                    }
                    for pa in self._all.values()
                ],
            }
