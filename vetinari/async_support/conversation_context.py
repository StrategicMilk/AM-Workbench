"""Extracted implementation helpers for conversation.py."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from vetinari.async_support.conversation import ConversationMessage


class ContextReconstructorMixin:
    """Shared method implementations for the compatibility wrapper."""

    if TYPE_CHECKING:
        _SYSTEM_HEADER: Any

    def reconstruct(
        self,
        messages: list[ConversationMessage],
        max_tokens: int = 4096,
    ) -> str:
        """Build a context string from *messages* within *max_tokens*.

        The method works in three steps:

        1. Reserve space for the system header.
        2. Walk messages from newest to oldest, collecting those that fit.
        3. Prepend a summary placeholder for any omitted older messages.

        Args:
            messages: Conversation history (chronological order).
            max_tokens: Token budget for the assembled context.

        Returns:
            Formatted context string ready to prepend to a prompt.
        """
        if not messages:
            return self._SYSTEM_HEADER

        from vetinari.async_support.conversation import _count_tokens

        token_budget = max_tokens - _count_tokens(self._SYSTEM_HEADER)

        selected: list[ConversationMessage] = []
        for msg in reversed(messages):
            formatted = self._format_message(msg)
            formatted_tokens = _count_tokens(formatted)
            if formatted_tokens > token_budget:
                break
            selected.append(msg)
            token_budget -= formatted_tokens

        selected.reverse()
        omitted_count = len(messages) - len(selected)

        parts: list[str] = [self._SYSTEM_HEADER]
        if omitted_count > 0:
            parts.append(f"[{omitted_count} earlier message(s) not shown]\n\n")
        parts.extend(self._format_message(msg) for msg in selected)

        return "".join(parts)

    @staticmethod
    def _format_message(msg: ConversationMessage) -> str:
        """Render a single message as a labelled block.

        Args:
            msg: Message to format.

        Returns:
            Formatted string ending with a newline.
        """
        return f"{msg.role.upper()}: {msg.content}\n"
