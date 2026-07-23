"""Context Window Manager (C4).

============================
Tracks token usage per conversation and compresses context when approaching
the model's context window limit.  Also provides a standalone ``ContextCompressor``
for dict-based message compression with tiered strategies (truncate verbose
outputs → summarize history → extract key decisions).

Strategy:
  - Track tokens per message in the conversation history
  - When usage exceeds 75% of the window, compress the oldest 50% into a summary
  - Stage-boundary compression between pipeline stages
  - Configurable per-model context window sizes

Exact counts come from AM Engine. A degradation-visible fallback remains for
startup and engine-unavailable paths.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from vetinari.async_support.conversation import ConversationMessage
from vetinari.context.window_manager_compression import WindowCompressionMixin

# ── Known context window sizes ────────────────────────────────────────

_MODEL_CONTEXT_WINDOWS: dict[str, int] = {
    # Local models (llama-cpp-python)
    "qwen2.5-coder-7b": 32768,
    "qwen2.5-coder-14b": 32768,
    "qwen2.5-72b": 32768,
    "qwen3-30b-a3b": 32768,
    "qwen2.5-vl-32b": 32768,
    # Cloud models
    "claude-opus-4-8": 1000000,
    "claude-opus-4-7": 1000000,
    "claude-sonnet-4-6": 1000000,
    "claude-haiku-4-5-20251001": 200000,
    "gpt-4o": 128000,
    "gemini-3.5-flash": 1000000,
    "gemini-3.1-flash-lite": 1000000,
    "gemini-3.1-pro-preview": 1000000,
    # Defaults
    "default": 32768,
}


def fallback_estimate_tokens(text: str, tokenizer: Any = None) -> int:
    """Estimate tokens only when AM Engine exact counting is unavailable.

    When *tokenizer* is provided (e.g. a llama-cpp-python ``Llama`` instance),
    its ``tokenize()`` method is used for an accurate count. Otherwise the
    fallback uses the larger of a word estimate and a UTF-8 byte-density
    estimate. The byte-density floor prevents minified JSON, hashes, and other
    unspaced content from being counted as a single token.

    Args:
        text: The input string to tokenise.
        tokenizer: Optional object with a ``tokenize(bytes) -> list`` method.

    Returns:
        Estimated (or exact) number of tokens in the input text.
    """
    logger.warning("Using heuristic token count because AM Engine exact counting is unavailable")
    if not text:
        return 0
    # Prefer real tokenizer when available
    if tokenizer is not None:
        try:
            return len(tokenizer.tokenize(text.encode("utf-8")))
        except Exception:
            logger.warning("Tokenizer failed; falling back to word heuristic", exc_info=True)
    word_count = len(text.split())
    # Heuristic: if text has lots of symbols, use higher multiplier
    symbol_ratio = sum(1 for c in text if not c.isalnum() and not c.isspace()) / max(len(text), 1)
    multiplier = 1.5 if symbol_ratio > 0.15 else 1.3
    word_estimate = max(1, int(word_count * multiplier))
    byte_density_estimate = max(1, (len(text.encode("utf-8")) + 3) // 4)
    return max(word_estimate, byte_density_estimate)


def count_tokens(text: str, *, model_id: str | None = None, tokenizer: Any = None) -> int:
    """Return an exact AM Engine token count with an explicit degraded fallback.

    Returns:
        Exact token count, or a degradation-visible heuristic count when the
        engine and optional tokenizer are unavailable.
    """
    if not text:
        return 0
    if tokenizer is not None:
        try:
            return len(tokenizer.tokenize(text.encode("utf-8")))
        except Exception:
            logger.warning("Tokenizer failed; trying AM Engine exact counting", exc_info=True)
    try:
        from vetinari.engine import get_engine_client

        engine_client = get_engine_client()
        if model_id is None:
            response = engine_client.count_tokens((text,))
        else:
            response = engine_client.count_tokens((text,), model_id=model_id)
        if len(response.counts) != 1:
            raise ValueError(f"AM Engine returned {len(response.counts)} counts for one item")
        return response.counts[0]
    except Exception:
        logger.warning("AM Engine token counting failed; using degraded estimate", exc_info=True)
        return fallback_estimate_tokens(text)


@dataclass
class WindowConversationMessage:
    """A single message in the conversation context."""

    role: str  # "system", "user", "assistant"
    content: str
    token_count: int = 0
    is_compressed: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def __repr__(self) -> str:
        return f"WindowConversationMessage(role={self.role!r}, token_count={self.token_count!r}, content={self.content[:40]!r})"

    def __post_init__(self):
        if self.token_count == 0:
            self.token_count = count_tokens(self.content)


@dataclass
class WindowState:
    """Tracks the state of a context window."""

    model_id: str
    max_tokens: int
    used_tokens: int = 0
    message_count: int = 0
    compressions: int = 0
    compression_ratio: float = 1.0  # ratio of original to compressed

    def __repr__(self) -> str:
        return (
            f"WindowState(model_id={self.model_id!r}, used_tokens={self.used_tokens!r}, max_tokens={self.max_tokens!r})"
        )


class ContextWindowManager(WindowCompressionMixin):
    """Manages context window usage and compression for a single model session."""

    def __init__(
        self,
        model_id: str = "default",
        max_context_ratio: float = 0.75,
        compression_target: float = 0.5,
        summary_max_tokens: int = 500,
        auto_compress: bool = True,
    ):
        self.model_id = model_id
        self._max_context_ratio = max_context_ratio
        self._compression_target = compression_target
        self._summary_max_tokens = summary_max_tokens
        self._auto_compress = auto_compress
        self._messages: list[ConversationMessage] = []
        self._pinned: list[ConversationMessage] = []  # survives compression
        self._previously_injected: set[str] = set()  # SHA-256 keys for delta injection
        self._compressions = 0
        self._compression_ratio = 1.0
        self._lock = threading.Lock()

        # Determine window size
        self.window_size = _MODEL_CONTEXT_WINDOWS.get(model_id, _MODEL_CONTEXT_WINDOWS["default"])
        self._threshold = int(self.window_size * self._max_context_ratio)

    @staticmethod
    def _parse_timestamp(saved_at: str) -> float:
        """Convert SQLite datetime string to UNIX timestamp (seconds since epoch).

        Args:
            saved_at: ISO8601-formatted datetime string from SQLite (e.g., '2024-01-15 10:30:45').

        Returns:
            UNIX timestamp as float (seconds since epoch).
        """
        from datetime import datetime

        # SQLite's datetime('now') format: YYYY-MM-DD HH:MM:SS
        try:
            dt = datetime.strptime(saved_at, "%Y-%m-%d %H:%M:%S")
            return dt.timestamp()
        except (ValueError, TypeError) as e:
            # Fallback: return current time if parsing fails
            logger.warning(
                "Could not parse timestamp '%s' — using current time instead: %s",
                saved_at,
                e,
            )
            return time.time()

    @property
    def used_tokens(self) -> int:
        """Return the total tokens consumed across all stored messages."""
        return sum(m.token_count for m in self._messages)

    @property
    def remaining_tokens(self) -> int:
        """Return the number of tokens still available before hitting the window limit."""
        return max(0, self.window_size - self.used_tokens)

    @property
    def usage_ratio(self) -> float:
        """Return the fraction of the context window currently in use (0.0 to 1.0)."""
        return self.used_tokens / self.window_size if self.window_size > 0 else 0.0

    def add_message(self, role: str, content: str, **metadata: Any) -> int:
        """Add a message and return its estimated token count.

        When ``auto_compress`` is enabled and the window is over the threshold
        after this message is appended, compression includes the new message in
        the overflow calculation before preserving the recent tail.

        Args:
            role: Message role (``"system"``, ``"user"``, or ``"assistant"``).
            content: The message text content.
            **metadata: Additional metadata key-value pairs for the message.

        Returns:
            Estimated token count for the added message.
        """
        token_count = count_tokens(content, model_id=self.model_id)
        msg = ConversationMessage(
            role=role,
            content=content,
            timestamp=time.time(),
            metadata=metadata,
            token_count=token_count,
        )
        with self._lock:
            self._messages.append(msg)
        if self._auto_compress and self.should_compress():
            self.compress()
        return token_count

    def should_compress(self) -> bool:
        """Check if compression is needed (usage > threshold)."""
        return self.used_tokens > self._threshold

    def stage_boundary_compress(self, stage_name: str = "") -> int:
        """Compress at pipeline stage boundaries.

        Called between pipeline stages to keep context lean. Only compresses
        if usage is above 50% (lower threshold than regular compression).

        Args:
            stage_name: Name of the pipeline stage boundary for logging.

        Returns:
            Number of tokens saved by compression, or 0 if no compression needed.
        """
        if self.usage_ratio > 0.5:
            logger.info("Stage boundary compression at '%s'", stage_name)
            return self.compress()
        return 0

    def get_messages(self) -> list[dict[str, str]]:
        """Return messages in the format expected by LLM APIs."""
        return [{"role": m.role, "content": m.content} for m in self._messages]

    def get_state(self) -> WindowState:
        """Return current window state."""
        return WindowState(
            model_id=self.model_id,
            max_tokens=self.window_size,
            used_tokens=self.used_tokens,
            message_count=len(self._messages),
            compressions=self._compressions,
            compression_ratio=self._compression_ratio,
        )

    def clear(self) -> None:
        """Clear all messages."""
        with self._lock:
            self._messages.clear()
            self._pinned.clear()
            self._previously_injected.clear()

    def pin_messages(self, messages: list[ConversationMessage]) -> None:
        """Mark messages as pinned so they survive compression.

        Pinned messages are re-appended at the end of the message list
        after each compression cycle, preserving recency bias.

        Args:
            messages: List of ConversationMessage objects to pin.  These
                must already be present in ``self._messages``.
        """
        with self._lock:
            for msg in messages:
                if msg not in self._pinned:
                    self._pinned.append(msg)

    def inject_context(self, context_items: list[str]) -> int:
        """Inject context strings as system messages, skipping already-seen ones.

        Deduplication is performed via SHA-256 hashes to prevent injecting
        the same context chunk multiple times across successive calls.

        Args:
            context_items: List of context strings to inject as system messages.

        Returns:
            Number of new (non-duplicate) items injected.
        """
        import hashlib

        injected = 0
        for item in context_items:
            key = hashlib.sha256(item.encode("utf-8")).hexdigest()
            if key in self._previously_injected:
                continue
            self._previously_injected.add(key)
            self.add_message("system", f"[Injected context]\n{item}")
            injected += 1
        return injected

    def page_in(self, query: str, memory_store: Any = None) -> list[str]:
        """Retrieve relevant memories from long-term storage and inject them.

        Searches the shared memory store for entries relevant to *query*
        and injects them into the context window as system messages,
        skipping any already seen (delta injection).

        Args:
            query: Natural language query to search memory with.
            memory_store: Optional memory store instance.  Defaults to
                :func:`~vetinari.memory.shared.get_shared_memory` when None.

        Returns:
            List of context string snippets that were injected (excludes
            duplicates that were skipped).
        """
        try:
            store = memory_store
            if store is None:
                from vetinari.memory.shared import get_shared_memory

                store = get_shared_memory()
            results = store.search(query, limit=5)
            snippets = [e.content for e in results if hasattr(e, "content")]
            import hashlib

            previously_injected = set(self._previously_injected)
            injected = self.inject_context(snippets)
            newly_injected = [
                snippet
                for snippet in snippets
                if hashlib.sha256(snippet.encode("utf-8")).hexdigest() not in previously_injected
            ][:injected]
            logger.debug("page_in: injected %d/%d context snippets for query %r", injected, len(snippets), query)
            return newly_injected
        except Exception as exc:
            logger.warning("page_in failed: %s", exc)
            return []

    def page_out(self, count: int = 5) -> list[ConversationMessage]:
        """Remove the oldest non-pinned messages from the active window.

        Removed messages are candidates for archiving to long-term memory.
        They are returned so that callers can decide whether to persist them.

        Args:
            count: Maximum number of messages to evict.

        Returns:
            List of evicted :class:`ConversationMessage` objects (oldest first).
        """
        with self._lock:
            pinned_ids = {id(m) for m in self._pinned}
            evictable = [m for m in self._messages if id(m) not in pinned_ids]
            to_evict = evictable[:count]
            for msg in to_evict:
                self._messages.remove(msg)
        return to_evict

    def summarize_and_archive(self, memory_store: Any = None) -> str:
        """Summarise the current window and store it in long-term memory.

        Compresses the full message list into a summary string, writes it
        to the provided (or default) memory store, and clears the window.

        Args:
            memory_store: Optional memory store instance.  Defaults to
                :func:`~vetinari.memory.shared.get_shared_memory` when None.

        Returns:
            The summary text that was archived, or an empty string on failure.
        """
        with self._lock:
            msgs = list(self._messages)
        if not msgs:
            return ""
        full_text = "\n".join(f"[{m.role}]: {m.content}" for m in msgs)
        summary = self._simple_compress(full_text)
        try:
            store = memory_store
            if store is None:
                from vetinari.memory.shared import get_shared_memory

                store = get_shared_memory()
            from vetinari.memory.interfaces import MemoryEntry, MemoryType

            entry = MemoryEntry(
                agent="context_window_manager",
                entry_type=MemoryType.DISCOVERY,
                content=summary,
                summary=f"Context window summary ({len(msgs)} messages)",
            )
            store.remember(entry)
            logger.info("summarize_and_archive: stored %d-msg summary in memory", len(msgs))
        except Exception as exc:
            logger.warning("summarize_and_archive: memory store failed: %s", exc)
        self.clear()
        return summary

    def save(self, session_id: str) -> int:
        """Persist the current message list to the ``context_window_history`` table.

        Replaces any previously saved state for *session_id* atomically.

        Args:
            session_id: Unique session identifier used as the storage key.

        Returns:
            Number of messages persisted.
        """
        from vetinari.database import get_connection

        conn = get_connection()
        with self._lock:
            msgs = list(self._messages)
        conn.execute("DELETE FROM context_window_history WHERE session_id = ?", (session_id,))
        conn.executemany(
            """INSERT INTO context_window_history
               (session_id, model_id, position, role, content, token_count, is_compressed)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            [
                (session_id, self.model_id, pos, m.role, m.content, m.token_count, int(m.is_compressed))
                for pos, m in enumerate(msgs)
            ],
        )
        conn.commit()
        logger.debug("ContextWindowManager.save: %d messages for session %s", len(msgs), session_id)
        return len(msgs)

    def load(self, session_id: str) -> int:
        """Load message history from the ``context_window_history`` table.

        Replaces the current in-memory message list with the stored state.

        Args:
            session_id: Unique session identifier to load from.

        Returns:
            Number of messages loaded, or 0 when no saved state exists.
        """
        from vetinari.database import get_connection

        conn = get_connection()
        rows = conn.execute(
            """SELECT role, content, token_count, is_compressed, saved_at
               FROM context_window_history
               WHERE session_id = ?
               ORDER BY position ASC""",
            (session_id,),
        ).fetchall()
        if not rows:
            return 0
        new_msgs = [
            ConversationMessage(
                role=row[0],
                content=row[1],
                timestamp=time.time() if not row[4] else self._parse_timestamp(row[4]),
                token_count=row[2],
                is_compressed=bool(row[3]),
            )
            for row in rows
        ]
        with self._lock:
            self._messages = new_msgs
        logger.debug("ContextWindowManager.load: %d messages for session %s", len(new_msgs), session_id)
        return len(new_msgs)

    def _simple_compress(self, text: str) -> str:
        """Simple compression: keep first and last parts, drop middle."""
        words = text.split()
        target_words = self._summary_max_tokens  # rough approximation
        if len(words) <= target_words:
            return text
        half = target_words // 2
        return " ".join(words[:half]) + "\n[...compressed...]\n" + " ".join(words[-half:])

    def to_dict(self) -> dict[str, Any]:
        """Dashboard-friendly state."""
        return {
            "model_id": self.model_id,
            "window_size": self.window_size,
            "used_tokens": self.used_tokens,
            "remaining_tokens": self.remaining_tokens,
            "usage_ratio": round(self.usage_ratio, 3),
            "message_count": len(self._messages),
            "threshold": self._threshold,
            "should_compress": self.should_compress(),
        }


# ── Singleton ─────────────────────────────────────────────────────────

_window_managers: dict[str, ContextWindowManager] = {}
_window_manager_lock = threading.Lock()


def get_window_manager(model_id: str = "default") -> ContextWindowManager:
    """Get or create a context window manager keyed by model_id.

    Each distinct model_id gets its own manager with the appropriate
    context window size, avoiding the bug where only the first caller's
    model_id was honoured.

    Args:
        model_id: Model identifier used to look up the context window size.

    Returns:
        The ContextWindowManager for the given model.
    """
    if model_id not in _window_managers:
        with _window_manager_lock:
            if model_id not in _window_managers:
                _window_managers[model_id] = ContextWindowManager(model_id=model_id)
    return _window_managers[model_id]


# ── ContextCompressor and related types — moved to context_compressor.py ──
from vetinari.context.context_compressor import (
    CompressionConfig,
    CompressionResult,
    ContextCompressor,
    get_context_compressor,
)

logger = logging.getLogger(__name__)


__all__ = [
    "CompressionConfig",
    "CompressionResult",
    "ContextCompressor",
    "ContextWindowManager",
    "ConversationMessage",
    "WindowState",
    "count_tokens",
    "fallback_estimate_tokens",
    "get_context_compressor",
]
