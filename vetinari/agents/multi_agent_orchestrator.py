"""Multi-Agent Orchestrator.

========================
Wraps the TwoLayerOrchestrator to provide a stable multi-agent orchestration
interface used by the web UI agent endpoints.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from vetinari.types import AgentType, StatusEnum

logger = logging.getLogger(__name__)


# Agent state constants
AGENT_STATE_IDLE = "idle"
AGENT_STATE_RUNNING = StatusEnum.RUNNING.value
AGENT_STATE_ERROR = "error"


class AgentStatus:
    """Lightweight status snapshot for a single agent."""

    def __init__(self, name: str, agent_type: str):
        self.name = name
        self.agent_type = agent_type
        self.state = AGENT_STATE_IDLE
        self.tasks_completed = 0
        self.current_task: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize this AgentStatus to a plain dictionary.

        Returns:
            Dictionary containing agent name, type, state, completed task
            count, and current task details.
        """
        return {
            "name": self.name,
            "agent_type": self.agent_type,
            "state": self.state,
            "tasks_completed": self.tasks_completed,
            "current_task": self.current_task,
        }


class MultiAgentOrchestrator:
    """Multi-agent orchestrator wrapping TwoLayerOrchestrator.

    Provides agent status tracking and task dispatch.
    """

    _instance: MultiAgentOrchestrator | None = None
    _lock = threading.Lock()

    def __init__(self):
        self.agents: dict[str, AgentStatus] = {}
        self.task_queue: list[dict[str, Any]] = []
        self._initialized = False

    @classmethod
    def get_instance(cls) -> MultiAgentOrchestrator | None:
        """Return the existing singleton instance, or None if not yet created.

        Returns:
            The current MultiAgentOrchestrator instance, or None.
        """
        return cls._instance

    @classmethod
    def get_or_create(cls) -> MultiAgentOrchestrator:
        """Return the singleton MultiAgentOrchestrator, creating it if needed.

        Returns:
            The shared MultiAgentOrchestrator instance for this process.
        """
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls()
        return cls._instance

    def initialize_agents(self) -> None:
        """Populate the agents dict with an AgentStatus entry for every known AgentType.

        Idempotent — calling again resets all statuses to their initial values.
        """
        self.agents = {
            a.value: AgentStatus(name=a.value.replace("_", " ").title(), agent_type=a.value) for a in AgentType
        }
        self._initialized = True
        logger.info("MultiAgentOrchestrator initialized with %s agents", len(self.agents))

    def get_agent_status(self) -> list[dict[str, Any]]:
        """Return status for all agents.

        Returns:
            List of status dictionaries (one per AgentType), each containing
            ``name``, ``agent_type``, ``state``, ``tasks_completed``, and
            ``current_task``.
        """
        if not self._initialized:
            self.initialize_agents()
        return [a.to_dict() for a in self.agents.values()]
