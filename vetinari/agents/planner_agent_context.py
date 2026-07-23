"""Context management mixin for ForemanAgent."""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any, ClassVar

from vetinari.agents.contracts import AgentResult, AgentTask
from vetinari.constants import TRUNCATE_CONTENT_ANALYSIS, TRUNCATE_CONTEXT
from vetinari.security.redaction import redact_text, redact_value

_CONTEXT_TEXT_KEYS = frozenset({
    "content",
    "history",
    "input",
    "message",
    "messages",
    "output",
    "prompt",
    "query",
    "raw_prompt",
    "response",
    "result",
    "text",
    "transcript",
    "transcript_text",
})


def _planner_module() -> Any:
    """Return the public planner_agent module for compatibility-patched symbols."""
    from vetinari.agents import planner_agent

    return planner_agent


def _redacted_text_receipt(value: str) -> str:
    digest = hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()[:12]
    return f"[REDACTED_CONTEXT_TEXT len={len(value)} sha256={digest}]"


def _minimize_context_value(value: Any, *, key_hint: str = "") -> Any:
    key = key_hint.lower()
    if isinstance(value, str):
        redacted = redact_text(value)
        if key in _CONTEXT_TEXT_KEYS or redacted != value:
            return _redacted_text_receipt(value)
        return redacted
    if isinstance(value, dict):
        return {
            item_key: _minimize_context_value(item, key_hint=str(item_key))
            for item_key, item in redact_value(value).items()
        }
    if isinstance(value, tuple):
        return tuple(_minimize_context_value(item, key_hint=key) for item in value)
    if isinstance(value, list):
        return [_minimize_context_value(item, key_hint=key) for item in value]
    return value


def _redacted_json(value: Any, *, limit: int) -> str:
    return json.dumps(_minimize_context_value(value), indent=2)[:limit]


def _load_memory_entries(
    entries_or_session_id: list[dict] | str | None = None,
    project_id: str = "",
    *,
    session_id: str | None = None,
) -> list[dict]:
    """Load memory entries and fail closed against cross-session leakage."""
    explicit_entries = entries_or_session_id if isinstance(entries_or_session_id, list) else None
    active_session_id = session_id if session_id is not None else (entries_or_session_id or "")
    entries = list(explicit_entries or [])
    if explicit_entries is None:
        try:
            from vetinari.memory.unified import get_unified_memory_store

            store = get_unified_memory_store()
            if hasattr(store, "search"):
                results = store.search("", limit=50)
                entries.extend(result.to_dict() if hasattr(result, "to_dict") else result for result in results or [])
        except Exception:
            _planner_module().logger.warning("Failed to load memory entries from unified memory store", exc_info=True)
    filtered = []
    for entry in entries:
        if active_session_id and entry.get("session_id") != active_session_id:
            continue
        if project_id and entry.get("project_id") != project_id:
            continue
        filtered.append(entry)
    return filtered


class ForemanContextMixin:
    """Consolidate, summarise, prune, and extract behavior for ForemanAgent."""

    _MAX_ENTRIES_FOR_CONSOLIDATION: ClassVar[int] = 50

    if TYPE_CHECKING:
        _max_context_tokens: int

        def _infer_json(self, prompt: str, **kwargs: Any) -> dict[str, Any] | None: ...

    def _execute_consolidate(self, task: AgentTask) -> AgentResult:
        """Consolidate session memory entries into compact knowledge.

        Args:
            task: Agent task containing session and project identifiers.

        Returns:
            AgentResult containing consolidated memory output.
        """
        ctx = task.context
        session_id = ctx.get("session_id", "")
        project_id = ctx.get("project_id", "")
        entries = self._load_memory_entries(session_id, project_id)

        if not entries:
            return AgentResult(
                success=True,
                output=self._fallback_consolidation(task, []),
                metadata={"operation": "consolidate", "entries_processed": 0},
            )

        entries_text = _redacted_json(entries[: self._MAX_ENTRIES_FOR_CONSOLIDATION], limit=TRUNCATE_CONTEXT)
        prompt = (
            f"Consolidate the following {len(entries)} memory entries. "
            f"Extract key knowledge, identify patterns, create concise summary.\n\n"
            f"## Entries\n{entries_text}\n\n"
            "## Output (JSON)\n"
            '{"consolidated_summary": "...", "key_knowledge": [{"fact": "...", "confidence": 0.9}], '
            '"patterns_identified": [...], "entries_processed": ' + str(len(entries)) + "}"
        )
        result = self._infer_json(prompt, fallback=self._fallback_consolidation(task, entries))
        if result and isinstance(result, dict):
            result.setdefault("entries_processed", len(entries))
            return AgentResult(
                success=True,
                output=result,
                metadata={"operation": "consolidate", "entries_processed": len(entries)},
            )
        fallback = self._fallback_consolidation(task, entries)
        return AgentResult(success=True, output=fallback, metadata={"operation": "consolidate"})

    def _execute_summarise(self, task: AgentTask) -> AgentResult:
        """Summarise a session history.

        Args:
            task: Agent task containing history or memory lookup context.

        Returns:
            AgentResult containing a session summary.
        """
        ctx = task.context
        history = ctx.get("history", []) or ctx.get("messages", [])
        if not history:
            history = self._load_memory_entries(ctx.get("session_id", ""), ctx.get("project_id", ""))

        prompt = (
            f"Summarise {len(history)} session entries for an AI orchestration system.\n\n"
            f"## History\n{_redacted_json(history[:30], limit=TRUNCATE_CONTENT_ANALYSIS)}\n\n"
            "## Output (JSON)\n"
            '{"session_summary": "...", "goals_achieved": [...], "next_steps": [...], '
            '"entries_processed": ' + str(len(history)) + "}"
        )
        result = self._infer_json(prompt, fallback=self._fallback_consolidation(task, history))
        if result and isinstance(result, dict):
            result.setdefault("entries_processed", len(history))
            return AgentResult(success=True, output=result, metadata={"operation": "summarise"})
        return AgentResult(success=True, output=self._fallback_consolidation(task, history))

    def _execute_prune(self, task: AgentTask) -> AgentResult:
        """Prune context entries to a target token budget.

        Args:
            task: Agent task containing entries and token budget.

        Returns:
            AgentResult containing retained and stale entry information.
        """
        ctx = task.context or {}
        entries = ctx.get("entries", [])
        max_tokens = ctx.get("max_tokens", self._max_context_tokens)

        if not entries:
            return AgentResult(
                success=True,
                output={
                    "consolidated_summary": "No entries to prune",
                    "pruned_count": 0,
                    "entries_processed": 0,
                },
            )

        prompt = (
            f"Prune context to fit within {max_tokens} tokens. "
            f"Keep highest relevance entries.\n\n"
            f"## Entries ({len(entries)})\n{_redacted_json(entries[:40], limit=TRUNCATE_CONTENT_ANALYSIS)}\n\n"
            "## Output (JSON)\n"
            '{"entries_to_retain": [...], "stale_entries": [...], "pruned_count": 0}'
        )
        result = self._infer_json(prompt, fallback={"pruned_count": 0, "entries_processed": len(entries)})
        if result and isinstance(result, dict):
            return AgentResult(success=True, output=result, metadata={"operation": "prune"})
        return AgentResult(success=True, output={"pruned_count": 0})

    def _execute_extract(self, task: AgentTask) -> AgentResult:
        """Extract structured knowledge from context text.

        Args:
            task: Agent task containing text to extract from.

        Returns:
            AgentResult containing knowledge facts and discovered entities.
        """
        text = task.context.get("text", "") or task.description or ""
        prompt = (
            f"Extract structured knowledge from:\n{_redacted_text_receipt(str(text))}\n\n"
            "## Output (JSON)\n"
            '{"key_knowledge": [{"fact": "...", "confidence": 0.9}], '
            '"entities_discovered": [{"name": "...", "type": "..."}]}'
        )
        result = self._infer_json(prompt, fallback={"key_knowledge": [], "entities_discovered": []})
        if result and isinstance(result, dict):
            return AgentResult(success=True, output=result, metadata={"operation": "extract"})
        return AgentResult(success=True, output={"key_knowledge": []})

    @staticmethod
    def _load_memory_entries(
        entries_or_session_id: list[dict] | str | None = None,
        project_id: str = "",
        *,
        session_id: str | None = None,
    ) -> list[dict]:
        """Load memory entries for a session and project.

        Args:
            entries_or_session_id: Existing entries to filter, or the session
                identifier for store-backed lookup.
            project_id: Project identifier for store filtering.
            session_id: Session identifier when filtering explicit entries.

        Returns:
            List of memory entry dictionaries.
        """
        return _load_memory_entries(entries_or_session_id, project_id, session_id=session_id)

    @staticmethod
    def _fallback_consolidation(task: AgentTask, entries: list) -> dict[str, Any]:
        """Build deterministic fallback output when LLM consolidation is unavailable.

        Args:
            task: Agent task being processed.
            entries: Entries considered for consolidation.

        Returns:
            Fallback consolidation dictionary.
        """
        return {
            "consolidated_summary": f"Context consolidation for: {_redacted_text_receipt(task.description or 'session')}",
            "session_summary": f"Processed {len(entries)} entries. LLM unavailable.",
            "key_knowledge": [],
            "entries_processed": len(entries),
            "retrieval_recommendations": [{"query_type": "semantic", "strategy": "hybrid"}],
        }
