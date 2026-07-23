"""Agent health monitoring — heartbeat tracking and step limits.

Detects stuck/silent agents and enforces per-task step limits to prevent
runaway execution. Integrates with the circuit breaker system.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from vetinari.exceptions import AgentError

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------


class StepLimitExceeded(AgentError):
    """Raised when an agent exceeds its maximum allowed step count."""


class AgentTimeoutError(AgentError):
    """Raised when an agent has not sent a heartbeat within its timeout window."""


# ---------------------------------------------------------------------------
# Internal state per agent
# ---------------------------------------------------------------------------


@dataclass
class _AgentState:
    """Internal tracking state for a single registered agent."""

    agent_id: str
    timeout_seconds: float
    max_steps: int
    step_count: int = 0
    last_heartbeat: float = field(default_factory=time.monotonic)
    registered_at: float = field(default_factory=time.monotonic)

    def __repr__(self) -> str:
        return f"_AgentState(agent_id={self.agent_id!r}, step_count={self.step_count!r}, max_steps={self.max_steps!r})"


# ---------------------------------------------------------------------------
# AgentMonitor
# ---------------------------------------------------------------------------


class AgentMonitor:
    """Singleton monitor that tracks agent heartbeats and step counts.

    Thread-safe. Use ``get_agent_monitor()`` to obtain the shared instance.

    Example::

        monitor = get_agent_monitor()
        monitor.register_agent("builder-1", timeout_seconds=60, max_steps=20)
        monitor.heartbeat("builder-1")
        monitor.record_step("builder-1")   # raises StepLimitExceeded if over
    """

    _instance: AgentMonitor | None = None
    _class_lock = threading.Lock()

    def __new__(cls) -> AgentMonitor:
        if cls._instance is None:
            with cls._class_lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._setup()
        return cls._instance

    def _setup(self) -> None:
        self._lock = threading.Lock()
        self._agents: dict[str, _AgentState] = {}
        self._total_steps_recorded: int = 0
        self._total_timeouts_detected: int = 0
        self._total_step_limit_violations: int = 0
        # Background monitoring thread state
        self._monitor_thread: threading.Thread | None = None
        self._stop_event: threading.Event = threading.Event()

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register_agent(
        self,
        agent_id: str,
        timeout_seconds: float = 300.0,
        max_steps: int = 50,
    ) -> bool:
        """Register an agent for health monitoring.

        Args:
            agent_id: Unique identifier for the agent instance.
            timeout_seconds: Maximum seconds between heartbeats before the
                agent is considered stuck. Defaults to 300.
            max_steps: Maximum number of steps allowed before
                ``StepLimitExceeded`` is raised. Defaults to 50.

        Returns:
            True when this call created the monitor entry; False when the agent
            was already registered and the duplicate registration was ignored.

        Raises:
            ValueError: If ``timeout_seconds`` or ``max_steps`` are not
                positive.
        """
        if timeout_seconds <= 0:
            raise ValueError(f"timeout_seconds must be positive, got {timeout_seconds}")
        if max_steps <= 0:
            raise ValueError(f"max_steps must be positive, got {max_steps}")

        with self._lock:
            if agent_id in self._agents:
                # Silently overwriting would reset the step counter mid-run, hiding
                # potential runaway execution.  Callers that need an intentional reset
                # should call reset_agent() explicitly.
                logger.warning(
                    "Agent %r is already registered — ignoring duplicate registration; "
                    "call reset_agent() to intentionally reset step counter",
                    agent_id,
                )
                return False
            self._agents[agent_id] = _AgentState(
                agent_id=agent_id,
                timeout_seconds=timeout_seconds,
                max_steps=max_steps,
            )
        logger.debug(
            "Registered agent %r (timeout=%.1fs, max_steps=%d)",
            agent_id,
            timeout_seconds,
            max_steps,
        )
        return True

    # ------------------------------------------------------------------
    # Heartbeat
    # ------------------------------------------------------------------

    def heartbeat(self, agent_id: str) -> None:
        """Record a heartbeat for an agent, resetting its timeout clock.

        Args:
            agent_id: The agent to record a heartbeat for.

        Raises:
            KeyError: If the agent has not been registered.
        """
        with self._lock:
            state = self._agents.get(agent_id)
            if state is None:
                raise KeyError(f"Agent {agent_id!r} is not registered")
            state.last_heartbeat = time.monotonic()

    # ------------------------------------------------------------------
    # Step tracking
    # ------------------------------------------------------------------

    def record_step(self, agent_id: str) -> None:
        """Increment the step counter for an agent.

        Also counts the step as a heartbeat so a busy agent does not appear
        stuck.

        Args:
            agent_id: The agent recording a step.

        Raises:
            KeyError: If the agent has not been registered.
            StepLimitExceeded: If the agent has exceeded its ``max_steps``.
        """
        with self._lock:
            state = self._agents.get(agent_id)
            if state is None:
                raise KeyError(f"Agent {agent_id!r} is not registered")

            state.step_count += 1
            state.last_heartbeat = time.monotonic()
            self._total_steps_recorded += 1

            if state.step_count > state.max_steps:
                self._total_step_limit_violations += 1
                raise StepLimitExceeded(
                    f"Agent {agent_id!r} exceeded step limit of {state.max_steps} (current step: {state.step_count})",
                    agent_id=agent_id,
                    step_count=state.step_count,
                    max_steps=state.max_steps,
                )

    # ------------------------------------------------------------------
    # Health check
    # ------------------------------------------------------------------

    def check_health(self) -> list[dict[str, Any]]:
        """Return a list of unhealthy agents.

        An agent is considered unhealthy if it has not sent a heartbeat
        within its configured ``timeout_seconds``.

        Returns:
            List of dicts, each containing ``agent_id``, ``reason``,
            ``last_heartbeat_ago`` (seconds), and ``timeout_seconds``.
        """
        now = time.monotonic()
        unhealthy: list[dict[str, Any]] = []

        with self._lock:
            for agent_id, state in self._agents.items():
                elapsed = now - state.last_heartbeat
                if elapsed > state.timeout_seconds:
                    self._total_timeouts_detected += 1
                    unhealthy.append(
                        {
                            "agent_id": agent_id,
                            "reason": "no_heartbeat",
                            "last_heartbeat_ago": elapsed,
                            "timeout_seconds": state.timeout_seconds,
                        },
                    )
                    logger.warning(
                        "Agent %r appears stuck — no heartbeat for %.1fs (limit %.1fs)",
                        agent_id,
                        elapsed,
                        state.timeout_seconds,
                    )

        return unhealthy

    # ------------------------------------------------------------------
    # Deregistration
    # ------------------------------------------------------------------

    def deregister_agent(self, agent_id: str) -> None:
        """Remove an agent from health monitoring.

        A no-op if the agent is not currently registered.

        Args:
            agent_id: The agent to remove.
        """
        with self._lock:
            self._agents.pop(agent_id, None)
        logger.debug("Deregistered agent %r", agent_id)

    # ------------------------------------------------------------------
    # Background monitoring loop
    # ------------------------------------------------------------------

    def start_monitoring(self, interval_seconds: float = 10.0) -> None:
        """Start a background daemon thread that periodically checks for timed-out agents.

        The thread runs ``_monitor_loop`` at the given interval until
        ``stop_monitoring()`` is called.

        Args:
            interval_seconds: Seconds between health checks. Must be positive.

        Raises:
            ValueError: If ``interval_seconds`` is not positive.
        """
        if interval_seconds <= 0:
            raise ValueError(f"interval_seconds must be positive, got {interval_seconds}")
        with self._lock:
            if self._monitor_thread is not None and self._monitor_thread.is_alive():
                logger.debug("Monitoring already running — ignoring start_monitoring call")
                return
            self._stop_event.clear()
            self._monitor_thread = threading.Thread(
                target=self._monitor_loop,
                args=(interval_seconds,),
                daemon=True,
                name="agent-monitor-loop",
            )
            self._monitor_thread.start()
        logger.debug("Started agent monitor loop (interval=%.2fs)", interval_seconds)

    def stop_monitoring(self) -> None:
        """Signal the background monitoring loop to stop and wait for it to exit.

        Safe to call when monitoring has not been started.
        """
        self._stop_event.set()
        with self._lock:
            thread = self._monitor_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=5.0)
        logger.debug("Stopped agent monitor loop")

    def is_monitoring(self) -> bool:
        """Return True if the background monitoring loop thread is alive.

        Returns:
            True when the monitor thread exists and is running, False otherwise.
        """
        with self._lock:
            return self._monitor_thread is not None and self._monitor_thread.is_alive()

    def _monitor_loop(self, interval_seconds: float) -> None:
        """Background loop that calls check_health at the given interval.

        Uses ``threading.Event.wait`` so that ``stop_monitoring`` can interrupt
        a sleep immediately rather than waiting for the full interval.

        Args:
            interval_seconds: Seconds to wait between successive health checks.
        """
        while not self._stop_event.wait(timeout=interval_seconds):
            unhealthy = self.check_health()
            if unhealthy:
                logger.warning(
                    "Monitor loop detected %d unhealthy agent(s): %s",
                    len(unhealthy),
                    [a["agent_id"] for a in unhealthy],
                )

    # ------------------------------------------------------------------
    # Reset
    # ------------------------------------------------------------------

    def reset_agent(self, agent_id: str) -> None:
        """Reset the step counter and heartbeat timestamp for an agent.

        Args:
            agent_id: The agent to reset.

        Raises:
            KeyError: If the agent has not been registered.
        """
        with self._lock:
            state = self._agents.get(agent_id)
            if state is None:
                raise KeyError(f"Agent {agent_id!r} is not registered")
            state.step_count = 0
            state.last_heartbeat = time.monotonic()
        logger.debug("Reset agent %r", agent_id)

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------

    @property
    def active_agents(self) -> frozenset[str]:
        """Return the set of currently registered agent IDs.

        Returns:
            A frozenset of agent ID strings for all currently registered agents.
            The snapshot is taken under the monitor lock so it is consistent.
        """
        with self._lock:
            return frozenset(self._agents.keys())

    def get_stats(self) -> dict[str, Any]:
        """Return monitoring statistics.

        Returns:
            Dict with keys ``registered_agents``, ``total_steps_recorded``,
            ``total_timeouts_detected``, ``total_step_limit_violations``,
            and ``agents`` (a list of per-agent dicts).
        """
        now = time.monotonic()
        with self._lock:
            agents_info = [
                {
                    "agent_id": s.agent_id,
                    "step_count": s.step_count,
                    "max_steps": s.max_steps,
                    "timeout_seconds": s.timeout_seconds,
                    "last_heartbeat_ago": now - s.last_heartbeat,
                }
                for s in self._agents.values()
            ]
            return {
                "registered_agents": len(self._agents),
                "total_steps_recorded": self._total_steps_recorded,
                "total_timeouts_detected": self._total_timeouts_detected,
                "total_step_limit_violations": self._total_step_limit_violations,
                "agents": agents_info,
            }


# ---------------------------------------------------------------------------
# Singleton accessor + reset helper
# ---------------------------------------------------------------------------


def get_agent_monitor() -> AgentMonitor:
    """Return the shared ``AgentMonitor`` singleton."""
    return AgentMonitor()


def reset_agent_monitor() -> None:
    """Stop any background monitoring and destroy the singleton.

    Intended for use in tests. Calling this while the monitor loop is running
    will cleanly signal the loop thread to exit before clearing the instance.
    """
    with AgentMonitor._class_lock:
        instance = AgentMonitor._instance
        if instance is not None:
            instance.stop_monitoring()
        AgentMonitor._instance = None
