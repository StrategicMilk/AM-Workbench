"""Extracted implementation helpers for compaction.py."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from vetinari.context.session_state import SessionState, get_session_state_extractor
from vetinari.context.window_manager import WindowConversationMessage

logger = logging.getLogger(__name__)
_ROLE_SYSTEM = "system"
_ROLE_ASSISTANT = "assistant"
_STATE_SUMMARY_PREFIX = "[Session state summary]\n"
_HISTORY_SUMMARY_PREFIX = "[Conversation history summary]\n"
_STATE_EXTRACTION_TIER = "state_extraction"
_SUMMARY_TIER = "summary"


class CompactionTierMixin:
    """Shared method implementations for the compatibility wrapper."""

    if TYPE_CHECKING:
        _build_resume_instruction: Any
        _format_state_as_summary: Any
        _preserve_recent: Any
        _simple_summarize: Any

    def _tier1_state_extraction(
        self,
        messages: list[WindowConversationMessage],
        task_id: str,
        stage: str,
        model_id: str,
    ) -> tuple[list[WindowConversationMessage], SessionState | None]:
        """Tier 1: extract structured state from older messages then replace them.

        Concatenates the content of all messages except the most recent
        ``preserve_recent`` ones, runs ``SessionStateExtractor`` over that
        text, and replaces the older messages with a single compact state-summary
        system message followed by a resume instruction.

        Args:
            messages: Full message list to process.
            task_id: Forwarded to ``SessionStateExtractor.extract``.
            stage: Forwarded to ``SessionStateExtractor.extract``.
            model_id: Forwarded to ``SessionStateExtractor.extract``.

        Returns:
            Two-tuple of (new_messages, extracted_state). The state is None
            if there were not enough messages to extract from.
        """
        if len(messages) <= self._preserve_recent:
            # Nothing old enough to replace — return unchanged.
            return list(messages), None

        split = len(messages) - self._preserve_recent
        older = messages[:split]
        recent = messages[split:]

        combined_text = "\n".join(f"[{m.role}]: {m.content}" for m in older)
        extractor = get_session_state_extractor()
        state = extractor.extract(
            text=combined_text,
            task_id=task_id,
            stage=stage,
            model_id=model_id,
        )

        summary_text = self._format_state_as_summary(state)
        resume_text = self._build_resume_instruction(state, summary=summary_text)

        state_msg = WindowConversationMessage(
            role=_ROLE_ASSISTANT,
            content=f"{_STATE_SUMMARY_PREFIX}{summary_text}",
            is_compressed=True,
            metadata={
                "compaction_tier": _STATE_EXTRACTION_TIER,
                "original_count": len(older),
                "authority": "derived_summary",
                "source": "context_compaction",
            },
        )
        resume_msg = WindowConversationMessage(
            role=_ROLE_SYSTEM,
            content=resume_text,
            is_compressed=True,
            metadata={"compaction_tier": _STATE_EXTRACTION_TIER, "is_resume_instruction": True},
        )

        new_messages = [state_msg, resume_msg, *recent]
        logger.debug(
            "_tier1_state_extraction: replaced %d older messages with state+resume, kept %d recent",
            len(older),
            len(recent),
        )
        return new_messages, state

    def _tier2_summarize(self, messages: list[WindowConversationMessage]) -> list[WindowConversationMessage]:
        """Tier 2: summarize older messages, preserve the most recent N verbatim.

        Builds a bullet-point plaintext summary of the older portion and
        inserts it as a single compressed system message at the front of the
        recent tail.

        Args:
            messages: Full message list to process.

        Returns:
            New message list with a summary prepended to the recent tail.
        """
        if len(messages) <= self._preserve_recent:
            return list(messages)

        split = len(messages) - self._preserve_recent
        older = messages[:split]
        recent = messages[split:]

        summary_text = self._simple_summarize(older)
        resume_text = self._build_resume_instruction(state=None, summary=summary_text)

        summary_msg = WindowConversationMessage(
            role=_ROLE_ASSISTANT,
            content=f"{_HISTORY_SUMMARY_PREFIX}{summary_text}",
            is_compressed=True,
            metadata={
                "compaction_tier": _SUMMARY_TIER,
                "original_count": len(older),
                "authority": "derived_summary",
                "source": "context_compaction",
            },
        )
        resume_msg = WindowConversationMessage(
            role=_ROLE_SYSTEM,
            content=resume_text,
            is_compressed=True,
            metadata={"compaction_tier": _SUMMARY_TIER, "is_resume_instruction": True},
        )

        logger.debug(
            "_tier2_summarize: summarized %d messages into %d chars, kept %d recent",
            len(older),
            len(summary_text),
            len(recent),
        )
        return [summary_msg, resume_msg, *recent]
