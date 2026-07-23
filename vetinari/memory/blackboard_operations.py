"""Operational mixin for the public blackboard facade."""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from vetinari.boundary_guards import account_evidence_drop, assert_dependency_success
from vetinari.constants import CACHE_TTL_ONE_HOUR
from vetinari.exceptions import StorageError
from vetinari.guards import require_subsystem
from vetinari.types import AgentType

if TYPE_CHECKING:
    from vetinari.memory.blackboard import BlackboardEntry

logger = logging.getLogger("vetinari.memory.blackboard")


class BlackboardOperationsMixin:
    """In-memory blackboard posting, claiming, delegation, and maintenance."""

    if TYPE_CHECKING:
        _entries: Any
        _lock: Any
        _observers: Any
        _persist_if_enabled: Any

    def _persist_blackboard_or_raise(self, action: str) -> None:
        try:
            result = self._persist_if_enabled()
        except StorageError:
            account_evidence_drop(
                logger=logger,
                evidence_ref=action,
                reason="blackboard_persist_failure",
            )
            raise
        if result is False:
            account_evidence_drop(
                logger=logger,
                evidence_ref=action,
                reason="blackboard_persist_failure",
            )
        assert_dependency_success(
            True if result is None else bool(result),
            dependency_id=f"blackboard.{action}.persist",
        )

    def post(
        self,
        content: str,
        request_type: str,
        requested_by: str,
        priority: int = 5,
        ttl_seconds: float = float(CACHE_TTL_ONE_HOUR),
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Post a new work item. Returns the entry_id.

        Args:
            content: The content.
            request_type: The request type.
            requested_by: The requested by.
            priority: The priority.
            ttl_seconds: The ttl seconds.
            metadata: The metadata.

        Returns:
            The generated entry_id (e.g. ``"bb_a3f7c2d1"``) that callers
            use to claim, complete, or wait on this work item.
        """
        from vetinari.memory.blackboard import BlackboardEntry

        entry_id = f"bb_{uuid.uuid4().hex[:8]}"
        entry = BlackboardEntry(
            entry_id=entry_id,
            content=content,
            request_type=request_type,
            requested_by=requested_by,
            priority=priority,
            ttl_seconds=ttl_seconds,
            metadata=metadata or {},
        )
        with self._lock:
            self._entries[entry_id] = entry
        logger.debug("[Blackboard] Posted %s (%s) by %s", entry_id, request_type, requested_by)
        self._persist_blackboard_or_raise("post")
        self._notify_observers(entry)
        return entry_id

    def claim(self, entry_id: str, agent_type: str) -> BlackboardEntry | None:
        """Claim a pending entry for processing. Returns None if unavailable.

        Phase 7.9H: Checks MODEL_INFERENCE permission before allowing claim.

        Args:
            entry_id: The entry id.
            agent_type: The agent type.

        Returns:
            The BlackboardEntry | None result.
        """
        from vetinari.memory.blackboard import EntryState

        # Permission check: agents can only claim work if permitted.
        with require_subsystem("blackboard_auth", "context"):
            from vetinari.execution_context import ToolPermission, get_context_manager

            ctx_mgr = get_context_manager()
            if not ctx_mgr.check_permission(ToolPermission.MODEL_INFERENCE):
                logger.warning(
                    "[Blackboard] Claim denied for %s on %s: MODEL_INFERENCE not allowed in current mode",
                    agent_type,
                    entry_id,
                )
                return None

        with self._lock:
            entry = self._entries.get(entry_id)
            if entry is None or entry.state != EntryState.PENDING or entry.is_expired:
                return None
            entry.state = EntryState.CLAIMED
            entry.claimed_by = agent_type
            entry.claimed_at = time.time()
        self._persist_blackboard_or_raise("claim")
        return entry

    def complete(self, entry_id: str, result: Any) -> bool:
        """Mark an entry as completed with a result.

        Args:
            entry_id: The entry id.
            result: The result.

        Returns:
            True if successful, False otherwise.
        """
        from vetinari.memory.blackboard import EntryState

        with self._lock:
            entry = self._entries.get(entry_id)
            if entry is None:
                return False
            entry.state = EntryState.COMPLETED
            entry.result = result
            entry.completed_at = time.time()
            entry._completion_event.set()
        logger.debug("[Blackboard] Completed %s", entry_id)
        self._persist_blackboard_or_raise("complete")
        return True

    def fail(self, entry_id: str, error: str) -> bool:
        """Mark an entry as failed.

        Args:
            entry_id: The entry id.
            error: The error.

        Returns:
            True if successful, False otherwise.
        """
        from vetinari.memory.blackboard import EntryState

        with self._lock:
            entry = self._entries.get(entry_id)
            if entry is None:
                return False
            entry.state = EntryState.FAILED
            entry.error = error
            entry.completed_at = time.time()
            entry._completion_event.set()
        logger.debug("[Blackboard] Failed %s: %s", entry_id, error)
        self._persist_blackboard_or_raise("fail")
        return True

    def get_result(self, entry_id: str, timeout: float = 30.0) -> Any:
        """Wait for a result using threading.Event (no polling).

        Args:
            entry_id: The entry id.
            timeout: The timeout.

        Returns:
            The Any result.

        Raises:
            RuntimeError: If the operation fails.
        """
        from vetinari.memory.blackboard import EntryState

        with self._lock:
            entry = self._entries.get(entry_id)
        if entry is None:
            return None
        if entry.state == EntryState.COMPLETED:
            return entry.result
        if entry.state == EntryState.FAILED:
            raise RuntimeError(f"Blackboard entry {entry_id} failed: {entry.error}")
        entry._completion_event.wait(timeout=timeout)
        if entry.state == EntryState.COMPLETED:
            return entry.result
        if entry.state == EntryState.FAILED:
            raise RuntimeError(f"Blackboard entry {entry_id} failed: {entry.error}")
        return None

    def get_pending(
        self,
        request_type: str | None = None,
        limit: int = 10,
    ) -> list[BlackboardEntry]:
        """Return pending entries, optionally filtered by type, sorted by priority.

        Args:
            request_type: The request type.
            limit: The limit.

        Returns:
            List of results.
        """
        from vetinari.memory.blackboard import EntryState

        with self._lock:
            entries = [
                e
                for e in self._entries.values()
                if e.state == EntryState.PENDING
                and not e.is_expired
                and (request_type is None or e.request_type == request_type)
            ]
        entries.sort(key=lambda e: (e.priority, e.created_at))
        return entries[:limit]

    def get_entry(self, entry_id: str) -> BlackboardEntry | None:
        """Get entry.

        Returns:
            The BlackboardEntry | None result.
        """
        with self._lock:
            return self._entries.get(entry_id)

    def delegate(
        self,
        task: Any,
        available_agents: dict[Any, Any],
    ) -> Any | None:
        """Try to find an agent that can handle ``task.assigned_agent`` type.

        Falls back to FOREMAN for unknown types, then returns a failure result
        if no fallback exists.

        Args:
            task: The task.
            available_agents: The available agents.

        Returns:
            The Any | None result.
        """
        from vetinari.agents.contracts import AgentTask

        fallback_order = [
            AgentType.FOREMAN,
            AgentType.WORKER,
        ]
        for fallback_type in fallback_order:
            if fallback_type in available_agents:
                agent = available_agents[fallback_type]
                logger.warning(
                    "[Blackboard] Delegating unhandled task %s (type=%s) to fallback %s",
                    task.id,
                    task.assigned_agent,
                    fallback_type.value,
                )
                try:
                    agent_task = AgentTask.from_task(task, task.description)
                    return agent.execute(agent_task)
                except Exception as exc:
                    logger.error("[Blackboard] Fallback delegation failed: %s", exc)

        return None

    def request_help(
        self,
        requesting_agent: str,
        request_type: str,
        description: str,
        priority: int = 5,
        timeout: float = 30.0,
        metadata: dict[str, Any] | None = None,
    ) -> Any:
        """Post a help request and wait for a capable agent to fulfil it.

        This is a synchronous convenience method: it posts, then blocks
        until a result is available or timeout expires.

        Args:
            requesting_agent: AgentType.value of the requester.
            request_type: Category of work (must match REQUEST_TYPE_ROUTING).
            description: Human-readable task description.
            priority: 1=highest, 10=lowest.
            timeout: Seconds to wait for result.
            metadata: Optional extra context for the handler.

        Returns:
            The result from the handling agent, or None on timeout.
        """
        entry_id = self.post(
            content=description,
            request_type=request_type,
            requested_by=requesting_agent,
            priority=priority,
            metadata=metadata or {},
        )
        logger.info("[Blackboard] %s requests help: %s (%s)", requesting_agent, request_type, entry_id)
        try:
            return self.get_result(entry_id, timeout=timeout)
        except RuntimeError:
            logger.warning(
                "Blackboard request %s from %s timed out waiting for a handler: returning None",
                entry_id,
                requesting_agent,
            )
            return None

    def escalate_error(
        self,
        agent_type: str,
        task_id: str,
        error: str,
        context: dict[str, Any] | None = None,
    ) -> str:
        """Escalate an error to the blackboard for error recovery.

        Posts a high-priority error_recovery request that the
        ErrorRecoveryAgent (or OPERATIONS) can pick up.

        Returns:
            The entry_id of the escalation.
        """
        return self.post(
            content=f"Error in {agent_type} task {task_id}: {error}",
            request_type="error_recovery",
            requested_by=agent_type,
            priority=1,
            metadata={
                "original_task_id": task_id,
                "error": error,
                **(context or {}),
            },
        )

    def request_consensus(
        self,
        requesting_agent: str,
        subject: str,
        options: list,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Post a consensus-check request for multi-agent voting.

        Multiple agents can claim the entry and vote. The caller should collect
        votes from the result.

        Returns:
            The entry_id of the consensus request.
        """
        return self.post(
            content=f"Consensus needed: {subject}",
            request_type="architecture_decision",
            requested_by=requesting_agent,
            priority=3,
            metadata={
                "consensus_request": True,
                "subject": subject,
                "options": options,
                **(metadata or {}),
            },
        )

    def subscribe(self, callback: Callable[[BlackboardEntry], None]) -> None:
        """Register a callback invoked when new entries are posted."""
        with self._lock:
            self._observers.append(callback)

    def _notify_observers(self, entry: BlackboardEntry) -> None:
        for cb in self._observers:
            try:
                cb(entry)
            except Exception as exc:
                logger.warning("[Blackboard] Observer error: %s", exc)

    def purge_expired(self) -> int:
        """Remove expired entries. Returns count of purged entries.

        Returns:
            int value produced by purge_expired().
        """
        from vetinari.memory.blackboard import EntryState

        with self._lock:
            expired = [eid for eid, entry in self._entries.items() if entry.is_expired]
            for eid in expired:
                self._entries[eid].state = EntryState.EXPIRED
            cutoff = time.time() - 7200
            stale = [
                eid
                for eid, entry in self._entries.items()
                if entry.created_at < cutoff
                and entry.state in (EntryState.COMPLETED, EntryState.FAILED, EntryState.EXPIRED)
            ]
            for eid in stale:
                del self._entries[eid]
        self._persist_blackboard_or_raise("purge_expired")
        return len(expired)

    def get_stats(self) -> dict[str, int]:
        """Return a summary of entry counts grouped by state.

        Returns:
            Mapping from state name (e.g. ``"pending"``, ``"completed"``)
            to the number of entries currently in that state.
        """
        with self._lock:
            states: dict[str, int] = {}
            for entry in self._entries.values():
                states[entry.state.value] = states.get(entry.state.value, 0) + 1
        return states

    def clear(self) -> None:
        """Clear all entries (use in tests only)."""
        with self._lock:
            self._entries.clear()
        self._persist_blackboard_or_raise("clear")
