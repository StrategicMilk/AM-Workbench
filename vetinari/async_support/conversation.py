"""Multi-turn conversation memory with token-aware context windowing and SQLite persistence.

Conversation history is stored both in-memory (for fast access) and in the unified
SQLite database (for durability across process restarts).

Database table (part of the unified schema in ``vetinari.database``):

.. code-block:: sql

    CREATE TABLE IF NOT EXISTS conversation_messages (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id   TEXT NOT NULL,
        role         TEXT NOT NULL,
        content      TEXT NOT NULL,
        timestamp    REAL NOT NULL,
        metadata_json TEXT NOT NULL DEFAULT '{}',
        created_at   TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE INDEX IF NOT EXISTS idx_conv_session ON conversation_messages(session_id);
    CREATE INDEX IF NOT EXISTS idx_conv_session_ts ON conversation_messages(session_id, timestamp);

On startup, ``ConversationStore.__init__`` loads the most recent ``_MAX_SESSIONS``
sessions from SQLite into the in-memory LRU cache.  When the in-memory cache evicts
a session (LRU overflow), the messages remain in SQLite and are re-loaded on demand
by ``get_history()`` and ``get_context_window()``.

This is step 3 support in the pipeline: requests carry multi-turn context that must
survive process restarts so users can resume conversations after server reboots.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

from vetinari.async_support.conversation_context import ContextReconstructorMixin
from vetinari.exceptions import ExecutionError
from vetinari.privacy.envelope import (
    PRIVACY_ENVELOPE_KEY,
    extract_privacy_envelope,
    require_privacy_envelope,
    wrap_for_persistence,
)

logger = logging.getLogger(__name__)


_CONVERSATION_RETENTION_DAYS = 30
_CONVERSATION_RETENTION_SECONDS = _CONVERSATION_RETENTION_DAYS * 24 * 60 * 60


def _count_tokens(content: str) -> int:
    """Return an exact engine count, preserving a visible degraded path."""
    from vetinari.context.window_manager import count_tokens

    return count_tokens(content)


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class ConversationMessage:
    """A single message in a conversation session.

    Attributes:
        role: Speaker role — typically ``"user"``, ``"assistant"``, or
            ``"system"``.
        content: Text content of the message.
        timestamp: UNIX timestamp (seconds) when the message was added.
        metadata: Arbitrary key-value metadata.
        token_count: Exact token count, or a degradation-visible fallback count.
    """

    role: str
    content: str
    timestamp: float
    metadata: dict[str, Any] = field(default_factory=dict)
    token_count: int = field(default=0)
    is_compressed: bool = field(default=False)

    def __repr__(self) -> str:
        return f"ConversationMessage(role={self.role!r}, content={self.content[:40]!r})"


# ---------------------------------------------------------------------------
# ConversationStore
# ---------------------------------------------------------------------------


class ConversationStore:
    """Thread-safe conversation session store backed by SQLite for durability.

    The store keeps up to ``_MAX_SESSIONS`` sessions in memory as an LRU cache.
    Every message is also written to the ``conversation_messages`` table in the
    unified SQLite database so history survives process restarts.

    On construction, existing sessions are restored from SQLite (up to
    ``_MAX_SESSIONS`` most recent by session_id sort order).  When a session is
    evicted from the in-memory LRU, its messages remain in SQLite and are
    re-loaded transparently when ``get_history()`` or ``get_context_window()``
    is called for that session.

    Obtain the singleton instance via :func:`get_conversation_store`.

    All public methods are safe to call from multiple threads simultaneously.
    """

    _MAX_SESSIONS = 200  # prevent unbounded session accumulation

    def __init__(self) -> None:
        # Module-level mutable state:
        #   _sessions: LRU cache keyed by session_id; written by add_message/
        #              create_session, read by get_history/get_context_window.
        #   _session_modes: per-session conversation mode (FSA-0050).  Default
        #              "task" routes to task execution; "free_form" disables
        #              task routing so the conversation stays chat-only.
        #   _lock: protects all reads/writes to _sessions (not SQLite — SQLite
        #          handles its own locking via WAL mode).
        self._sessions: OrderedDict[str, list[ConversationMessage]] = OrderedDict()
        self._session_modes: dict[str, str] = {}
        self._engine_session_ids: dict[str, str] = {}
        self._lock = threading.Lock()
        self._restore_from_db()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _restore_from_db(self) -> None:
        """Load existing sessions from SQLite into the in-memory LRU cache.

        Queries the ``conversation_messages`` table for distinct session IDs,
        loads the most recent ``_MAX_SESSIONS`` sessions by message timestamp,
        and populates ``_sessions``.  Called once from ``__init__``.
        """
        try:
            # Lazy import to avoid circular dependency at module load time.
            from vetinari.database import get_connection

            conn = get_connection()
            cursor = conn.execute(
                "SELECT session_id FROM conversation_messages "
                "GROUP BY session_id "
                "ORDER BY MAX(timestamp) DESC, MAX(id) DESC "
                "LIMIT ?",
                (self._MAX_SESSIONS,),
            )
            session_ids = [row[0] for row in cursor.fetchall()]

            # Reinsert oldest-to-newest so the OrderedDict LRU tail is the
            # newest restored session, not the lexicographically largest ID.
            sessions_to_load = list(reversed(session_ids))

            loaded = 0
            for sid in sessions_to_load:
                messages = self._load_session_from_db(sid)
                with self._lock:
                    self._sessions[sid] = messages
                loaded += 1

            if loaded:
                logger.info("Restored %d conversation sessions from SQLite", loaded)
        except Exception as exc:
            logger.warning(
                "Could not restore conversation sessions from SQLite — starting with empty in-memory store: %s",
                exc,
            )

    @staticmethod
    def _message_metadata_for_persistence(session_id: str, msg: ConversationMessage) -> dict[str, Any]:
        metadata = dict(msg.metadata)
        envelope_source = f"conversation:{session_id}:{msg.role}"
        metadata[PRIVACY_ENVELOPE_KEY] = extract_privacy_envelope(
            wrap_for_persistence(
                {},
                privacy_class="subject_data",
                source=envelope_source,
                subject_id=session_id,
                retention_days=_CONVERSATION_RETENTION_DAYS,
            )
        )
        return metadata

    @staticmethod
    def _message_is_expired(timestamp: float, metadata: dict[str, Any], now: float | None = None) -> bool:
        current = time.time() if now is None else now
        try:
            envelope = extract_privacy_envelope(require_privacy_envelope(metadata))
            retention_days = int(envelope.get("retention_days") or _CONVERSATION_RETENTION_DAYS)
        except (TypeError, ValueError):
            retention_days = _CONVERSATION_RETENTION_DAYS
        return current - float(timestamp) > retention_days * 24 * 60 * 60

    @staticmethod
    def _delete_expired_messages(session_id: str, cutoff: float) -> None:
        try:
            from vetinari.database import get_connection

            conn = get_connection()
            conn.execute(
                "DELETE FROM conversation_messages WHERE session_id = ? AND timestamp < ?",
                (session_id, cutoff),
            )
            conn.commit()
        except Exception as exc:
            logger.warning("Could not prune expired messages for session %s: %s", session_id, exc)

    @staticmethod
    def _load_session_from_db(session_id: str) -> list[ConversationMessage]:
        """Fetch all messages for *session_id* from SQLite, ordered by timestamp.

        Args:
            session_id: The session whose messages to load.

        Returns:
            List of :class:`ConversationMessage` ordered oldest-first.
            Returns an empty list if the session has no persisted messages or
            if the database is unavailable.
        """
        try:
            from vetinari.database import get_connection

            conn = get_connection()
            cursor = conn.execute(
                "SELECT role, content, timestamp, metadata_json "
                "FROM conversation_messages "
                "WHERE session_id = ? "
                "ORDER BY timestamp ASC, id ASC",
                (session_id,),
            )
            messages: list[ConversationMessage] = []
            expired_count = 0
            for row in cursor.fetchall():
                try:
                    metadata = json.loads(row[3] or "{}")
                except (json.JSONDecodeError, TypeError):
                    metadata = {}
                timestamp = float(row[2])
                if ConversationStore._message_is_expired(timestamp, metadata):
                    expired_count += 1
                    continue
                user_metadata = dict(metadata)
                user_metadata.pop(PRIVACY_ENVELOPE_KEY, None)
                messages.append(
                    ConversationMessage(
                        role=row[0],
                        content=row[1],
                        timestamp=timestamp,
                        metadata=user_metadata,
                    )
                )
            if expired_count:
                ConversationStore._delete_expired_messages(
                    session_id,
                    time.time() - _CONVERSATION_RETENTION_SECONDS,
                )
            return messages
        except Exception as exc:
            logger.warning(
                "Could not load session %s from SQLite — returning empty history: %s",
                session_id,
                exc,
            )
            return []

    @staticmethod
    def _persist_message(
        session_id: str,
        msg: ConversationMessage,
    ) -> None:
        """Write *msg* to the SQLite ``conversation_messages`` table.

        Best-effort: logs a warning on failure but never raises to the caller.

        Args:
            session_id: Session the message belongs to.
            msg: The message to persist.
        """
        try:
            from vetinari.database import get_connection

            conn = get_connection()
            conn.execute(
                "INSERT INTO conversation_messages "
                "(session_id, role, content, timestamp, metadata_json) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    session_id,
                    msg.role,
                    msg.content,
                    msg.timestamp,
                    json.dumps(ConversationStore._message_metadata_for_persistence(session_id, msg)),
                ),
            )
            conn.commit()
        except Exception as exc:
            logger.warning(
                "Could not persist message for session %s to SQLite — message stored in-memory only: %s",
                session_id,
                exc,
            )

    @staticmethod
    def _delete_session_from_db(session_id: str) -> None:
        """Delete all persisted messages for *session_id* from SQLite.

        Best-effort: logs a warning on failure but never raises.

        Args:
            session_id: The session whose messages to delete.
        """
        try:
            from vetinari.database import get_connection

            conn = get_connection()
            conn.execute(
                "DELETE FROM conversation_messages WHERE session_id = ?",
                (session_id,),
            )
            conn.commit()
        except Exception as exc:
            logger.warning(
                "Could not delete session %s from SQLite — messages may reappear on next restart: %s",
                session_id,
                exc,
            )

    def _ensure_in_memory(self, session_id: str) -> bool:
        """Ensure *session_id* is in the in-memory cache, loading from SQLite if needed.

        Must be called WITHOUT holding ``_lock``.  Acquires the lock internally
        when writing into ``_sessions``.

        Args:
            session_id: The session ID to load.

        Returns:
            ``True`` if the session is now in ``_sessions`` (either it was
            already there or was successfully loaded from SQLite).
            ``False`` if the session does not exist in SQLite either.
        """
        with self._lock:
            if session_id in self._sessions:
                return True

        # Not in memory — try loading from SQLite.
        messages = self._load_session_from_db(session_id)
        # An empty list could mean the session truly doesn't exist, or it has
        # no messages yet.  Check whether the session_id appears in SQLite at
        # all (any row for it) to distinguish the two cases.
        try:
            from vetinari.database import get_connection

            conn = get_connection()
            row = conn.execute(
                "SELECT 1 FROM conversation_messages WHERE session_id = ? LIMIT 1",
                (session_id,),
            ).fetchone()
            exists_in_db = row is not None
        except Exception:
            exists_in_db = bool(messages)

        if not exists_in_db and not messages:
            return False

        with self._lock:
            # Another thread may have loaded it while we were querying.
            if session_id not in self._sessions:
                # Evict oldest if at capacity.
                while len(self._sessions) >= self._MAX_SESSIONS:
                    evicted_id, _ = self._sessions.popitem(last=False)
                    self._engine_session_ids.pop(evicted_id, None)
                    logger.debug("Evicted oldest conversation session %s", evicted_id)
                self._sessions[session_id] = messages
        return True

    # ------------------------------------------------------------------
    # Session management
    # ------------------------------------------------------------------

    def create_session(self, session_id: str | None = None, *, mode: str = "task") -> str:
        """Create a new conversation session.

        Args:
            session_id: Optional explicit ID.  A UUID4 is generated when
                omitted.
            mode: Conversation mode.  ``"task"`` (default) routes user
                turns through the task-execution pipeline; ``"free_form"``
                (FSA-0050) keeps the conversation chat-only and disables
                task routing.  Unknown values fail closed to ``"task"``
                with a warning log.

        Returns:
            The session ID string.

        Raises:
            ExecutionError: If *session_id* already exists.
        """
        normalized_mode = mode if mode in ("task", "free_form") else "task"
        if normalized_mode != mode:
            logger.warning(
                "Unknown conversation mode %r — falling back to 'task' for safety",
                mode,
            )
        sid = session_id or str(uuid.uuid4())
        with self._lock:
            if sid in self._sessions:
                raise ExecutionError(f"Session '{sid}' already exists")
            # Evict oldest sessions when at capacity.
            while len(self._sessions) >= self._MAX_SESSIONS:
                evicted_id, _ = self._sessions.popitem(last=False)
                self._session_modes.pop(evicted_id, None)
                self._engine_session_ids.pop(evicted_id, None)
                logger.debug("Evicted oldest conversation session %s", evicted_id)
            self._sessions[sid] = []
            self._session_modes[sid] = normalized_mode
        logger.debug("Created conversation session %s (mode=%s)", sid, normalized_mode)
        return sid

    def get_engine_session_id(self, session_id: str) -> str | None:
        """Return the AM Engine KV-session handle for a conversation.

        Returns:
            Bound engine session ID, or ``None`` when no live handle exists.
        """
        with self._lock:
            return self._engine_session_ids.get(session_id)

    def set_engine_session_id(self, session_id: str, engine_session_id: str) -> None:
        """Bind a first-turn AM Engine KV-session handle to a conversation.

        Args:
            session_id: Conversation that owns the engine session.
            engine_session_id: Non-empty AM Engine KV-session handle.

        Raises:
            KeyError: If the conversation does not exist.
            ValueError: If the engine session handle is empty.
        """
        if not engine_session_id:
            raise ValueError("engine_session_id must be non-empty")
        with self._lock:
            if session_id not in self._sessions:
                raise KeyError(f"Session '{session_id}' does not exist")
            self._engine_session_ids[session_id] = engine_session_id

    def clear_engine_session_id(self, session_id: str) -> None:
        """Clear a stale KV-session handle after engine-down/session-unknown."""
        with self._lock:
            self._engine_session_ids.pop(session_id, None)

    def get_session_mode(self, session_id: str) -> str:
        """Return the conversation mode for *session_id*.

        Args:
            session_id: Session to look up.

        Returns:
            ``"task"`` or ``"free_form"``.  Defaults to ``"task"`` for
            unknown or pre-existing sessions that predate the mode field.
        """
        with self._lock:
            return self._session_modes.get(session_id, "task")

    def should_route_to_task_execution(self, session_id: str) -> bool:
        """Return whether incoming messages for *session_id* should route to tasks.

        Free-form sessions (FSA-0050) explicitly disable task routing so the
        user can chat without the system kicking off task decomposition for
        every message.

        Args:
            session_id: Session to evaluate.

        Returns:
            True for ``"task"`` mode (the default); False for ``"free_form"``.
        """
        return self.get_session_mode(session_id) != "free_form"

    def add_message(
        self,
        session_id: str,
        role: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Append a message to an existing session and persist it to SQLite.

        The message is appended to the in-memory list inside ``_lock``, then
        written to SQLite outside the lock (best-effort — a DB failure logs a
        warning but never blocks the caller).

        Args:
            session_id: Target session ID.
            role: Speaker role (e.g. ``"user"`` or ``"assistant"``).
            content: Message text.
            metadata: Optional metadata dict.

        Raises:
            KeyError: If *session_id* does not exist.
        """
        msg = ConversationMessage(
            role=role,
            content=content,
            timestamp=time.time(),
            metadata=metadata or {},
            token_count=_count_tokens(content),
        )
        with self._lock:
            if session_id not in self._sessions:
                raise KeyError(f"Session '{session_id}' does not exist")
            self._sessions[session_id].append(msg)

        # Write to SQLite outside the lock — WAL mode handles concurrent writers.
        self._persist_message(session_id, msg)

    # ------------------------------------------------------------------
    # Retrieval
    # ------------------------------------------------------------------

    def get_history(self, session_id: str, limit: int = 50) -> list[ConversationMessage]:
        """Return up to *limit* most recent messages for a session.

        If the session was evicted from the in-memory LRU, it is transparently
        re-loaded from SQLite before returning.

        Args:
            session_id: Target session ID.
            limit: Maximum number of messages to return.  0 means all.

        Returns:
            List of :class:`ConversationMessage` objects, oldest first.

        Raises:
            KeyError: If *session_id* does not exist in memory or SQLite.
        """
        if not self._ensure_in_memory(session_id):
            raise KeyError(f"Session '{session_id}' does not exist")

        with self._lock:
            messages = list(self._sessions[session_id])

        if limit and len(messages) > limit:
            return messages[-limit:]
        return messages

    def get_context_window(self, session_id: str, max_tokens: int = 4096) -> list[ConversationMessage]:
        """Return the most recent messages that fit within a token budget.

        Token counts come from AM Engine when available. Messages are included
        from newest to oldest until the budget
        is exhausted, then returned in chronological order.

        If the session was evicted from the in-memory LRU, it is transparently
        re-loaded from SQLite before returning.

        Args:
            session_id: Target session ID.
            max_tokens: Maximum number of tokens to include.

        Returns:
            List of :class:`ConversationMessage` objects fitting within the
            token budget, oldest first.

        Raises:
            KeyError: If *session_id* does not exist in memory or SQLite.
        """
        if not self._ensure_in_memory(session_id):
            raise KeyError(f"Session '{session_id}' does not exist")

        with self._lock:
            messages = list(self._sessions[session_id])

        selected: list[ConversationMessage] = []
        budget = max_tokens

        for msg in reversed(messages):
            cost = msg.token_count or _count_tokens(msg.content)
            if cost > budget:
                break
            selected.append(msg)
            budget -= cost

        selected.reverse()
        return selected

    # ------------------------------------------------------------------
    # Housekeeping
    # ------------------------------------------------------------------

    def clear_session(self, session_id: str) -> None:
        """Remove all messages from a session (in memory and in SQLite).

        Args:
            session_id: Target session ID.

        Raises:
            KeyError: If *session_id* does not exist.
        """
        with self._lock:
            if session_id not in self._sessions:
                raise KeyError(f"Session '{session_id}' does not exist")
            self._sessions[session_id] = []
            self._engine_session_ids.pop(session_id, None)

        # Delete persisted rows outside the lock — best-effort.
        self._delete_session_from_db(session_id)

    def list_sessions(self) -> list[str]:
        """Return the IDs of all existing in-memory sessions.

        Returns:
            Sorted list of session ID strings currently held in the LRU cache.
        """
        with self._lock:
            return sorted(self._sessions.keys())


# ---------------------------------------------------------------------------
# Singleton accessor
# ---------------------------------------------------------------------------

_store: ConversationStore | None = None
_store_lock = threading.Lock()


def get_conversation_store() -> ConversationStore:
    """Return the process-wide singleton :class:`ConversationStore`.

    The store is created on first call and restores persisted sessions from
    SQLite during construction.  Thread-safe via double-checked locking.

    Returns:
        The singleton :class:`ConversationStore` instance.
    """
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                _store = ConversationStore()
    return _store


def _reset_conversation_store() -> None:
    """Reset the singleton and clear the SQLite backing table (for testing only).

    Drops all rows from ``conversation_messages`` so tests start from a clean
    state regardless of prior test runs.
    """
    global _store
    with _store_lock:
        _store = None

    # Clear the SQLite table so persistence tests are isolated.
    try:
        from vetinari.database import get_connection

        conn = get_connection()
        conn.execute("DELETE FROM conversation_messages")
        conn.commit()
    except Exception as exc:
        logger.warning("Could not clear conversation_messages table during store reset: %s", exc)


# ---------------------------------------------------------------------------
# ContextReconstructor
# ---------------------------------------------------------------------------


class ContextReconstructor(ContextReconstructorMixin):
    """Build a formatted prompt context string from conversation history.

    The reconstructor fits the most recent messages into a token budget,
    prepends a system header, and summarises older messages when present.
    """

    _SYSTEM_HEADER = "You are a helpful AI assistant.\n\n"
    _SUMMARY_HEADER = "[Earlier conversation summarised]\n\n"
