"""Default task-handler support for pipeline orchestration."""

from __future__ import annotations

import contextlib
import json
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, cast

from vetinari.adapters.adapter_cache import get_local_inference_adapter
from vetinari.constants import INFERENCE_STATUS_OK

if TYPE_CHECKING:
    from vetinari.orchestration.execution_graph import ExecutionTaskNode

logger = logging.getLogger(__name__)


_LOCAL_MODEL_MARKERS = ("qwen", "llama", "mistral", "gemma", "phi", "local", "default")


class PipelineDefaultHandlerMixin:
    """Default inference-backed execution handler factory."""

    if TYPE_CHECKING:
        agent_context: Any

    def _make_default_handler(self) -> Callable[[ExecutionTaskNode], dict[str, Any]]:
        """Create the default task handler using agent inference with token optimisation."""

        def handle_task(task: ExecutionTaskNode) -> dict[str, Any]:
            exec_ctx = self._enter_execution_mode(task.id)
            try:
                return self._handle_task_inner(task)
            finally:
                if exec_ctx is not None:
                    with contextlib.suppress(Exception):
                        exec_ctx.__exit__(None, None, None)

        return handle_task

    @staticmethod
    def _enter_execution_mode(task_id: str) -> Any | None:
        """Switch to EXECUTION mode so tool permission checks pass."""
        try:
            import vetinari.execution_context as execution_context
            from vetinari.types import ExecutionMode

            ctx_mgr = execution_context.get_context_manager()
            exec_ctx = ctx_mgr.temporary_mode(ExecutionMode.EXECUTION, task_id=task_id)
            exec_ctx.__enter__()
            return exec_ctx
        except Exception:
            logger.warning("Exception handled by  enter execution mode fallback", exc_info=True)
            return None

    @staticmethod
    def _build_task_context(task: ExecutionTaskNode) -> str:
        """Build prompt context from task input and rework metadata."""
        task_context = " ".join(str(v)[:500] for v in task.input_data.values() if v) if task.input_data else ""
        rework_feedback = task.input_data.get("rework_feedback", "")
        rework_hint = task.input_data.get("rework_hint", "")
        if rework_hint == "research_context_before_retry":
            task_context += " [REWORK: Research additional context before answering]"
        elif rework_hint == "widen_scope":
            task_context += " [REWORK: Consider a broader solution scope]"
        if rework_feedback:
            task_context += f" [PRIOR FAILURE FEEDBACK: {rework_feedback[:500]}]"
        return task_context

    @staticmethod
    def _prepare_prompt(task: ExecutionTaskNode, assigned_model: str, task_context: str) -> tuple[str, int, Any]:
        """Prepare an optimized prompt or fall back to configured inference defaults."""
        import vetinari.config.inference_config as inference_config

        is_cloud = not any(marker in assigned_model.lower() for marker in _LOCAL_MODEL_MARKERS)
        try:
            import vetinari.token_optimizer as token_optimizer

            opt_result = token_optimizer.get_token_optimizer().prepare_prompt(
                prompt=task.description,
                context=task_context,
                task_type=task.task_type or "general",
                task_description=task.description,
                is_cloud_model=is_cloud,
                task_id=task.id,
            )
            return opt_result["prompt"], cast(int, opt_result["max_tokens"]), opt_result["temperature"]
        except Exception:
            try:
                fallback = inference_config.get_inference_config().get_effective_params(task.task_type or "general")
                return task.description, cast(int, fallback.get("max_tokens", 2048)), fallback.get("temperature", 0.3)
            except Exception:
                logger.warning("Exception handled by  prepare prompt fallback", exc_info=True)
                return task.description, 2048, 0.3

    @staticmethod
    def _post_blackboard_task(task: ExecutionTaskNode, task_type_label: str) -> None:
        """Post task metadata to the optional blackboard."""
        try:
            import vetinari.memory.blackboard as blackboard

            blackboard.get_blackboard().post(
                content=task.description[:500],
                request_type=task_type_label,
                requested_by="orchestrator",
                priority=5,
                metadata={"task_id": task.id},
            )
        except Exception:
            logger.warning("Blackboard write failed; continuing without it", exc_info=True)

    @staticmethod
    def _augment_with_web_context(prompt: str, task: ExecutionTaskNode, task_type_label: str) -> str:
        """Add web-search snippets for research-like tasks when the tool is available."""
        if task_type_label not in ("research", "exploration", "documentation", "fact_finding"):
            return prompt
        try:
            from vetinari.tools.web_search import web_search

            results = web_search(task.description[:200], max_results=3)
            if not results:
                return prompt
            web_context = "\nRelevant web search results:\n"
            for result in results[:3]:
                web_context += f"- [{result.get('title', '')}]({result.get('url', '')}): {result.get('snippet', '')}\n"
            return web_context + "\n" + prompt
        except Exception:
            logger.warning("Web search unavailable; continuing without augmentation", exc_info=True)
            return prompt

    def _build_system_prompt(self, task: ExecutionTaskNode, task_type_label: str) -> str:
        """Build the system prompt, including optional project context."""
        system_prompt = (
            f"You are Vetinari, an AI orchestration system executing a {task_type_label} task. "
            "Produce structured, production-quality output. Return valid JSON when structured output is requested. "
            "Include reasoning and confidence scores with decisions. Report errors with actionable context."
        )
        project_context = task.input_data.get("project_context")
        if project_context and isinstance(project_context, dict):
            parts = self._project_context_parts(project_context)
            if parts:
                system_prompt += "\n\nProject context:\n" + "\n".join(parts)
        return system_prompt

    @staticmethod
    def _project_context_parts(project_context: dict[str, Any]) -> list[str]:
        """Format project-context fields for system prompt inclusion."""
        parts = []
        for key, label in (
            ("tech_stack", "Tech stack"),
            ("category", "Category"),
            ("priority", "Priority"),
            ("required_features", "Required features"),
            ("things_to_avoid", "Constraints"),
        ):
            value = project_context.get(key)
            if isinstance(value, list):
                value = ", ".join(value)
            if value:
                parts.append(f"{label}: {value}")
        return parts

    @staticmethod
    def _score_best_of_n_candidate(candidate: str, task_type_label: str) -> float:
        """Score candidates by output quality signals, not output length."""
        text = candidate.strip()
        if not text:
            return 0.0
        lowered = text.lower()
        score = 0.35
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                score += 0.25
                score += 0.05 * min(
                    sum(key in parsed for key in ("result", "summary", "confidence", "evidence", "tests")),
                    3,
                )
            elif isinstance(parsed, list) and parsed:
                score += 0.15
        except json.JSONDecodeError:
            if any(marker in text for marker in ("\n-", "\n*", "```", ":")):
                score += 0.1

        if any(marker in lowered for marker in ("evidence", "verified", "test", "implemented", "result", "summary")):
            score += 0.15
        if task_type_label and task_type_label.lower() in lowered:
            score += 0.05
        if any(
            marker in lowered for marker in ("todo", "placeholder", "lorem ipsum", "i don't know", "error:", "failed")
        ):
            score -= 0.3
        if len(text.split()) < 3:
            score -= 0.15
        return max(0.0, min(1.0, score))

    @staticmethod
    def _best_of_n_output(
        adapter_manager: Any,
        assigned_model: str,
        optimised_prompt: str,
        system_prompt: str,
        max_tokens: int,
        temperature: Any,
        task: ExecutionTaskNode,
        task_type_label: str,
    ) -> str | None:
        """Try Best-of-N generation for task tiers that request it."""
        try:
            import vetinari.models.best_of_n as best_of_n
            import vetinari.models.dynamic_model_router as dynamic_model_router
            from vetinari.adapters.base import InferenceRequest

            n = best_of_n.get_n_for_tier(task_type_label)
            if n <= 1:
                return None
            router = dynamic_model_router.get_model_router()

            def generate_fn(prompt: str) -> str:
                request = InferenceRequest(
                    model_id=assigned_model,
                    prompt=prompt,
                    system_prompt=system_prompt,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    metadata={"task_type": task_type_label},
                )
                response = adapter_manager.infer(request)
                return response.output if response.status == INFERENCE_STATUS_OK else ""

            selector = router.get_best_of_n_selector(generate_fn)
            selected = selector.generate_and_select(
                optimised_prompt,
                n=n,
                scorer=lambda candidate: PipelineDefaultHandlerMixin._score_best_of_n_candidate(
                    candidate,
                    task_type_label,
                ),
                tie_breaker=lambda candidate: (0, candidate),
            )
            if isinstance(selected, str):
                logger.debug("Best-of-%d selection applied for task %s (tier=%s)", n, task.id, task_type_label)
                return selected
            logger.warning("Best-of-N generation returned non-string output for task %s", task.id)
        except Exception:
            logger.warning("Best-of-N generation unavailable for task %s - falling back", task.id)
        return None

    def _run_adapter_manager(
        self,
        task: ExecutionTaskNode,
        assigned_model: str,
        optimised_prompt: str,
        system_prompt: str,
        max_tokens: int,
        temperature: Any,
        task_type_label: str,
    ) -> dict[str, Any] | None:
        """Run inference through the configured adapter manager."""
        adapter_manager = self.agent_context.get("adapter_manager")
        if not adapter_manager:
            return None
        try:
            from vetinari.adapters.base import InferenceRequest

            output_text = self._best_of_n_output(
                adapter_manager,
                assigned_model,
                optimised_prompt,
                system_prompt,
                max_tokens,
                temperature,
                task,
                task_type_label,
            )
            tokens_used = 0
            metadata: dict[str, Any] = {}
            if output_text is None:
                response = adapter_manager.infer(
                    InferenceRequest(
                        model_id=assigned_model,
                        prompt=optimised_prompt,
                        system_prompt=system_prompt,
                        max_tokens=max_tokens,
                        temperature=temperature,
                    )
                )
                if response.status == INFERENCE_STATUS_OK:
                    output_text = response.output
                    tokens_used = response.tokens_used
                    metadata = response.metadata
            if output_text is not None:
                return {
                    "result": output_text,
                    "status": "ok",
                    "task_id": task.id,
                    "tokens_used": tokens_used,
                    "metadata": metadata,
                }
        except Exception as exc:
            logger.warning("Adapter inference failed for task %s: %s", task.id, exc)
        return None

    @staticmethod
    def _run_local_adapter(
        task: ExecutionTaskNode,
        assigned_model: str,
        system_prompt: str,
        optimised_prompt: str,
        task_type_label: str,
    ) -> dict[str, Any]:
        """Run fallback local adapter inference."""
        adapter = get_local_inference_adapter(assigned_model)
        result = adapter.chat(
            model_id=assigned_model,
            system_prompt=system_prompt,
            input_text=optimised_prompt,
            task_type=task_type_label,
        )
        return {
            "result": result.get("output", ""),
            "status": "ok",
            "task_id": task.id,
            "tokens_used": result.get("tokens_used", 0),
        }

    def _handle_task_inner(self, task: ExecutionTaskNode) -> dict[str, Any]:
        """Execute a single task node via agent inference."""
        try:
            assigned_model = task.input_data.get("assigned_model", "default")
            task_type_label = task.task_type or "general"
            task_context = self._build_task_context(task)
            prompt, max_tokens, temperature = self._prepare_prompt(task, assigned_model, task_context)
            self._post_blackboard_task(task, task_type_label)
            prompt = self._augment_with_web_context(prompt, task, task_type_label)
            system_prompt = self._build_system_prompt(task, task_type_label)
            result = self._run_adapter_manager(
                task, assigned_model, prompt, system_prompt, max_tokens, temperature, task_type_label
            )
            if result is not None:
                return result
            return self._run_local_adapter(task, assigned_model, system_prompt, prompt, task_type_label)
        except Exception as exc:
            logger.error("Task handler failed for %s: %s", task.id, exc)
            return {"result": "", "status": "error", "error": str(exc), "task_id": task.id}
