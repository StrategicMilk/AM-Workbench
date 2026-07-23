"""Post-compaction context restoration — re-injects critical context after any compaction event.

When context is compacted to fit within the model's window, essential information
(agent role, task description, recent output) must be restored. This module manages
per-type token budgets to ensure the most important context is always present.

Injection order: agent instructions first, then task + plan, then recent pipeline
output, then key memories. Each category is independently truncated to its budget
so that one large item cannot crowd out others.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from vetinari.context.session_state import SessionState
from vetinari.context.window_manager import WindowConversationMessage, count_tokens

logger = logging.getLogger(__name__)


# ── RestorationBudget ──────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class RestorationBudget:
    """Per-category token budgets applied during context restoration.

    Each field caps how many tokens a restored category may consume.
    The total is a property so callers can quickly check whether a given
    context window can accommodate a full restoration pass.

    Attributes:
        agent_instructions: Max tokens for the agent's role / system prompt.
        task_and_plan: Max tokens for the current task description and plan steps.
        recent_output: Max tokens for the most recent Worker or Inspector output.
        memories: Max tokens for key memory snippets injected from long-term store.
    """

    agent_instructions: int = 1000  # tokens for agent role/instructions
    task_and_plan: int = 2000  # tokens for current task + plan
    recent_output: int = 2000  # tokens for most recent pipeline output
    memories: int = 1500  # tokens for key memories/context

    @property
    def total(self) -> int:
        """Sum of all per-category budgets — the maximum tokens a full restoration injects."""
        return self.agent_instructions + self.task_and_plan + self.recent_output + self.memories

    def __repr__(self) -> str:
        return (
            "RestorationBudget("
            f"agent_instructions={self.agent_instructions}, "
            f"task_and_plan={self.task_and_plan}, "
            f"recent_output={self.recent_output}, memories={self.memories})"
        )


# ── RestorationContext ─────────────────────────────────────────────────


@dataclass
class RestorationContext:
    """Everything needed to rebuild the model's working context after compaction.

    Callers populate this before calling ``ContextRestorer.restore()``. Fields
    default to empty so callers can set only what they have — the restorer skips
    any category that contributes zero content.

    Attributes:
        agent_instructions: The agent's system prompt or role description.
        task_description: The current task being worked on (one-liner or paragraph).
        plan_summary: Current plan steps as a text block (e.g. numbered list).
        recent_output: The most recent Worker or Inspector output to restore.
        memories: Relevant memory snippets retrieved from long-term store.
        session_state: Optional structured snapshot from the compaction tier-1 extractor.
    """

    agent_instructions: str = ""  # the agent's system prompt / role
    task_description: str = ""  # current task being worked on
    plan_summary: str = ""  # current plan steps
    recent_output: str = ""  # most recent Worker/Inspector output
    memories: list[str] = field(default_factory=list)  # relevant memory snippets
    session_state: SessionState | None = None  # from compaction tier 1

    def __repr__(self) -> str:
        return (
            "RestorationContext("
            f"agent_instructions={bool(self.agent_instructions)}, "
            f"task_description={bool(self.task_description)}, "
            f"plan_summary={bool(self.plan_summary)}, "
            f"recent_output={bool(self.recent_output)}, "
            f"memories={len(self.memories)}, "
            f"session_state={self.session_state is not None})"
        )


# ── RestorationResult ──────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class RestorationResult:
    """Describes what the restorer did during a single ``restore()`` call.

    Attributes:
        messages_injected: Total number of system messages prepended to the list.
        tokens_injected: Total tokens consumed by the injected messages.
        budget_used: Mapping of category name to tokens actually used.
        truncated: Category names whose content was cut to fit the budget.
    """

    messages_injected: int  # number of system messages prepended
    tokens_injected: int  # total tokens across all injected messages
    budget_used: dict[str, int]  # category -> tokens actually used
    truncated: list[str]  # categories that were truncated to fit budget

    def __repr__(self) -> str:
        return (
            "RestorationResult("
            f"messages_injected={self.messages_injected}, "
            f"tokens_injected={self.tokens_injected}, "
            f"truncated={self.truncated!r})"
        )


# ── ContextRestorer ────────────────────────────────────────────────────


class ContextRestorer:
    """Re-injects critical context into the message list after a compaction event.

    Each category (agent instructions, task/plan, recent output, memories) is
    truncated independently so that one large item cannot crowd out the others.
    The injected messages are prepended to the existing list so they appear before
    any surviving conversation history.

    Intended to be used as a singleton via ``get_restorer()``.
    """

    def __init__(self, budget: RestorationBudget | None = None) -> None:
        """Configure per-category token budgets.

        Args:
            budget: Token budgets per category. Defaults to ``RestorationBudget()``
                which uses the standard allocations defined in the design doc.
        """
        self._budget = budget if budget is not None else RestorationBudget()

    # ── Public API ─────────────────────────────────────────────────────

    def restore(
        self,
        messages: list[WindowConversationMessage],
        context: RestorationContext,
    ) -> tuple[list[WindowConversationMessage], RestorationResult]:
        """Prepend restored context messages to the existing message list.

        Builds restoration messages from *context*, truncating each category to
        its configured budget, then returns a new list with those messages at
        the front. The original *messages* list is not mutated.

        Args:
            messages: Existing conversation messages (post-compaction).
            context: The context to restore, containing agent instructions,
                task/plan, recent output, and memory snippets.

        Returns:
            A two-tuple of ``(updated_messages, result)`` where
            ``updated_messages`` has the restoration messages prepended and
            ``result`` describes what was injected.
        """
        restoration_messages = self._build_restoration_messages(context)

        budget_used: dict[str, int] = {}
        truncated: list[str] = []

        for msg in restoration_messages:
            category = msg.metadata.get("restoration_category", "unknown")
            tokens = msg.token_count
            budget_used[category] = tokens
            if msg.metadata.get("was_truncated", False):
                truncated.append(category)

        total_tokens = sum(budget_used.values())
        updated = [*restoration_messages, *messages]

        result = RestorationResult(
            messages_injected=len(restoration_messages),
            tokens_injected=total_tokens,
            budget_used=budget_used,
            truncated=truncated,
        )

        logger.info(
            "Context restoration: injected %d messages (%d tokens), truncated=%s",
            result.messages_injected,
            result.tokens_injected,
            truncated or "none",
        )
        return updated, result

    # ── Private helpers ────────────────────────────────────────────────

    @staticmethod
    def _truncate_to_budget(text: str, max_tokens: int) -> tuple[str, bool]:
        """Shorten *text* to fit within *max_tokens*, preserving a suffix marker.

        Truncation is word-based to avoid cutting multi-byte characters midway.
        The trimmed result always ends with a ``[...truncated...]`` marker so
        the model can tell that content was removed rather than seeing an
        abrupt cut.

        Args:
            text: The text to potentially shorten.
            max_tokens: Maximum allowed token count for the result.

        Returns:
            A two-tuple ``(result_text, was_truncated)`` where *was_truncated*
            is ``True`` when the text was shortened.
        """
        if not text:
            return text, False

        if count_tokens(text) <= max_tokens:
            return text, False

        marker = "\n[...truncated...]"
        marker_tokens = count_tokens(marker)
        # Reserve tokens for the marker itself
        target_tokens = max(0, max_tokens - marker_tokens)

        words = text.split()
        # Binary search for the largest prefix that fits
        lo, hi = 0, len(words)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if count_tokens(" ".join(words[:mid])) <= target_tokens:
                lo = mid
            else:
                hi = mid - 1

        truncated_text = " ".join(words[:lo]) + marker
        return truncated_text, True

    def _build_restoration_messages(self, context: RestorationContext) -> list[WindowConversationMessage]:
        """Assemble ordered restoration messages, each clipped to its budget.

        Builds one system message per non-empty category in priority order:
        agent instructions, task + plan combined, recent pipeline output, then
        memories. Empty categories produce no message.

        Args:
            context: The populated restoration context.

        Returns:
            Ordered list of ``ConversationMessage`` objects ready to be
            prepended to the conversation.
        """
        messages: list[WindowConversationMessage] = []

        # 1. Agent instructions — highest priority, injected first
        if context.agent_instructions.strip():
            self._append_restored_message(
                messages,
                "agent_instructions",
                "agent instructions",
                context.agent_instructions,
                self._budget.agent_instructions,
            )

        # 2. Task description + plan summary — combined into one message so
        #    the model sees them as a single coherent context block
        task_parts: list[str] = []
        if context.task_description.strip():
            task_parts.append(f"Task:\n{context.task_description}")
        if context.plan_summary.strip():
            task_parts.append(f"Plan:\n{context.plan_summary}")

        if task_parts:
            combined = "\n\n".join(task_parts)
            self._append_restored_message(
                messages, "task_and_plan", "task and plan", combined, self._budget.task_and_plan
            )

        # 3. Most recent pipeline output
        if context.recent_output.strip():
            self._append_restored_message(
                messages,
                "recent_output",
                "recent pipeline output",
                context.recent_output,
                self._budget.recent_output,
            )

        # 4. Memory snippets — injected as a single block to avoid many small messages
        non_empty_memories = [m for m in context.memories if m.strip()]
        if non_empty_memories:
            combined = "\n---\n".join(non_empty_memories)
            self._append_restored_message(
                messages,
                "memories",
                "key memories",
                combined,
                self._budget.memories,
                role="assistant",
                authority="retrieved_memory",
            )

        return messages

    def _append_restored_message(
        self,
        messages: list[WindowConversationMessage],
        category: str,
        label: str,
        text: str,
        budget: int,
        *,
        role: str = "system",
        authority: str = "restored_context",
    ) -> None:
        body, was_truncated = self._truncate_to_budget(text, budget)
        msg = WindowConversationMessage(
            role=role,
            content=f"[Restored: {label}]\n{body}",
            metadata={
                "restoration_category": category,
                "was_truncated": was_truncated,
                "authority": authority,
                "source": "context_restoration",
            },
        )
        messages.append(msg)
        logger.debug("Restoration: %s %d tokens (truncated=%s)", category, msg.token_count, was_truncated)


# ── Module-level singleton ─────────────────────────────────────────────

# Who writes: get_restorer() on first call (lazy, GIL-safe for single-process servers)
# Who reads: any module handling post-compaction recovery
# Lifecycle: created once per process; the restorer holds no mutable state between calls
# Lock: none required — RestorationBudget is frozen; restorer methods are pure
_restorer: ContextRestorer | None = None


def get_restorer() -> ContextRestorer:
    """Get or create the process-wide ``ContextRestorer`` with default budgets.

    Uses lazy initialisation — the singleton is created on the first call and
    reused thereafter. Thread-safe under the GIL for the typical single-process
    deployment.

    Returns:
        The process-wide ``ContextRestorer`` instance.
    """
    global _restorer
    if _restorer is None:
        _restorer = ContextRestorer()
    return _restorer


__all__ = [
    "ContextRestorer",
    "RestorationBudget",
    "RestorationContext",
    "RestorationResult",
    "get_restorer",
]
