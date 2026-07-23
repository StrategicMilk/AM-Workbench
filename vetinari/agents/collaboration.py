"""Inter-agent collaboration mixin for Vetinari agents.

Provides Blackboard-based help requests, findings publishing, task delegation,
mid-task info requests, and cross-model validation to any agent inheriting
BaseAgent.  These methods rely on ``self.agent_type``, ``self.name``,
``self._adapter_manager``, ``self._context``, and ``self.default_model``
being present (supplied by BaseAgent at runtime).
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any

from vetinari.adapters.base import InferenceRequest
from vetinari.config.inference_config import get_inference_config
from vetinari.constants import (
    CROSS_VALIDATION_AGREE_SCORE,
    CROSS_VALIDATION_DISAGREE_SCORE,
    TRUNCATE_OUTPUT_PREVIEW,
    TRUNCATE_TASK_DESC,
)
from vetinari.types import AgentType, StatusEnum

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from vetinari.adapters.manager import AdapterManager


# ── Cross-validation modes (US-059) ───────────────────────────────────
_CROSS_VALIDATE_MODES = frozenset({"architecture", "security_audit"})
_CROSS_VALIDATION_PROFILE = "cross_validation"
_VALIDATION_AGREE_RE = re.compile(r"^\s*AGREE\b", re.IGNORECASE)
_VALIDATION_DISAGREE_RE = re.compile(r"^\s*DISAGREE\b", re.IGNORECASE)

# ── Mid-task info request metadata constants (US-074) ─────────────────
NEEDS_INFO = "needs_info"
NEEDS_USER_INPUT = "needs_user_input"
DELEGATE_TO = "delegate_to"
QUESTION = "question"
CONTEXT_NEEDED = "context_needed"


class CollaborationBehavior:
    """Inter-agent collaboration capabilities mixed into BaseAgent.

    All methods rely on ``self.agent_type``, ``self.name``,
    ``self._adapter_manager``, ``self._context``, and
    ``self.default_model`` being present on the host class (supplied by
    BaseAgent).
    """

    agent_type: AgentType
    name: str
    _adapter_manager: AdapterManager | None
    _context: dict[str, Any]
    default_model: str

    # ------------------------------------------------------------------
    # Inter-agent communication via Blackboard
    # ------------------------------------------------------------------

    def request_help(
        self,
        content: str,
        request_type: str,
        priority: int = 5,
        ttl_seconds: int = 3600,
    ) -> str:
        """Post a help request on the shared Blackboard.

        Use this when the agent encounters a sub-task outside its expertise.
        The Blackboard routes it to the most capable available agent.

        Args:
            content: The content of the help request.
            request_type: Category string used for routing (e.g. "research").
            priority: Integer priority (1 = highest, 10 = lowest). Default 5.
            ttl_seconds: How long the entry remains valid. Default 3600.

        Returns:
            entry_id string — pass to ``get_help_result()`` to retrieve
            the answer.
        """
        from vetinari.memory.blackboard import get_blackboard

        board = get_blackboard()
        entry_id = board.post(
            content=content,
            request_type=request_type,
            requested_by=self.agent_type.value,
            priority=priority,
            ttl_seconds=ttl_seconds,
        )
        logger.debug("[%s] posted help request %s: %r", self.name, entry_id, request_type)
        return entry_id

    def get_help_result(self, entry_id: str, timeout: float = 30.0) -> Any | None:
        """Wait for and retrieve the result of a help request.

        Args:
            entry_id: ID returned by ``request_help()``.
            timeout: Maximum seconds to wait for the result.

        Returns:
            The result posted by the helper agent, or None if timed out or
            if the entry was not found.
        """
        from vetinari.memory.blackboard import get_blackboard

        board = get_blackboard()
        return board.get_result(entry_id, timeout=timeout)

    def publish_finding(self, key: str, value: Any, finding_type: str = "general") -> None:
        """Publish a discovery or intermediate result on the Blackboard.

        Other agents can query these findings via ``query_findings()``.

        Args:
            key: Unique name for this finding (e.g. "security_issues").
            value: The data to share.
            finding_type: Category for filtering (e.g. "security", "architecture").
        """
        from vetinari.memory.blackboard import get_blackboard

        board = get_blackboard()
        board.post(
            content=value,
            request_type=f"finding:{finding_type}",
            requested_by=self.agent_type.value,
            priority=3,
            metadata={"finding_key": key, "agent": self.agent_type.value},
        )
        logger.debug("[%s] published finding '%s' (%s)", self.name, key, finding_type)

    def query_findings(self, finding_type: str | None = None) -> list[dict[str, Any]]:
        """Query published findings from the Blackboard.

        Args:
            finding_type: Filter by category, or None to return all findings.

        Returns:
            List of dicts with keys: content, agent, finding_key.
        """
        from vetinari.memory.blackboard import get_blackboard

        board = get_blackboard()
        prefix = f"finding:{finding_type}" if finding_type else "finding:"
        entries = [entry for entry in board.get_pending(limit=1000) if entry.request_type.startswith(prefix)]
        return [
            {
                "content": e.content,
                "agent": e.metadata.get("agent", "unknown"),
                "finding_key": e.metadata.get("finding_key", ""),
            }
            for e in entries
        ]

    def delegate_task(self, task: Any, reason: str) -> Any:
        """Signal that this task is outside the agent's domain.

        Returns an AgentResult with ``delegation_requested=True`` so the
        orchestrator (AgentGraph) reassigns it to an appropriate agent.

        Args:
            task: The AgentTask to delegate.
            reason: Human-readable explanation for why delegation is needed.

        Returns:
            AgentResult with success=False and delegation metadata set.
        """
        from vetinari.agents.contracts import AgentResult

        logger.info("[%s] delegating task '%s': %s", self.name, task.task_id, reason)
        return AgentResult(
            success=False,
            output="",
            errors=[f"Task delegated: {reason}"],
            metadata={
                "delegation_requested": True,
                "delegation_reason": reason,
                "delegating_agent": self.agent_type.value,
            },
        )

    def can_handle(self, task: Any) -> bool:
        """Return True if this agent can handle the given task.

        Default implementation always returns True. Override in subclasses
        for smarter routing. AgentGraph queries this before assignment to
        find capable agents.

        Args:
            task: The AgentTask to check.

        Returns:
            True if this agent is able to handle the task.
        """
        return True

    # ------------------------------------------------------------------
    # Mid-task info request (US-074)
    # ------------------------------------------------------------------

    def request_info(
        self,
        question_text: str,
        delegate_to: AgentType | None = None,
    ) -> Any:
        """Signal that the agent needs more information before continuing.

        Args:
            question_text: The question to surface to the user or delegate agent.
            delegate_to: If set, route the question to this agent type
                instead of the user.

        Returns:
            AgentResult with success=False and needs_info metadata.
        """
        from vetinari.agents.contracts import AgentResult

        partial_work = getattr(self, "_current_output", None) or ""
        delegate_value = delegate_to.value if delegate_to else None
        return AgentResult(
            success=False,
            output={"partial_work": partial_work},
            metadata={
                NEEDS_INFO: True,
                QUESTION: question_text,
                DELEGATE_TO: delegate_value,
                NEEDS_USER_INPUT: delegate_to is None,
            },
        )

    # ------------------------------------------------------------------
    # Cross-model validation (US-059)
    # ------------------------------------------------------------------

    def _cross_validate(
        self,
        output: str,
        prompt: str,
        mode: str = "",
    ) -> dict[str, Any]:
        """Validate output using a secondary model for critical decisions.

        Auto-triggers for architecture and security_audit modes.
        Can be explicitly triggered via ``context['cross_validate'] = True``.

        Args:
            output: The primary model's output to validate.
            prompt: The prompt that produced the output.
            mode: The current agent mode.

        Returns:
            Dict with validated, agreement, notes, model_used keys.
        """
        explicit = self._context.get("cross_validate", False)
        is_critical = mode in _CROSS_VALIDATE_MODES

        if not is_critical and not explicit:
            return {
                "validated": False,
                StatusEnum.SKIPPED.value: True,
                "agreement": 1.0,
                "notes": "Cross-validation not required for this mode",
                "model_used": None,
            }

        adapter = self._adapter_manager
        if adapter is None:
            logger.debug("Cross-validation skipped: no adapter_manager available")
            return {
                "validated": False,
                "agreement": 0.0,
                "notes": "Validation skipped - secondary model unavailable",
                "model_used": None,
            }

        validation_model = self._select_validation_model(adapter)
        validation_profile = get_inference_config().get_profile(_CROSS_VALIDATION_PROFILE)
        try:
            validation_prompt = self._validation_prompt(output, prompt)
            response = adapter.infer(
                InferenceRequest(
                    model_id=validation_model,
                    prompt=validation_prompt,
                    max_tokens=validation_profile.max_tokens,
                    task_type="cross_validation",
                )
            )
            response_text = response.output
            verdict = self._parse_validation_verdict(response_text)
            agrees = verdict == "agree"
            validated = verdict in {"agree", "disagree"}
            return {
                "validated": validated,
                "agreement": CROSS_VALIDATION_AGREE_SCORE if agrees else CROSS_VALIDATION_DISAGREE_SCORE,
                "notes": response_text[:200],
                "model_used": validation_model,
                "calibration": {
                    "verdict": verdict,
                    "parser": "anchored_agree_disagree",
                    "fallback_model_used": validation_model == self.default_model,
                },
                "issues": [] if agrees else [response_text[:200]],
            }
        except Exception:
            logger.warning("Cross-validation inference failed - failing closed")
            return {
                "validated": False,
                "agreement": 0.0,
                "notes": "Validation failed - secondary model inference error",
                "model_used": None,
            }

    @staticmethod
    def _validation_prompt(output: str, prompt: str) -> str:
        return (
            "Review this output for correctness and completeness:\n\n"
            f"Original prompt: {prompt[:TRUNCATE_TASK_DESC]}\n\n"
            f"Output to validate: {output[:TRUNCATE_OUTPUT_PREVIEW]}\n\n"
            "Respond with: AGREE if correct, DISAGREE with reasons if not."
        )

    @staticmethod
    def _parse_validation_verdict(response_text: str) -> str:
        if _VALIDATION_AGREE_RE.search(response_text):
            return "agree"
        if _VALIDATION_DISAGREE_RE.search(response_text):
            return "disagree"
        return "uncertain"

    def _select_validation_model(self, adapter: Any) -> str:
        try:
            from vetinari.learning.model_selector import get_thompson_selector

            selector = get_thompson_selector()
            candidates = [m for m in (adapter.list_models() or []) if m != self.default_model]
            return selector.select_model(
                "cross_validation",
                candidates,
                cost_per_model=self._validation_cost_map(),
            )
        except Exception:
            logger.warning("Exception handled by  select validation model fallback", exc_info=True)
            return self.default_model

    @staticmethod
    def _validation_cost_map() -> dict[str, float] | None:
        try:
            from vetinari.analytics.cost import get_cost_tracker

            report = get_cost_tracker().get_summary()
            if report:
                return {m: v.get("cost_usd", 0.0) for m, v in report.get("by_model", {}).items()}
        except Exception:
            logger.warning("Cost data unavailable for model selection")
        return None
