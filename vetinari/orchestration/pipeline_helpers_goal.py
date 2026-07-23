"""Goal enrichment, memory lookup, analysis, and clarification helpers."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from vetinari.types import MemoryType

logger = logging.getLogger(__name__)

PLANNING_MEMORY_ENTRY_TYPES = (
    MemoryType.DECISION,
    MemoryType.PATTERN,
    MemoryType.WARNING,
    MemoryType.SOLUTION,
)
MAX_PLANNING_MEMORY_QUERY_CHARS = 1000


class PipelineGoalServicesMixin:
    """Goal analysis and planning-context helper methods."""

    if TYPE_CHECKING:
        _agents: Any

    @staticmethod
    def _enrich_goal(goal: str, context: dict[str, Any]) -> str:
        """Append intake-form context fields to the goal text before planning.

        Args:
            goal: Raw user goal string.
            context: Pipeline context dict possibly containing ``required_features``,
                ``things_to_avoid``, ``tech_stack``, and ``priority``.

        Returns:
            Enriched goal string with structured context appended.
        """
        enriched = goal
        if context.get("required_features"):
            enriched += "\n\nRequired features:\n" + "\n".join(f"- {f}" for f in context["required_features"])
        if context.get("things_to_avoid"):
            enriched += "\n\nDo NOT include:\n" + "\n".join(f"- {a}" for a in context["things_to_avoid"])
        if context.get("tech_stack"):
            enriched += f"\n\nTech stack: {context['tech_stack']}"
        if context.get("priority"):
            enriched += f"\n\nPriority: {context['priority']}"
        return enriched

    @staticmethod
    def _retrieve_memory_for_planning(goal: str) -> list[dict[str, Any]]:
        """Query long-term memory for entries relevant to the current goal.

        Searches for decision, pattern, warning, and solution entries that
        might inform plan generation - avoiding past mistakes and reusing
        proven approaches.

        Args:
            goal: The enriched goal text to search against.

        Returns:
            List of memory entry summaries with type, content, and timestamp.
            Empty list if memory store is unavailable or no matches found.
        """
        import vetinari.memory.unified as unified_memory

        try:
            store = unified_memory.get_unified_memory_store()
            query = goal[:MAX_PLANNING_MEMORY_QUERY_CHARS]
            results = store.search(
                query,
                entry_types=[entry_type.value for entry_type in PLANNING_MEMORY_ENTRY_TYPES],
                limit=5,
            )
            return [
                {
                    "type": entry.entry_type.value if hasattr(entry.entry_type, "value") else str(entry.entry_type),
                    "content": entry.summary or entry.content[:300],
                    "timestamp": str(entry.timestamp),
                    "memory_id": getattr(entry, "id", None),
                    "source": getattr(entry, "provenance", None) or getattr(entry, "source", None),
                    "quality_score": getattr(entry, "quality_score", None),
                    "importance": getattr(entry, "importance", None),
                }
                for entry in results
            ]
        except Exception:
            logger.warning("Memory store unavailable for planning enrichment", exc_info=True)
            return []

    @staticmethod
    def _analyze_input(goal: str, constraints: dict[str, Any]) -> dict[str, Any]:
        """Classify the input goal and estimate complexity.

        Delegates to classify_goal_detailed() for LLM-backed classification
        with confidence scoring. Falls back to keyword matching as an explicit
        degraded state when the classifier is unavailable.

        Args:
            goal: Enriched goal text.
            constraints: Pipeline constraints dict (currently unused but
                kept for future rule-based analysis).

        Returns:
            Dict with keys ``goal``, ``estimated_complexity``, ``domain``,
            ``needs_research``, ``needs_code``, ``needs_ui``,
            ``classification_confidence``, ``classification_source``,
            ``cross_cutting``.
        """
        result: dict[str, Any] = {
            "goal": goal,
            "estimated_complexity": "medium",
            "domain": "general",
            "goal_type": "general",
            "needs_research": False,
            "needs_code": False,
            "needs_ui": False,
        }
        # Use confidence-gated classification (LLM when available, keyword fallback)
        try:
            import vetinari.orchestration.request_routing as request_routing

            classification = request_routing.classify_goal_detailed(goal)
            category = classification.get("category", "general")
            result["estimated_complexity"] = classification.get("complexity", "medium")
            result["classification_confidence"] = classification.get("confidence", 0.3)
            result["classification_source"] = classification.get("source", "keyword")
            result["cross_cutting"] = classification.get("cross_cutting", [])
            result["goal_type"] = category

            # Map category to domain flags
            result["needs_code"] = category in ("code", "devops", "git")
            result["needs_research"] = category in ("research", "data")
            result["needs_ui"] = category in ("ui", "image")
            result["domain"] = (
                "coding" if result["needs_code"] else "research" if result["needs_research"] else "general"
            )
        except Exception:
            logger.warning("Goal classification unavailable - using keyword fallback for pipeline analysis")
            # Minimal keyword fallback (degraded state)
            g = goal.lower()
            result["needs_code"] = any(k in g for k in ["code", "implement", "build", "create", "program"])
            result["needs_research"] = any(k in g for k in ["research", "analyze", "investigate", "study"])
            result["needs_ui"] = any(k in g for k in ["ui", "frontend", "interface", "dashboard"])
            result["domain"] = (
                "coding" if result["needs_code"] else "research" if result["needs_research"] else "general"
            )
            # Set goal_type based on keyword fallback
            result["goal_type"] = (
                "code" if result["needs_code"] else "research" if result["needs_research"] else "general"
            )
            word_count = len(goal.split())
            result["estimated_complexity"] = "simple" if word_count < 10 else "complex" if word_count > 30 else "medium"
            result["classification_confidence"] = 0.2  # Explicit degraded-state confidence
            result["classification_source"] = "keyword_fallback"
        return result

    def _run_clarification(self, goal: str, context: dict[str, Any]) -> dict[str, Any] | None:
        """Run ForemanAgent clarification mode to detect ambiguity in the goal.

        Looks up the FOREMAN agent from the agent cache or AgentGraph registry,
        runs it in ``"clarify"`` mode, and returns the result dict if the agent
        is available and the execution succeeds.

        Args:
            goal: The user's goal string.
            context: The pipeline context.

        Returns:
            Clarify result dict, or None if clarification is unavailable.
        """
        try:
            import vetinari.orchestration.agent_graph as agent_graph_module
            from vetinari.agents.contracts import AgentTask
            from vetinari.types import AgentType

            planner = None
            if hasattr(self, "_agents"):
                # _agents dict is keyed by string ("FOREMAN"), not AgentType enum
                planner = self._agents.get(AgentType.FOREMAN.value)
            if planner is None:
                # Try AgentGraph registry
                try:
                    ag = agent_graph_module.get_agent_graph()
                    planner = ag._agents.get(AgentType.FOREMAN.value)
                except Exception:
                    logger.warning("Planner lookup failed", exc_info=True)
            if planner is None:
                logger.debug("[Pipeline] No planner available for clarification")
                return None

            clarify_task = AgentTask(
                task_id="clarify-intake",
                agent_type=AgentType.FOREMAN,
                description=f"Check if this goal needs clarification: {goal}",
                prompt=goal,
                context={"goal": goal, "existing_context": context, "mode": "clarify"},
            )
            result = planner.execute(clarify_task)
            if result.success and isinstance(result.output, dict):
                return result.output
            return None
        except Exception as e:
            logger.warning("[Pipeline] Clarification failed: %s", e)
            return None
