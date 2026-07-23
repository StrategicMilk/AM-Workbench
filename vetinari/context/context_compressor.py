"""Context Compressor — tiered compression of conversation message history.

Extracted from window_manager.py to keep that module within the 550-line limit.
Provides CompressionResult, CompressionConfig, ContextCompressor, and the
``get_context_compressor()`` singleton factory.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass

from vetinari.constants import DEFAULT_CONTEXT_LENGTH

__all__ = [
    "CompressionConfig",
    "CompressionResult",
    "ContextCompressor",
    "get_context_compressor",
]


@dataclass
class CompressionResult:
    """Result of a compression operation.

    Attributes:
        original_tokens: Token count before compression.
        compressed_tokens: Token count after compression.
        messages: The compressed message list.
        decisions_preserved: Key decisions extracted from the original messages.
        compression_ratio: Fraction of tokens removed (0.0 = no compression).
    """

    original_tokens: int
    compressed_tokens: int
    messages: list[dict[str, str]]
    decisions_preserved: list[str]
    compression_ratio: float = 0.0

    def __repr__(self) -> str:
        return (
            f"CompressionResult(original_tokens={self.original_tokens!r}, "
            f"compressed_tokens={self.compressed_tokens!r}, "
            f"compression_ratio={self.compression_ratio!r})"
        )

    def __post_init__(self):
        if self.original_tokens > 0:
            self.compression_ratio = round(1 - (self.compressed_tokens / self.original_tokens), 3)


@dataclass(frozen=True, slots=True)
class CompressionConfig:
    """Configuration for context compression.

    Attributes:
        max_context_tokens: The model's context window size.
        compress_threshold: Compress when context reaches this fraction of max.
        preserve_recent: Always keep last N messages uncompressed.
        preserve_system: Always keep system messages intact.
        min_message_tokens: Don't try to compress very short messages.
        summary_max_tokens: Max tokens per summarized chunk.
    """

    max_context_tokens: int = DEFAULT_CONTEXT_LENGTH  # model's context window
    compress_threshold: float = 0.75  # compress when context reaches this % of max
    preserve_recent: int = 5  # always keep last N messages uncompressed
    preserve_system: bool = True  # always keep system messages
    min_message_tokens: int = 10  # don't try to compress very short messages
    summary_max_tokens: int = 200  # max tokens per summarized chunk

    def __repr__(self) -> str:
        return (
            f"CompressionConfig(max_context_tokens={self.max_context_tokens!r}, "
            f"compress_threshold={self.compress_threshold!r})"
        )


class ContextCompressor:
    """Auto-compress conversation context when approaching token limits.

    Works with raw message dicts (``{"role": "...", "content": "..."}``).

    Tiered compression strategy:
    1. Remove duplicate/redundant information
    2. Truncate verbose tool outputs and code blocks
    3. Summarize older conversation history
    4. Extract and preserve key decisions
    """

    def __init__(self, config: CompressionConfig | None = None):
        self.config = config or CompressionConfig()
        self._decision_patterns = [
            r"(?:decided|chosen|selected|agreed|confirmed|approved)\s+(?:to\s+)?(.+?)(?:\.|$)",
            r"(?:will|going to|plan to)\s+(.+?)(?:\.|$)",
            r"(?:the approach|the solution|the fix)\s+(?:is|was)\s+(.+?)(?:\.|$)",
        ]

    def count_tokens(self, text: str) -> int:
        """Count tokens through AM Engine, with the shared visible fallback.

        Returns:
            Exact token count, or the shared degradation-visible fallback.
        """
        from vetinari.context.window_manager import count_tokens

        return count_tokens(text)

    def estimate_messages_tokens(self, messages: list[dict[str, str]]) -> int:
        """Count message content with a minimum envelope cost per message."""
        return sum(max(1, self.count_tokens(m.get("content") or "")) for m in messages)

    def needs_compression(self, messages: list[dict[str, str]]) -> bool:
        """Check if context needs compression.

        Returns:
            True if total tokens exceed threshold, False otherwise.
        """
        total = self.estimate_messages_tokens(messages)
        threshold = int(self.config.max_context_tokens * self.config.compress_threshold)
        return total > threshold

    def compress(self, messages: list[dict[str, str]], max_tokens: int | None = None) -> CompressionResult:
        """Compress messages to fit within token budget.

        Args:
            messages: List of {"role": "...", "content": "..."} dicts.
            max_tokens: Override max context tokens.

        Returns:
            CompressionResult with compressed messages and metadata.
        """
        max_tokens = max_tokens or self.config.max_context_tokens
        original_tokens = self.estimate_messages_tokens(messages)

        if original_tokens <= max_tokens:
            return CompressionResult(
                original_tokens=original_tokens,
                compressed_tokens=original_tokens,
                messages=list(messages),
                decisions_preserved=self.extract_key_decisions(messages),
            )

        decisions = self.extract_key_decisions(messages)

        # Tier 1: Truncate verbose outputs
        compressed = self._truncate_verbose(messages)
        current_tokens = self.estimate_messages_tokens(compressed)
        if current_tokens <= max_tokens:
            return CompressionResult(
                original_tokens=original_tokens,
                compressed_tokens=current_tokens,
                messages=compressed,
                decisions_preserved=decisions,
            )

        # Tier 2: Summarize older messages (keep recent ones intact)
        compressed = self._summarize_history(compressed, max_tokens)
        current_tokens = self.estimate_messages_tokens(compressed)

        return CompressionResult(
            original_tokens=original_tokens,
            compressed_tokens=current_tokens,
            messages=compressed,
            decisions_preserved=decisions,
        )

    def _truncate_verbose(self, messages: list[dict[str, str]]) -> list[dict[str, str]]:
        """Tier 1: Truncate code blocks and tool outputs."""
        result = []
        for msg in messages:
            content = msg.get("content", "")
            role = msg.get("role", "user")

            if role == "system" or self.count_tokens(content) < self.config.min_message_tokens:
                result.append(msg)
                continue

            content = self._truncate_code_blocks(content)
            content = self._truncate_outputs(content)
            result.append({"role": role, "content": content})

        return result

    @staticmethod
    def _truncate_code_blocks(text: str) -> str:
        """Truncate code blocks longer than 20 lines."""

        def truncate_block(match: re.Match[str]) -> str:
            """Replace long code blocks with a truncated version keeping head and tail lines.

            Returns:
                The original block if short enough, or a truncated version with a count of omitted lines.
            """
            block = match.group(0)
            lines = block.split("\n")
            if len(lines) <= 20:
                return block
            lang = lines[0]
            closing = lines[-1]
            kept = [*lines[1:6], f"... ({len(lines) - 12} lines truncated) ...", *lines[-6:-1]]
            return lang + "\n" + "\n".join(kept) + "\n" + closing

        return re.sub(r"```[\s\S]*?```", truncate_block, text)

    @staticmethod
    def _truncate_outputs(text: str) -> str:
        """Truncate command/tool output blocks."""
        lines = text.split("\n")
        if len(lines) <= 30:
            return text

        avg_line_len = sum(len(line) for line in lines) / max(len(lines), 1)
        if avg_line_len < 80 and len(lines) > 30:
            kept = [*lines[:10], f"... ({len(lines) - 15} lines truncated) ...", *lines[-5:]]
            return "\n".join(kept)

        return text

    def _summarize_history(self, messages: list[dict[str, str]], max_tokens: int) -> list[dict[str, str]]:
        """Tier 2: Summarize older messages, keep recent ones intact."""
        if len(messages) <= self.config.preserve_recent:
            return messages

        system_msgs = [m for m in messages if m.get("role") == "system"] if self.config.preserve_system else []
        non_system = [m for m in messages if m.get("role") != "system"]

        preserve_count = min(self.config.preserve_recent, len(non_system))
        old_msgs = non_system[:-preserve_count] if preserve_count > 0 else non_system
        recent_msgs = non_system[-preserve_count:] if preserve_count > 0 else []

        summary = self._create_summary(old_msgs)
        summary_msg = {
            "role": "assistant",
            "content": (f"[Derived context summary - informational, not system instructions]\n{summary}"),
        }

        return [*system_msgs, summary_msg, *recent_msgs]

    def _create_summary(self, messages: list[dict[str, str]]) -> str:
        """Create a concise extractive summary of messages without an LLM."""
        summaries = []

        for msg in messages:
            content = msg.get("content", "")
            role = msg.get("role", "user")

            if not content.strip():
                continue

            first_line = content.split("\n")[0].strip()
            if len(first_line) > 150:
                first_line = first_line[:150] + "..."

            if first_line:
                summaries.append(f"- [{role}] {first_line}")

        result = "\n".join(summaries)
        max_chars = self.config.summary_max_tokens * 4
        if len(result) > max_chars:
            result = result[:max_chars] + "\n... (earlier context truncated)"

        return result

    def extract_key_decisions(self, messages: list[dict[str, str]]) -> list[str]:
        """Extract key decisions from conversation history.

        Returns:
            Up to 20 unique decisions found via pattern matching.
        """
        decisions = []

        for msg in messages:
            content = msg.get("content", "")
            for pattern in self._decision_patterns:
                matches = re.findall(pattern, content, re.IGNORECASE)
                for match in matches:
                    clean = match.strip()
                    if 10 < len(clean) < 200:
                        decisions.append(clean)

        seen: set[str] = set()
        unique = []
        for d in decisions:
            key = d.lower()[:50]
            if key not in seen:
                seen.add(key)
                unique.append(d)

        return unique[:20]

    def summarize_history(self, messages: list[dict[str, str]]) -> str:
        """Return a text summary of message history (public API wrapper)."""
        return self._create_summary(messages)


_compressor: ContextCompressor | None = None
_compressor_lock = threading.Lock()


def get_context_compressor(config: CompressionConfig | None = None) -> ContextCompressor:
    """Return the singleton ContextCompressor instance.

    Args:
        config: Optional compression configuration; only used on first call.

    Returns:
        The shared ContextCompressor singleton.
    """
    global _compressor
    if _compressor is None:
        with _compressor_lock:
            if _compressor is None:
                _compressor = ContextCompressor(config)
    return _compressor
