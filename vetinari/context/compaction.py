"""Three-tier context compaction — graduated strategies to fit within model context limits.

Tier 1 (cheapest): Extract structured session state, discard raw messages.
Tier 2 (moderate): Summarize oldest messages, preserve recent N verbatim.
Tier 3 (aggressive): Head+tail truncation with sliding window.

This is the core compaction engine used by the context budget tracker.
Each tier is tried in order; the first one that brings token usage under
the target is used. If all three tiers succeed, the cheapest is preferred.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum

from vetinari.context.compaction_tiers import CompactionTierMixin
from vetinari.context.session_state import SessionState
from vetinari.context.window_manager import WindowConversationMessage

logger = logging.getLogger(__name__)


# ── Constants ──────────────────────────────────────────────────────────

# Sentinel role used for injected state-summary messages
_ROLE_SYSTEM = "system"

# Prefix markers so downstream code can identify compacted messages
_STATE_SUMMARY_PREFIX = "[Session state summary]\n"
_HISTORY_SUMMARY_PREFIX = "[Conversation history summary]\n"
_TRUNCATION_PREFIX = "[Context truncated — head+tail window]\n"

# Resume instruction template injected as the first system message after compaction
_RESUME_TEMPLATE = (
    "You are resuming a conversation. Here is what happened so far:\n\n"
    "{summary}\n\n"
    "Continue from where the conversation left off."
)


# ── CompactionTier enum ────────────────────────────────────────────────


class CompactionTier(Enum):
    """Which compaction strategy was applied."""

    STATE_EXTRACTION = "state_extraction"  # Tier 1: cheapest — pattern-based state extraction
    SUMMARY = "summary"  # Tier 2: moderate — bullet-point history summary
    TRUNCATION = "truncation"  # Tier 3: aggressive — head+tail window


# ── CompactionResult ───────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class CompactionResult:
    """Immutable record of a single compaction operation.

    Captures the before/after metrics for the compaction so callers can
    log savings, update budget trackers, and decide whether to escalate
    to the next tier.

    Attributes:
        tier: Which compaction strategy was applied.
        messages_before: Number of messages in the list before compaction.
        messages_after: Number of messages in the list after compaction.
        tokens_before: Estimated token total before compaction.
        tokens_after: Estimated token total after compaction.
        tokens_saved: Difference (tokens_before - tokens_after); always >= 0.
        state_extracted: Structured session state captured during Tier 1,
            or None when Tier 1 was not used.
    """

    tier: CompactionTier
    messages_before: int
    messages_after: int
    tokens_before: int
    tokens_after: int
    tokens_saved: int
    state_extracted: SessionState | None = None

    def __repr__(self) -> str:
        return (
            f"CompactionResult(tier={self.tier.value!r}, "
            f"msgs={self.messages_before}->{self.messages_after}, "
            f"tokens={self.tokens_before}->{self.tokens_after}, "
            f"saved={self.tokens_saved})"
        )


# ── ContextCompactor ───────────────────────────────────────────────────


class ContextCompactor(CompactionTierMixin):
    """Three-tier compaction engine for conversation message lists.

    Tries each compaction tier in ascending order of aggressiveness and
    stops at the first tier that brings the message list under
    ``target_tokens``. If the list is already under target, returns it
    unchanged with a Tier 1 result showing zero savings.

    Tier 1 — State extraction (cheapest):
        Uses ``SessionStateExtractor`` to capture key decisions and
        outputs from the older messages, then replaces those messages
        with a compact state-summary system message. No LLM call needed.

    Tier 2 — Bullet-point summarization (moderate):
        Preserves the most recent ``preserve_recent`` messages verbatim.
        Older messages are condensed into a short bullet-point summary
        built from their content (no LLM call).

    Tier 3 — Head+tail truncation (aggressive):
        Keeps the first ``head_tokens`` worth of messages and the last
        ``tail_tokens`` worth, dropping everything in between. A visible
        marker message is inserted at the truncation point.

    Thread safety: each ``compact()`` call is stateless with respect to
    the compactor instance — no shared mutable state is written during a
    call, so concurrent calls are safe without locking.
    """

    def __init__(
        self,
        preserve_recent: int = 4,
        head_tokens: int = 500,
        tail_tokens: int = 1500,
    ) -> None:
        """Configure compaction parameters.

        Args:
            preserve_recent: Number of most-recent messages to keep verbatim
                during Tier 2 summarization. Must be >= 1.
            head_tokens: Token budget for the head window in Tier 3 truncation.
                Controls how much early context is preserved.
            tail_tokens: Token budget for the tail window in Tier 3 truncation.
                Controls how much recent context is preserved. Should be larger
                than head_tokens since recent messages carry more value.
        """
        if preserve_recent < 1:
            raise ValueError(f"preserve_recent must be >= 1, got {preserve_recent}")
        if head_tokens < 1:
            raise ValueError(f"head_tokens must be >= 1, got {head_tokens}")
        if tail_tokens < 1:
            raise ValueError(f"tail_tokens must be >= 1, got {tail_tokens}")

        self._preserve_recent = preserve_recent
        self._head_tokens = head_tokens
        self._tail_tokens = tail_tokens

    # ── Public API ─────────────────────────────────────────────────────

    def compact(
        self,
        messages: list[WindowConversationMessage],
        target_tokens: int,
        task_id: str = "",
        stage: str = "",
        model_id: str = "",
    ) -> tuple[list[WindowConversationMessage], CompactionResult]:
        """Compact a message list to fit within target_tokens.

        Tries tiers in order (Tier 1 -> Tier 2 -> Tier 3) and returns
        as soon as the result fits under the target. If the list is already
        within budget, it is returned unchanged.

        Args:
            messages: Current conversation message list. Not mutated.
            target_tokens: Maximum token count the result must not exceed.
            task_id: Task identifier forwarded to ``SessionStateExtractor``
                for structured state extraction (Tier 1).
            stage: Pipeline stage name forwarded to ``SessionStateExtractor``.
            model_id: Model identifier forwarded to ``SessionStateExtractor``.

        Returns:
            A two-tuple of (compacted_messages, CompactionResult).
        """
        tokens_before = sum(m.token_count for m in messages)
        msgs_before = len(messages)

        if tokens_before <= target_tokens:
            # Already within budget — nothing to do.
            logger.debug(
                "compact: %d tokens already <= target %d — skipping",
                tokens_before,
                target_tokens,
            )
            return list(messages), self._result(
                CompactionTier.STATE_EXTRACTION, msgs_before, msgs_before, tokens_before
            )

        # ── Tier 1: state extraction ───────────────────────────────────
        t1_msgs, state = self._tier1_state_extraction(messages, task_id, stage, model_id)
        t1_tokens = sum(m.token_count for m in t1_msgs)
        if t1_tokens <= target_tokens:
            logger.info(
                "compact: Tier 1 state extraction succeeded — %d -> %d tokens (saved %d)",
                tokens_before,
                t1_tokens,
                tokens_before - t1_tokens,
            )
            result = self._result(
                CompactionTier.STATE_EXTRACTION,
                msgs_before,
                len(t1_msgs),
                tokens_before,
                t1_tokens,
                state,
            )
            return t1_msgs, result

        # ── Tier 2: summarize oldest messages ─────────────────────────
        t2_msgs = self._tier2_summarize(messages)
        t2_tokens = sum(m.token_count for m in t2_msgs)
        if t2_tokens <= target_tokens:
            logger.info(
                "compact: Tier 2 summarization succeeded — %d -> %d tokens (saved %d)",
                tokens_before,
                t2_tokens,
                tokens_before - t2_tokens,
            )
            result = self._result(CompactionTier.SUMMARY, msgs_before, len(t2_msgs), tokens_before, t2_tokens)
            return t2_msgs, result

        t3_msgs = self._tier3_truncate(messages, target_tokens)
        t3_tokens = sum(m.token_count for m in t3_msgs)
        logger.info(
            "compact: Tier 3 truncation applied — %d -> %d tokens (saved %d)",
            tokens_before,
            t3_tokens,
            tokens_before - t3_tokens,
        )
        result = self._result(CompactionTier.TRUNCATION, msgs_before, len(t3_msgs), tokens_before, t3_tokens)
        return t3_msgs, result

    @staticmethod
    def _result(
        tier: CompactionTier,
        messages_before: int,
        messages_after: int,
        tokens_before: int,
        tokens_after: int | None = None,
        state_extracted: SessionState | None = None,
    ) -> CompactionResult:
        after = tokens_before if tokens_after is None else tokens_after
        return CompactionResult(
            tier=tier,
            messages_before=messages_before,
            messages_after=messages_after,
            tokens_before=tokens_before,
            tokens_after=after,
            tokens_saved=max(0, tokens_before - after),
            state_extracted=state_extracted,
        )

    # ── Private tier implementations ───────────────────────────────────

    def _tier3_truncate(
        self,
        messages: list[WindowConversationMessage],
        target_tokens: int,
    ) -> list[WindowConversationMessage]:
        """Tier 3: keep first head_tokens and last tail_tokens, drop the middle.

        Greedily fills the head window from the oldest messages, then fills
        the tail window from the newest messages. A single marker system
        message is inserted at the boundary to make the gap visible.

        Args:
            messages: Full message list to truncate.
            target_tokens: Token budget hint used for logging; truncation
                always applies head+tail windows regardless of this value.

        Returns:
            Truncated message list with a gap marker inserted.
        """
        head: list[WindowConversationMessage] = []
        head_used = 0
        for msg in messages:
            if head_used + msg.token_count > self._head_tokens:
                break
            head.append(msg)
            head_used += msg.token_count

        tail: list[WindowConversationMessage] = []
        tail_used = 0
        for msg in reversed(messages):
            if tail_used + msg.token_count > self._tail_tokens:
                break
            tail.insert(0, msg)
            tail_used += msg.token_count

        # Avoid duplicating messages that appear in both windows.
        head_ids = {id(m) for m in head}
        tail = [m for m in tail if id(m) not in head_ids]

        gap_msg = WindowConversationMessage(
            role=_ROLE_SYSTEM,
            content=f"{_TRUNCATION_PREFIX}Messages between the head and tail windows have been dropped.",
            is_compressed=True,
            metadata={
                "compaction_tier": CompactionTier.TRUNCATION.value,
                "head_messages": len(head),
                "tail_messages": len(tail),
                "target_tokens": target_tokens,
            },
        )

        result = [*head, gap_msg, *tail]
        logger.debug(
            "_tier3_truncate: head=%d msgs (%d tokens), tail=%d msgs (%d tokens), gap marker inserted",
            len(head),
            head_used,
            len(tail),
            tail_used,
        )
        return result

    @staticmethod
    def _simple_summarize(messages: list[WindowConversationMessage]) -> str:
        """Build a bullet-point summary from message content without an LLM call.

        Each message contributes one bullet point. Long message content is
        truncated to keep the summary compact. System messages that are
        already compressed summaries are reproduced verbatim (they are
        already compact).

        Args:
            messages: The messages to summarize.

        Returns:
            A multi-line bullet-point string summarizing the messages.
        """
        _MAX_BULLET_CHARS = 200  # Keep each bullet short
        bullets: list[str] = []

        for msg in messages:
            content = msg.content.strip()
            if not content:
                continue
            # Already-compressed messages are already terse — keep as-is.
            if msg.is_compressed:
                bullets.append(f"- [summary] {content[:_MAX_BULLET_CHARS]}")
                continue
            truncated = content[:_MAX_BULLET_CHARS]
            if len(content) > _MAX_BULLET_CHARS:
                truncated += "..."
            bullets.append(f"- [{msg.role}] {truncated}")

        if not bullets:
            return "(no messages to summarize)"
        return "\n".join(bullets)

    @staticmethod
    def _build_resume_instruction(
        state: SessionState | None,
        summary: str = "",
    ) -> str:
        """Build a resume system message for local models.

        Local models do not have memory of prior context after a compaction
        event. This instruction bridges the gap by telling the model what
        happened and asking it to continue seamlessly.

        When structured ``state`` is available, key decisions and outputs
        are appended after the general summary for extra clarity.

        Args:
            state: Extracted session state from Tier 1, or None.
            summary: Plain-text summary string (from Tier 1 or Tier 2).

        Returns:
            A system message string suitable for injection as the first
            message in the compacted context.
        """
        parts: list[str] = [summary or "(context was compacted)"]

        if state is not None:
            if state.key_decisions:
                parts.append("\nKey decisions made:")
                parts.extend(f"  - {d}" for d in state.key_decisions[:5])
            if state.outputs_produced:
                parts.append("\nOutputs/artifacts produced:")
                parts.extend(f"  - {o}" for o in state.outputs_produced[:5])
            if state.quality_scores:
                score_str = ", ".join(f"{k}={v:.2f}" for k, v in list(state.quality_scores.items())[:3])
                parts.append(f"\nQuality scores: {score_str}")

        combined = "\n".join(parts)
        return _RESUME_TEMPLATE.format(summary=combined)

    # ── Internal helpers ───────────────────────────────────────────────

    @staticmethod
    def _format_state_as_summary(state: SessionState) -> str:
        """Render a SessionState as a concise human-readable summary string.

        Used to produce the content of the state-summary system message in
        Tier 1 compaction. All fields with content are included; empty fields
        are omitted to keep the summary short.

        Args:
            state: The extracted session state to render.

        Returns:
            A multi-line summary string.
        """
        lines: list[str] = [
            f"Task: {state.task_id or '(unknown)'}",
            f"Stage: {state.stage or '(unknown)'}",
        ]
        if state.model_used:
            lines.append(f"Model: {state.model_used}")
        if state.key_decisions:
            lines.append("Decisions:")
            lines.extend(f"  - {d}" for d in state.key_decisions[:5])
        if state.outputs_produced:
            lines.append("Outputs:")
            lines.extend(f"  - {o}" for o in state.outputs_produced[:5])
        if state.quality_scores:
            score_str = ", ".join(f"{k}={v:.2f}" for k, v in list(state.quality_scores.items())[:3])
            lines.append(f"Quality: {score_str}")
        return "\n".join(lines)


# ── Stale tool result detection ───────────────────────────────────────

# Cold cache threshold: tool results older than this are considered stale
_STALE_THRESHOLD_SECONDS = 3600  # 1 hour


def should_clear_stale_tool_results(last_stage_time: datetime | None) -> bool:
    """Determine whether old tool results should be cleared due to cold cache.

    When more than _STALE_THRESHOLD_SECONDS have passed since the last pipeline
    stage completed, tool results are considered stale (cold cache). Clearing
    them reclaims context window budget.

    Args:
        last_stage_time: Timestamp of the last completed pipeline stage, or None
            if no stage has completed yet.

    Returns:
        True if the gap exceeds 1 hour and stale results should be cleared.
    """
    if last_stage_time is None:
        return False
    now = datetime.now(timezone.utc)
    # Handle naive datetimes by assuming UTC
    if last_stage_time.tzinfo is None:
        last_stage_time = last_stage_time.replace(tzinfo=timezone.utc)
    elapsed = (now - last_stage_time).total_seconds()
    return elapsed >= _STALE_THRESHOLD_SECONDS


# ── Module singleton ───────────────────────────────────────────────────

# Who writes: get_compactor() (lazy initialisation on first call)
# Who reads: any module that needs compaction (context budget tracker, tests)
# Lifecycle: created once per process; ContextCompactor is stateless between calls
# Lock: double-checked locking protects simultaneous first-callers
_compactor: ContextCompactor | None = None
_compactor_lock = threading.Lock()


def get_compactor() -> ContextCompactor:
    """Return the process-wide singleton ContextCompactor.

    Uses lazy initialisation with double-checked locking so the compactor
    is created exactly once even under concurrent access.

    Returns:
        The singleton ContextCompactor instance.
    """
    global _compactor
    if _compactor is None:
        with _compactor_lock:
            if _compactor is None:
                _compactor = ContextCompactor()
    return _compactor


__all__ = [
    "CompactionResult",
    "CompactionTier",
    "ContextCompactor",
    "get_compactor",
    "should_clear_stale_tool_results",
]
