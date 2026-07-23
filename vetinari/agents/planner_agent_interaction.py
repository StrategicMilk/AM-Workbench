"""Clarification and interaction mixin for ForemanAgent."""

from __future__ import annotations

import logging
import sys
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from vetinari.agents.contracts import AgentResult, AgentTask

logger = logging.getLogger(__name__)


def _planner_module() -> Any:
    """Return the public planner_agent module for compatibility-patched symbols."""
    from vetinari.agents import planner_agent

    return planner_agent


class ForemanInteractionMixin:
    """Clarify-mode behavior for ForemanAgent."""

    if TYPE_CHECKING:
        _callback: Any
        _infer_json: Any
        _interaction_mode: Any

    def _execute_clarify(self, task: AgentTask) -> AgentResult:
        """Detect goal ambiguity and collect clarifying answers.

        Args:
            task: The Foreman task carrying goal and existing context.

        Returns:
            AgentResult containing enriched context or pending questions.
        """
        goal = task.context.get("goal", task.description)
        existing_context = task.context.get("existing_context", {})
        max_questions = task.context.get("max_questions", 3)

        is_ambiguous, questions = self._detect_ambiguity(goal, existing_context)

        if not is_ambiguous or not questions:
            return AgentResult(
                success=True,
                output=existing_context,
                metadata={"questions_asked": 0, "ambiguous": False},
            )

        questions = questions[:max_questions]
        self._pending_questions = [{"question": q, "answered": False} for q in questions]

        if self._interaction_mode == "interactive":
            responses = self._interactive_prompt(questions)
        elif self._interaction_mode == "callback" and self._callback:
            responses = self._callback_prompt(goal, questions)
        else:
            return AgentResult(
                success=True,
                output={"pending_questions": questions, "needs_user_input": True, "existing_context": existing_context},
                metadata={"questions_asked": len(questions), "needs_user_input": True},
            )

        enriched = dict(existing_context)
        for q, r in zip(questions, responses):
            enriched[f"clarification_{len(enriched)}"] = {"question": q, "answer": r}
        self._gathered_context = enriched

        return AgentResult(
            success=True,
            output=enriched,
            metadata={"questions_asked": len(questions), "responses_gathered": len(responses)},
        )

    def _detect_ambiguity(self, goal: str, context: dict) -> tuple:
        """Detect whether a goal needs clarifying questions.

        Args:
            goal: The goal string to analyze.
            context: Existing context available for disambiguation.

        Returns:
            Tuple of ``(is_ambiguous, questions)``.
        """
        prompt = (
            f'Analyze this goal for ambiguity: "{goal}"\n'
            f"Context available: {list(context.keys())}\n\n"
            "Respond as JSON:\n"
            '{"is_ambiguous": true/false, "questions": ["..."], "missing_information": ["..."]}\n\n'
            "Only flag as ambiguous if critical information is missing."
        )
        result = self._infer_json(prompt)
        if result and isinstance(result, dict):
            return result.get("is_ambiguous", False), result.get("questions", [])

        questions = []
        goal_lower = goal.lower()
        if len(goal.split()) < 5:
            questions.append("Could you provide more details about what you want to accomplish?")
        if any(word in goal_lower for word in ["something", "stuff", "things", "it"]):
            questions.append("Can you be more specific about what 'it' refers to?")
        if any(word in goal_lower for word in ["build", "create", "make"]) and not any(
            word in goal_lower for word in ["python", "javascript", "web", "api", "cli"]
        ):
            questions.append("What technology stack should be used?")
        return len(questions) > 0, questions

    @staticmethod
    def _interactive_prompt(questions: list[str]) -> list[str]:
        """Collect answers for clarification questions from stdin.

        Args:
            questions: Clarifying questions to ask.

        Returns:
            List of answer strings, one per question.
        """
        responses = []
        _planner_module().logger.info("Additional context needed:")
        for i, question in enumerate(questions, 1):
            _planner_module().logger.info("%d. %s", i, question)
            try:
                response = input("   > ").strip() if sys.stdin.isatty() else sys.stdin.readline().strip()
                responses.append(response or "(no response)")
            except (EOFError, KeyboardInterrupt):
                responses.append("(skipped)")
        return responses

    def _callback_prompt(self, goal: str, questions: list[str]) -> list[str]:
        """Collect clarification answers through the configured callback.

        Args:
            goal: Goal being clarified.
            questions: Clarifying questions to pass to the callback.

        Returns:
            List of answers aligned with ``questions``.
        """
        if not self._callback:
            return ["(no callback)"] * len(questions)
        try:
            result = self._callback(goal, questions)
            return result if isinstance(result, list) else [str(result)] * len(questions)
        except Exception:
            logger.warning("Exception handled by  callback prompt fallback", exc_info=True)
            _planner_module().logger.warning(
                "Clarification callback raised an error for goal %r - substituting '(callback error)' for all %d questions",
                goal,
                len(questions),
            )
            return ["(callback error)"] * len(questions)

    def set_interaction_mode(self, mode: str, callback: Callable | None = None) -> None:
        """Configure how the Foreman collects answers during clarify operations.

        Args:
            mode: Interaction mode such as ``"interactive"`` or ``"callback"``.
            callback: Callable invoked with ``(goal, questions)`` when mode is
                ``"callback"``.
        """
        self._interaction_mode = mode
        self._callback = callback
