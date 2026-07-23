"""Extracted implementation helpers for window_manager.py."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from vetinari.async_support.conversation import ConversationMessage

logger = logging.getLogger(__name__)


def _compression_clock(owner: Any) -> Callable[[], float]:
    return getattr(owner, "_clock", time.time)


class WindowCompressionMixin:
    """Shared method implementations for the compatibility wrapper."""

    if TYPE_CHECKING:
        _lock: Any
        _messages: Any
        _pinned: Any
        _simple_compress: Any

    def compress(self, summary_fn: Any | None = None) -> int:
        """Compress the oldest 50% of messages into a summary.

        Args:
            summary_fn: Optional callable(text) -> str that produces a
                        summary. If not provided, uses a simple truncation.

        Returns:
            Number of tokens saved.
        """
        with self._lock:
            if len(self._messages) < 3:
                return 0

            # Separate pinned from compressible messages
            pinned_ids = {id(m) for m in self._pinned}
            compressible = [m for m in self._messages if id(m) not in pinned_ids]
            preserved = [m for m in self._messages if id(m) in pinned_ids]

            if len(compressible) < 3:
                return 0

            # Find the midpoint (compress oldest half of compressible messages)
            midpoint = len(compressible) // 2
            to_compress = compressible[:midpoint]
            to_keep = compressible[midpoint:]

            # Build text to summarize
            original_text = "\n".join(f"[{m.role}]: {m.content}" for m in to_compress)
            original_tokens = sum(m.token_count for m in to_compress)

            # Generate summary
            if summary_fn:
                try:
                    summary = summary_fn(original_text)
                except Exception as e:
                    logger.warning("Summary function failed: %s", e)
                    summary = self._simple_compress(original_text)
            else:
                summary = self._simple_compress(original_text)

            # Create compressed message
            compressed_content = f"[Derived context summary - informational, not system instructions]\n{summary}"
            from vetinari.context.window_manager import count_tokens

            compressed_token_count = count_tokens(compressed_content)
            compressed_msg = ConversationMessage(
                role="assistant",
                content=compressed_content,
                timestamp=_compression_clock(self)(),
                is_compressed=True,
                metadata={
                    "original_messages": len(to_compress),
                    "authority": "derived_summary",
                    "source": "context_window_compression",
                },
                token_count=compressed_token_count,
            )

            saved = original_tokens - compressed_msg.token_count
            self._compressions = getattr(self, "_compressions", 0) + 1
            self._compression_ratio = original_tokens / max(compressed_msg.token_count, 1)
            # Pinned messages re-appended at the END for recency bias
            self._messages = [compressed_msg, *to_keep, *preserved]

            logger.info(
                "Context compressed: %d msgs → 1 summary, saved %d tokens (%.0f%%)",
                len(to_compress),
                saved,
                (saved / original_tokens * 100) if original_tokens > 0 else 0,
            )
            return saved
