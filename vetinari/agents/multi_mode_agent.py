"""Multi-Mode Agent Base Class (Phase 3).

======================================
Base class for consolidated agents that support multiple operational modes.

Each consolidated agent handles multiple task modes by routing tasks
to the appropriate mode based on task context. This preserves specialized
logic per mode while reducing the agent count from 22 to 8.

Usage::

    class InspectorAgent(MultiModeAgent):
        MODES = {
            "code_review": "_execute_code_review",
            "security_audit": "_execute_security_audit",
            "test_generation": "_execute_test_generation",
        }

        def _execute_code_review(self, task): ...
        def _execute_security_audit(self, task): ...
        def _execute_test_generation(self, task): ...
"""

from __future__ import annotations

import dataclasses
import logging
from typing import TYPE_CHECKING, Any

from vetinari.agents.base_agent import BaseAgent
from vetinari.agents.contracts import (
    AgentResult,
    AgentTask,
    VerificationResult,
)
from vetinari.agents.self_reflection import (
    ReflectionStrategy,
    get_reflection_strategy,
    reflect,
)
from vetinari.types import AgentType

logger = logging.getLogger(__name__)


if TYPE_CHECKING:
    from vetinari.skills.skill_spec import SkillSpec


def _score_verification_substance(values: list[Any]) -> float:
    """Return a bounded score for the amount of real output content present."""
    score = 0.0
    for value in values:
        if isinstance(value, str):
            text = value.strip()
            if len(text) >= 120:
                score += 0.45
            elif len(text) >= 40:
                score += 0.30
            elif len(text) >= 12:
                score += 0.15
        elif isinstance(value, dict):
            populated = [item for item in value.values() if item not in (None, "", [], {})]
            if populated:
                score += min(0.45, 0.15 + 0.08 * len(populated))
        elif isinstance(value, list):
            populated = [item for item in value if item not in (None, "", [], {})]
            if populated:
                score += min(0.45, 0.12 + 0.06 * len(populated))
        else:
            score += 0.05
    return min(0.75, round(score, 3))


class MultiModeAgent(BaseAgent):
    """Base class for consolidated multi-mode agents.

    Subclasses define ``MODES`` (a dict mapping mode names to handler method
    names) and ``DEFAULT_MODE``.  The mode is determined from
    ``task.context["mode"]`` or inferred from the task description.
    """

    # Subclasses override these
    MODES: dict[str, str] = {}  # mode_name -> method_name
    DEFAULT_MODE: str = ""  # fallback mode
    MODE_KEYWORDS: dict[str, list[str]] = {}  # mode_name -> keyword list

    def __init__(self, agent_type: AgentType, config: dict[str, Any] | None = None):
        super().__init__(agent_type, config)
        self._current_mode: str | None = None
        # B5: Validate MODES at init — catch misconfigurations early
        for mode_name, method_name in self.MODES.items():
            handler = getattr(self, method_name, None)
            if handler is None or not callable(handler):
                raise TypeError(
                    f"{self.__class__.__name__}: MODES[{mode_name!r}] references "
                    f"{method_name!r} which is not a callable method on this class.",
                )

    @property
    def current_mode(self) -> str | None:
        """Return the currently active mode."""
        return self._current_mode

    @property
    def available_modes(self) -> list[str]:
        """Return all available modes."""
        return list(self.MODES.keys())

    def _resolve_mode(self, task: AgentTask) -> str:
        """Determine which mode to use for a task.

        Resolution order:
        1. Explicit ``task.context["mode"]``
        2. Thompson Sampling override (when sufficient data exists)
        3. Keyword matching against task description
        4. DEFAULT_MODE fallback
        """
        # 1. Explicit mode — check task.mode attribute first, then context dict.
        # task.mode is the structured field; task.context["mode"] is the legacy dict key.
        # Return as-is so execute() can handle unknown modes via delegation rather
        # than silently remapping to the default.
        task_mode = getattr(task, "mode", None)
        mode = task_mode if isinstance(task_mode, str) else ""
        mode = mode or task.context.get("mode", "")
        if mode:
            return mode

        # 2. Try Thompson Sampling mode selection (Dept 6, connection #77)
        task_type = task.context.get("task_type", "general")
        agent_type_str = self.agent_type.value if hasattr(self.agent_type, "value") else str(self.agent_type)
        try:
            from vetinari.learning.model_selector import get_thompson_selector  # Late import to avoid circular

            selector = get_thompson_selector()
            if selector.has_mode_data(agent_type_str, task_type):
                thompson_mode = selector.select_mode(agent_type_str, task_type, list(self.MODES.keys()))
                if thompson_mode in self.MODES:
                    logger.info(
                        "Thompson selected mode %s for %s/%s",
                        thompson_mode,
                        agent_type_str,
                        task_type,
                    )
                    return thompson_mode
        except Exception:
            logger.warning("Thompson mode selection unavailable, using default", exc_info=True)

        # 3. Keyword matching
        desc = (task.description or "").lower()
        best_mode = ""
        best_score = 0
        for mode_name, keywords in self.MODE_KEYWORDS.items():
            score = sum(1 for kw in keywords if kw in desc)
            if score > best_score:
                best_score = score
                best_mode = mode_name

        if best_mode:
            return best_mode

        # 4. Default
        return self.DEFAULT_MODE or (next(iter(self.MODES.keys())) if self.MODES else "")

    def execute(self, task: AgentTask) -> AgentResult:
        """Route task to the appropriate mode handler.

        Returns:
            The AgentResult result.
        """

        def execute_mode(prepared_task: AgentTask) -> AgentResult:
            """Execute the resolved mode handler for a prepared task.

            Returns:
                The result produced by the selected mode handler.
            """
            mode = self._resolve_mode(prepared_task)
            self._current_mode = mode

            handler_name = self.MODES.get(mode)
            if not handler_name:
                # Use delegate_task so the orchestrator can reassign to a more capable agent
                # rather than silently failing.
                self._log("warning", "Unknown mode '%s' for %s — delegating", mode, self.agent_type.value)
                return self.delegate_task(
                    prepared_task,
                    reason=f"Agent {self.agent_type.value!r} does not handle mode {mode!r}",
                )

            handler = getattr(self, handler_name, None)
            if handler is None:
                self._log("error", "Handler '%s' not found on %s", handler_name, self.__class__.__name__)
                return AgentResult(
                    success=False,
                    output=None,
                    errors=[f"Handler '{handler_name}' not implemented"],
                )

            self._log("info", "Executing in mode '%s' via %s", mode, handler_name)
            try:
                result = handler(prepared_task)
            except Exception as e:
                logger.exception("Mode '%s' execution failed in %s", mode, self.__class__.__name__)
                self._log("error", "Mode '%s' execution failed: %s", mode, e)
                return AgentResult(
                    success=False,
                    output=None,
                    errors=[f"Mode '{mode}' failed: {e!s}"],
                )

            return self._apply_self_reflection(prepared_task, result, handler)

        return self._execute_safely(task, execute_mode)

    def _apply_self_reflection(
        self,
        task: AgentTask,
        result: AgentResult,
        handler: Any,
    ) -> AgentResult:
        """Apply self-reflection to refine handler output before submission.

        Self-reflection lets the agent critique and improve its own output
        before it reaches the Inspector, reducing rejection rates.  Only runs
        when the Foreman has set a non-SIMPLE strategy in ``task.context``.

        Args:
            task: The task being executed.
            result: Initial handler result.
            handler: The mode handler callable, for re-execution during refinement.

        Returns:
            The original or refined AgentResult.
        """
        if not result.success:
            return result  # Don't reflect on failed results

        strategy = get_reflection_strategy(task)
        if strategy == ReflectionStrategy.SIMPLE:
            return result  # No-op — skip reflection overhead

        refiner = self._reflection_refiner(handler)
        reflection = reflect(task, result, strategy, self._reflection_evaluator, refiner)

        if reflection.is_improved:
            logger.info(
                "Self-reflection improved output for task %s (%s, %d iterations)",
                task.task_id,
                strategy.value,
                reflection.iterations_used,
            )
            return dataclasses.replace(
                result,
                output=reflection.refined_output,
                metadata={
                    **(result.metadata or {}),
                    "self_reflection": {
                        "strategy": strategy.value,
                        "iterations": reflection.iterations_used,
                        "is_improved": True,
                        "notes": reflection.evaluation_notes,
                    },
                },
            )

        return result

    def _reflection_evaluator(self, output: Any) -> tuple[bool, list[str]]:
        vr = self.verify(output)
        issues_as_str: list[str] = []
        for issue in vr.issues or []:
            if isinstance(issue, dict):
                issues_as_str.append(issue.get("message", str(issue)))
            else:
                issues_as_str.append(str(issue))
        return vr.passed, issues_as_str

    @staticmethod
    def _reflection_refiner(handler: Any) -> Any:
        def refiner(t: AgentTask, prior: AgentResult, feedback: list[str]) -> AgentResult:
            del prior
            refined_task = dataclasses.replace(
                t,
                context={**t.context, "reflection_feedback": feedback},
            )
            return handler(refined_task)

        return refiner

    def verify(self, output: Any) -> VerificationResult:
        """Default verification — requires structured dict output with content.

        Subclasses should override with mode-specific checks.

        Returns:
            VerificationResult requiring positive evidence.
        """
        if output is None:
            return VerificationResult(
                passed=False,
                issues=[{"message": "Output is None"}],
                score=0.0,
            )
        if not isinstance(output, dict):
            return VerificationResult(
                passed=False,
                issues=[{"message": "No structured verification output"}],
                score=0.0,
            )
        if not output:
            return VerificationResult(
                passed=False,
                issues=[{"message": "Empty output dict"}],
                score=0.0,
            )
        # Require enough content-bearing substance to avoid passing on a dict
        # that contains only metadata or a single token-shaped answer.
        CONTENT_FIELDS = {"content", "result", "output", "findings", "issues", "tests", "sections"}
        content_values = [output.get(k) for k in CONTENT_FIELDS if k in output and output.get(k)]
        if not content_values:
            return VerificationResult(
                passed=False,
                issues=[
                    {
                        "message": "Output dict has no recognized content fields (content, result, output, findings, issues, tests, sections)"
                    }
                ],
                score=0.0,
            )
        substance_score = _score_verification_substance(content_values)
        evidence_bonus = 0.0
        if output.get("metadata"):
            evidence_bonus += 0.08
        if output.get("provenance") or output.get("evidence"):
            evidence_bonus += 0.12
        if output.get("tests") or output.get("findings") or output.get("sections"):
            evidence_bonus += 0.10

        score = min(1.0, round(substance_score + evidence_bonus, 3))
        if score < 0.65:
            return VerificationResult(
                passed=False,
                issues=[{"message": "Output content is too thin for default multimode verification"}],
                score=score,
            )
        return VerificationResult(passed=True, score=score)

    def get_system_prompt(self) -> str:
        """Return mode-aware system prompt loaded from agent markdown files.

        Assembles the prompt in cache-friendly order:
        1. Agent spec from ``vetinari/config/agents/<name>.md`` (static per agent)
        2. Mode-relevant practices from ``practices.py`` (static per mode)
        3. Code standards for code-generating modes (static per mode)

        Falls back to subclass overrides or a generic default.

        Returns:
            The assembled system prompt string.
        """
        mode = self._current_mode or self.DEFAULT_MODE

        # Try loading from vetinari/config/agents/*.md
        try:
            from vetinari.agents.base_agent import _parse_model_size_b
            from vetinari.agents.practices import get_code_standards, get_practices_for_mode
            from vetinari.agents.prompt_loader import load_agent_prompt

            loaded = load_agent_prompt(self.agent_type, mode=mode)
            if loaded:
                # Inject mode-relevant practices (2-4 per mode, ~200-350 tokens)
                practices = get_practices_for_mode(mode)
                if practices:
                    loaded = loaded + "\n\n" + practices

                # Append code standards for code-generating modes only when the
                # active model has enough capacity (>7B).  Small models (<=7B)
                # skip the verbose standards block to conserve context budget.
                _active_model = self._last_inference_model_id or self.default_model or ""
                _model_size = _parse_model_size_b(_active_model)
                _is_compact_model = 0.0 < _model_size <= 7.0
                code_std = get_code_standards(mode)
                if code_std and not _is_compact_model:
                    loaded = loaded + "\n\n" + code_std
                return loaded
        except Exception:
            logger.warning(
                "Failed to load prompt from markdown for %s",
                self.agent_type.value,
                exc_info=True,
            )

        # Fallback to subclass overrides
        mode_prompt = self._get_mode_system_prompt(mode)
        if mode_prompt:
            return mode_prompt
        return self._get_base_system_prompt()

    def _get_base_system_prompt(self) -> str:
        """Base system prompt — subclasses override."""
        return f"You are the {self.name} agent operating in {self._current_mode or 'default'} mode."

    def _get_mode_system_prompt(self, mode: str) -> str:
        """Mode-specific system prompt — subclasses override for custom prompts."""
        return ""

    def get_capabilities(self) -> list[str]:
        """Return capabilities across all modes."""
        return list(self.MODES.keys())

    @classmethod
    def to_skill_spec(cls) -> SkillSpec:
        """Auto-derive a SkillSpec from this agent's class metadata.

        Generates a baseline SkillSpec using MODES, MODE_KEYWORDS, agent_type,
        and class docstring. Hand-written standards/constraints can be merged
        on top via ``merge_skill_spec()``.

        Returns:
            A SkillSpec populated from agent class attributes.
        """
        from vetinari.skills.skill_spec import SkillSpec  # lazy to avoid circular import

        # Derive agent_type from the class-level AGENT_TYPE attribute.
        # Class-name guessing is intentionally avoided — guessed names produce
        # skill_ids that diverge from the canonical AgentType enum values,
        # causing SKILL_REGISTRY lookups to silently miss.
        agent_type_value = ""
        if hasattr(cls, "AGENT_TYPE") and cls.AGENT_TYPE:
            agent_type_value = cls.AGENT_TYPE.value if hasattr(cls.AGENT_TYPE, "value") else str(cls.AGENT_TYPE)
        else:
            logger.warning(
                "to_skill_spec: %s has no AGENT_TYPE — skill_id will be derived from class name and may not match registry keys",
                cls.__name__,
            )
            # Fall back to class name without the "Agent" suffix, uppercased.
            agent_type_value = cls.__name__.removesuffix("Agent").upper()

        # skill_id: lowercase agent type
        skill_id = agent_type_value.lower().replace("consolidated", "").strip("_")

        # Human-readable name from class name
        raw_name = cls.__name__
        for suffix in ("Agent",):
            raw_name = raw_name.removesuffix(suffix)
        # Insert spaces before capitals: "QualityAgent" -> "Quality"
        display_name = raw_name

        # Description from class docstring
        doc = (cls.__doc__ or "").strip()
        description = doc.split("\n")[0] if doc else f"{display_name} agent"

        # Modes
        modes = list(cls.MODES.keys()) if cls.MODES else []

        # Capabilities: flatten MODE_KEYWORDS values + mode names
        capabilities = list(modes)
        seen = set(capabilities)
        for keywords in (cls.MODE_KEYWORDS or {}).values():
            for kw in keywords:
                if kw not in seen:
                    capabilities.append(kw)
                    seen.add(kw)

        return SkillSpec(
            skill_id=skill_id,
            name=display_name,
            description=description,
            agent_type=agent_type_value,
            modes=modes,
            capabilities=capabilities,
            tags=["auto-derived"],
        )
