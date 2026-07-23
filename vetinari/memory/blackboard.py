"""Vetinari Blackboard inter-agent communication and delegation.

The public imports for this module are preserved here while Blackboard behavior
is split into focused helper mixins.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from vetinari.constants import CACHE_TTL_ONE_HOUR
from vetinari.memory.blackboard_operations import BlackboardOperationsMixin
from vetinari.memory.blackboard_persistence import BlackboardPersistenceMixin
from vetinari.security.redaction import redact_text, redact_value
from vetinari.types import AgentType

logger = logging.getLogger(__name__)


BOUNDARY_ADR = "ADR-0132"
CANONICAL_BOUNDARY = "memory.agent_coordination"


class EntryState(Enum):
    """Entry state."""

    PENDING = "pending"
    CLAIMED = "claimed"
    COMPLETED = "completed"
    FAILED = "failed"
    EXPIRED = "expired"


@dataclass
class BlackboardEntry:
    """A single work item or message on the blackboard."""

    entry_id: str
    content: str
    request_type: str
    requested_by: str
    priority: int = 5
    state: EntryState = EntryState.PENDING
    claimed_by: str | None = None
    result: Any = None
    error: str | None = None
    created_at: float = field(default_factory=time.time)
    claimed_at: float | None = None
    completed_at: float | None = None
    ttl_seconds: float = CACHE_TTL_ONE_HOUR
    metadata: dict[str, Any] = field(default_factory=dict)
    scope: str = "global"
    _completion_event: threading.Event = field(
        default_factory=threading.Event,
        repr=False,
        compare=False,
    )

    def __repr__(self) -> str:
        """Show key identifying fields for debugging."""
        return (
            f"BlackboardEntry(entry_id={self.entry_id!r},"
            f" request_type={self.request_type!r},"
            f" state={self.state!r}, priority={self.priority!r})"
        )

    @property
    def is_expired(self) -> bool:
        """Whether this entry has exceeded its TTL without being completed or failed."""
        if self.state in (EntryState.COMPLETED, EntryState.FAILED):
            return False
        return (time.time() - self.created_at) > self.ttl_seconds

    def to_dict(self) -> dict[str, Any]:
        """Serialize this BlackboardEntry to a plain dictionary.

        Returns:
            A dict containing all fields needed for faithful restore via
            ``Blackboard.restore()``. String results are truncated to 500 chars;
            rich result payloads are serialized as JSON for fidelity.
        """
        result_serialized = None
        if self.result is not None:
            if isinstance(self.result, str):
                result_serialized = redact_text(self.result)[:500]
            else:
                try:
                    result_serialized = redact_value(self.result)
                except (TypeError, ValueError):
                    result_serialized = redact_text(str(self.result))[:500]

        return {
            "entry_id": self.entry_id,
            "content": redact_text(self.content),
            "request_type": self.request_type,
            "requested_by": self.requested_by,
            "priority": self.priority,
            "state": self.state.value,
            "claimed_by": self.claimed_by,
            "claimed_at": self.claimed_at,
            "result": redact_value(result_serialized),
            "error": redact_text(self.error) if self.error else self.error,
            "created_at": self.created_at,
            "completed_at": self.completed_at,
            "ttl_seconds": self.ttl_seconds,
            "metadata": redact_value(self.metadata),
            "scope": self.scope,
        }


class Blackboard(BlackboardOperationsMixin, BlackboardPersistenceMixin):
    """Thread-safe inter-agent message board."""

    _instance: Blackboard | None = None
    _cls_lock = threading.Lock()

    def __init__(
        self,
        project_id: str = "global",
        auto_persist: bool = True,
        auto_restore: bool = True,
    ) -> None:
        self._entries: dict[str, BlackboardEntry] = {}
        self._lock = threading.RLock()
        self._observers: list[Callable[[BlackboardEntry], None]] = []
        self._project_id = project_id
        self._auto_persist = auto_persist
        if auto_restore:
            self.restore(project_id)

    @classmethod
    def get_instance(cls) -> Blackboard:
        """Return the singleton Blackboard, creating it on first call.

        Returns:
            The shared Blackboard instance for this process.
        """
        with cls._cls_lock:
            if cls._instance is None:
                cls._instance = cls()
        return cls._instance


REQUEST_TYPE_ROUTING: dict[str, list[str]] = {
    "code_search": [AgentType.WORKER.value],
    "code_review": [AgentType.INSPECTOR.value],
    "security_audit": [AgentType.INSPECTOR.value],
    "architecture_decision": [AgentType.WORKER.value],
    "documentation": [AgentType.WORKER.value],
    "implementation": [AgentType.WORKER.value],
    "test_generation": [AgentType.INSPECTOR.value],
    "cost_analysis": [AgentType.WORKER.value],
    "research": [AgentType.WORKER.value],
    "ui_design": [AgentType.WORKER.value],
    "devops": [AgentType.WORKER.value],
    "error_recovery": [AgentType.WORKER.value],
    "image_generation": [AgentType.WORKER.value],
    "data_engineering": [AgentType.WORKER.value],
    "creative_writing": [AgentType.WORKER.value],
}


def get_capable_agents(request_type: str) -> list[str]:
    """Return agent type strings capable of handling a given request type."""
    return REQUEST_TYPE_ROUTING.get(request_type, [])


class SharedExecutionContext:
    """Key-value store accessible to all agents during a single plan execution.

    Lifetime: created at plan start, cleaned up at plan completion.

    Use case: RESEARCHER stores ``codebase_map`` mid-execution and BUILDER
    reads it without requiring an explicit DAG edge between them.

    Thread-safe via RLock.
    """

    def __init__(self, plan_id: str) -> None:
        self.plan_id = plan_id
        self._store: dict[str, Any] = {}
        self._provenance: dict[str, str] = {}
        self._lock = threading.RLock()

    def set(self, key: str, value: Any, agent_type: str) -> None:
        """Store a value, recording which agent wrote it.

        Args:
            key: The key.
            value: The value.
            agent_type: The agent type.
        """
        with self._lock:
            self._store[key] = value
            self._provenance[key] = agent_type
        logger.debug("[SharedCtx:%s] %s set '%s'", self.plan_id, agent_type, key)

    def get(self, key: str, default: Any = None) -> Any:
        """Read a value and return *default* if missing.

        Args:
            key: The key.
            default: The default.

        Returns:
            The Any result.
        """
        with self._lock:
            return self._store.get(key, default)

    def get_all(self) -> dict[str, Any]:
        """Return a shallow copy of all stored key-value pairs.

        Returns:
            Snapshot of the context store, safe to iterate without holding the
            lock. Mutating the returned dict does not affect the store.
        """
        with self._lock:
            return dict(self._store)

    def get_all_by_agent(self, agent_type: str) -> dict[str, Any]:
        """Return all entries written by a specific agent type.

        Returns:
            Mapping of keys to their stored values, filtered to only those
            originally written by ``agent_type``.
        """
        with self._lock:
            return {key: self._store[key] for key, agent in self._provenance.items() if agent == agent_type}

    def keys(self) -> list[str]:
        """Return list of stored keys.

        Returns:
            Snapshot of all keys currently in the context store, safe to
            iterate without holding the lock.
        """
        with self._lock:
            return list(self._store.keys())

    def clear(self) -> None:
        """Remove all entries."""
        with self._lock:
            self._store.clear()
            self._provenance.clear()


_blackboard: Blackboard | None = None
_board_lock = threading.Lock()


def get_blackboard() -> Blackboard:
    """Return the global Blackboard singleton.

    Returns:
        The process-wide Blackboard instance, shared across all agents.
    """
    global _blackboard
    if _blackboard is None:
        with _board_lock:
            if _blackboard is None:
                _blackboard = Blackboard.get_instance()
    return _blackboard
