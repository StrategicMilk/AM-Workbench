"""2-Stage LLM Pipeline: Architect + Executor.

Implements a pipeline where a larger "architect" model creates a high-level plan,
then a smaller "executor" model implements each step. This enables cost-effective
orchestration by using expensive models only for planning and cheap models for
execution of well-defined tasks.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from typing import Any

from vetinari.adapters.adapter_cache import get_local_inference_adapter
from vetinari.boundary_guards import assert_dependency_success
from vetinari.constants import INFERENCE_STATUS_OK, TRUNCATE_OUTPUT_PREVIEW
from vetinari.orchestration.architect_executor_models import ArchitectPlan, PipelineConfig
from vetinari.types import StatusEnum

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------


class ArchitectExecutorPipeline:
    """2-stage pipeline: architect plans, executor implements.

    Stage 1 (Architect): A larger model analyzes the goal and repository context
    to produce a structured plan with ordered steps, dependencies, and file targets.

    Stage 2 (Executor): A smaller, faster model implements each step in dependency
    order, receiving only the focused context it needs.

    This separation keeps costs low while maintaining high-quality plans.
    """

    def __init__(
        self,
        architect_model: str | None = None,
        executor_model: str | None = None,
        config: dict | None = None,
    ):
        if config and isinstance(config, dict):
            self._config = PipelineConfig.from_dict(config)
        elif config and isinstance(config, PipelineConfig):
            self._config = config
        else:
            self._config = PipelineConfig()

        # Override models from explicit args if provided
        self.architect_model = architect_model or self._config.architect_model
        self.executor_model = executor_model or self._config.executor_model
        self._enabled = self._config.enabled

        logger.info(
            "ArchitectExecutorPipeline initialized "
            f"(architect={self.architect_model}, executor={self.executor_model}, "
            f"enabled={self._enabled})",
        )

    @property
    def enabled(self) -> bool:
        """Whether the pipeline is enabled."""
        return self._enabled

    @enabled.setter
    def enabled(self, value: bool) -> None:
        """Set whether the architect-executor pipeline is enabled.

        Args:
            value: True to enable the pipeline, False to disable it.
        """
        self._enabled = value

    # ------------------------------------------------------------------
    # Stage 1: Architect
    # ------------------------------------------------------------------

    def create_plan(self, goal: str, context: dict | None = None) -> ArchitectPlan:
        """Stage 1: Architect creates a high-level plan.

        The architect model receives the goal and repository context, then
        produces a structured plan with steps, dependencies, and file targets.

        Args:
            goal: What needs to be accomplished.
            context: Repository context (files, structure, constraints, etc.).

        Returns:
            ArchitectPlan with ordered steps and dependencies.

        Raises:
            RuntimeError: If the operation fails.
        """
        context = context or {}
        logger.info("[Architect] Creating plan for: %s", goal[:80])
        start = time.time()

        # Build the architect prompt
        prompt = self._build_architect_prompt(goal, context)

        # Try to call the architect model
        plan = None
        try:
            raw_output = self._call_model(
                model=self.architect_model,
                prompt=prompt,
                system_prompt=(
                    "You are a senior software architect. Analyze the goal and "
                    "produce a structured JSON plan with steps, dependencies, "
                    "and file targets. Each step should have: id, description, "
                    "files (list), agent_type, complexity (low/medium/high)."
                ),
                max_tokens=self._config.architect_max_tokens,
                temperature=self._config.architect_temperature,
                context=context,
            )
            plan = self._parse_architect_output(raw_output, goal)
        except Exception as e:
            logger.warning("[Architect] Model call failed: %s", e)
            if self._config.fallback_to_single:
                logger.info("[Architect] Falling back to single-step plan")
                plan = self._make_fallback_plan(goal, context)
            else:
                raise

        if plan is None:
            if self._config.fallback_to_single:
                plan = self._make_fallback_plan(goal, context)
            else:
                raise RuntimeError("Architect failed to produce a plan")

        # Enforce max_steps
        if len(plan.steps) > self._config.max_steps:
            logger.warning("[Architect] Plan has %s steps, truncating to %s", len(plan.steps), self._config.max_steps)
            truncated_ids = {s["id"] for s in plan.steps[: self._config.max_steps]}
            plan.steps = plan.steps[: self._config.max_steps]
            plan.dependencies = {
                k: [d for d in v if d in truncated_ids] for k, v in plan.dependencies.items() if k in truncated_ids
            }

        elapsed = time.time() - start
        logger.info("[Architect] Plan created: %s steps in %.1fs", elapsed, plan.step_count())
        return plan

    # ------------------------------------------------------------------
    # Stage 2: Executor
    # ------------------------------------------------------------------

    def execute_plan(self, plan: ArchitectPlan, executor_fn: Callable | None = None) -> list[dict[str, Any]]:
        """Stage 2: Executor implements each step.

        Steps are executed in dependency order. Each step receives a focused
        prompt containing only its description and relevant file context.

        Args:
            plan: The architect plan to execute.
            executor_fn: Optional custom executor function.
                         Signature: (step: dict, plan: ArchitectPlan) -> dict
                         Must return a dict with at least a "success" key.

        Returns:
            List of result dicts, one per step executed.
        """
        logger.info("[Executor] Executing plan %s (%s steps)", plan.plan_id, plan.step_count())
        results: list[dict[str, Any]] = []
        completed_ids: set = set()
        failed_ids: set = set()

        # Process steps in dependency order
        max_iterations = plan.step_count() + 5  # Safety bound
        iteration = 0

        while len(completed_ids) + len(failed_ids) < plan.step_count():
            iteration += 1
            if iteration > max_iterations:
                logger.error("[Executor] Exceeded max iterations, aborting")
                break

            ready_candidates = plan.get_ready_steps(completed_ids | failed_ids)
            ready = []
            for candidate in ready_candidates:
                candidate_id = candidate.get("id", "")
                try:
                    for dep_id in plan.dependencies.get(candidate_id, []):
                        assert_dependency_success(dep_id, failed_ids)
                except RuntimeError as exc:
                    failed_ids.add(candidate_id)
                    results.append({"step_id": candidate_id, "success": False, "error": str(exc)})
                    logger.warning("[Executor] Step %s blocked by failed dependency: %s", candidate_id, exc)
                    continue
                ready.append(candidate)
            if not ready:
                # Check for deadlock
                remaining = plan.step_count() - len(completed_ids) - len(failed_ids)
                if remaining > 0:
                    logger.warning("[Executor] No ready steps but %s remain -- possible dependency deadlock", remaining)
                break

            for step in ready:
                step_id = step.get("id", f"step-{len(results)}")
                # Fetch the canonical step dict via get_step() so any future
                # per-step enrichment (e.g., injected metadata) is read from
                # the single authoritative source rather than the iteration var.
                canonical_step = plan.get_step(step_id)
                if canonical_step is not None:
                    step = canonical_step
                logger.info("[Executor] Running step %s: %s", step_id, step.get("description", "")[:60])

                try:
                    if executor_fn:
                        result = executor_fn(step, plan)
                    else:
                        result = self._default_execute_step(step, plan)

                    result.setdefault("step_id", step_id)
                    if "success" not in result:
                        result["success"] = False  # Assume failure unless explicitly set
                    results.append(result)

                    if result.get("success"):
                        completed_ids.add(step_id)
                    else:
                        failed_ids.add(step_id)
                        logger.warning("[Executor] Step %s failed: %s", step_id, result.get("error", "unknown"))

                except Exception as e:
                    logger.error("[Executor] Step %s raised exception: %s", step_id, e)
                    results.append({
                        "step_id": step_id,
                        "success": False,
                        "error": str(e),
                    })
                    failed_ids.add(step_id)

        logger.info(
            f"[Executor] Plan {plan.plan_id} finished: {len(completed_ids)} completed, {len(failed_ids)} failed",
        )
        return results

    # ------------------------------------------------------------------
    # Full pipeline
    # ------------------------------------------------------------------

    def run(
        self,
        goal: str,
        context: dict | None = None,
        executor_fn: Callable | None = None,
    ) -> dict[str, Any]:
        """Full pipeline: plan then execute.

        Args:
            goal: What needs to be accomplished.
            context: Repository context.
            executor_fn: Optional custom executor function.

        Returns:
            Dict with plan, results, and overall success flag.
        """
        if not self._enabled:
            logger.info("[Pipeline] Disabled, returning empty result")
            return {
                "plan": None,
                "results": [],
                "success": False,
                StatusEnum.SKIPPED.value: True,
                "reason": "pipeline_disabled",
            }

        start = time.time()
        logger.info("[Pipeline] Starting 2-stage pipeline for: %s", goal[:80])

        plan = self.create_plan(goal, context)
        results = self.execute_plan(plan, executor_fn)

        elapsed = time.time() - start
        all_success = all(r.get("success") for r in results) if results else False

        return {
            "plan": plan.to_dict(),
            "plan_id": plan.plan_id,
            "results": results,
            "success": all_success,
            "total_steps": plan.step_count(),
            "completed_steps": sum(1 for r in results if r.get("success")),
            "failed_steps": sum(1 for r in results if not r.get("success")),
            "elapsed_seconds": round(elapsed, 2),
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _build_architect_prompt(goal: str, context: dict) -> str:
        """Build the prompt for the architect model."""
        parts = [f"## Goal\n{goal}"]

        if context.get("files"):
            file_list = "\n".join(f"- {f}" for f in context["files"][:50])
            parts.append(f"## Relevant Files\n{file_list}")

        if context.get("tech_stack"):
            parts.append(f"## Tech Stack\n{context['tech_stack']}")

        if context.get("constraints"):
            parts.append(f"## Constraints\n{context['constraints']}")

        if context.get("existing_code"):
            parts.append(f"## Existing Code Context\n{context['existing_code'][:TRUNCATE_OUTPUT_PREVIEW]}")

        parts.append(
            "## Instructions\n"
            "Produce a JSON plan with the following structure:\n"
            "```json\n"
            "{\n"
            '  "steps": [\n'
            '    {"id": "step-1", "description": "...", "files": [...], '
            '"agent_type": "...", "complexity": "low|medium|high"}\n'
            "  ],\n"
            '  "dependencies": {"step-2": ["step-1"]},\n'
            '  "estimated_tokens": 5000\n'
            "}\n"
            "```\n"
            "Keep steps atomic and actionable. Order by dependency.",
        )

        return "\n\n".join(parts)

    @staticmethod
    def _call_model(
        model: str,
        prompt: str,
        system_prompt: str = "",
        max_tokens: int = 2048,
        temperature: float = 0.3,
        context: dict | None = None,
    ) -> str:
        """Call an LLM model and return raw text output.

        Tries adapter_manager from context first, then falls back to
        the local inference adapter.
        """
        context = context or {}

        # Try adapter_manager if available in context
        adapter_manager = context.get("adapter_manager")
        if adapter_manager:
            try:
                from vetinari.adapters.base import InferenceRequest

                req = InferenceRequest(
                    model_id=model,
                    prompt=prompt,
                    system_prompt=system_prompt,
                    max_tokens=max_tokens,
                    temperature=temperature,
                )
                resp = adapter_manager.infer(req)
                if resp.status == INFERENCE_STATUS_OK:
                    return resp.output
            except Exception as e:
                logger.warning("adapter_manager inference failed: %s", e)

        # Fallback: local inference adapter
        adapter = get_local_inference_adapter(model)
        result = adapter.chat(
            model_id=model,
            system_prompt=system_prompt,
            input_text=prompt,
            task_type="coding",
        )
        return result.get("output", "")

    def _parse_architect_output(self, raw_output: str, goal: str) -> ArchitectPlan | None:
        """Parse the architect model's JSON output into an ArchitectPlan."""
        if not raw_output or not raw_output.strip():
            return None

        # Try to extract JSON from the output (may be wrapped in markdown)
        json_str = raw_output.strip()

        # Strip markdown code fences if present
        if "```json" in json_str:
            start = json_str.index("```json") + 7
            end = json_str.index("```", start)
            json_str = json_str[start:end].strip()
        elif "```" in json_str:
            start = json_str.index("```") + 3
            end = json_str.index("```", start)
            json_str = json_str[start:end].strip()

        try:
            data = json.loads(json_str)
        except json.JSONDecodeError:
            logger.warning("[Architect] Could not parse JSON from model output")
            return None

        steps = data.get("steps", [])
        dependencies = data.get("dependencies", {})
        estimated_tokens = data.get("estimated_tokens", 0)

        # Ensure each step has an id
        for i, step in enumerate(steps):
            if "id" not in step:
                step["id"] = f"step-{i + 1}"

        return ArchitectPlan(
            goal=goal,
            steps=steps,
            dependencies=dependencies,
            estimated_tokens=estimated_tokens,
            architect_model=self.architect_model,
        )

    @staticmethod
    def _make_fallback_plan(goal: str, context: dict | None = None) -> ArchitectPlan:
        """Create a simple single-step fallback plan."""
        return ArchitectPlan(
            goal=goal,
            steps=[
                {
                    "id": "step-1",
                    "description": goal,
                    "files": context.get("files", []) if context else [],
                    "agent_type": "general",
                    "complexity": "medium",
                },
            ],
            dependencies={},
            estimated_tokens=0,
            architect_model="fallback",
        )

    def _default_execute_step(self, step: dict[str, Any], plan: ArchitectPlan) -> dict[str, Any]:
        """Default step executor using the executor model."""
        description = step.get("description", "")
        files = step.get("files", [])

        prompt = f"## Task\n{description}"
        if files:
            prompt += "\n\n## Target Files\n" + "\n".join(f"- {f}" for f in files)

        try:
            output = self._call_model(
                model=self.executor_model,
                prompt=prompt,
                system_prompt=(
                    "You are a code executor. Implement the task precisely. Output only the code changes or result."
                ),
                max_tokens=self._config.executor_max_tokens,
                temperature=self._config.executor_temperature,
            )
            return {
                "step_id": step.get("id", "unknown"),
                "success": True,
                "output": output,
                "model": self.executor_model,
            }
        except Exception as e:
            logger.warning(
                "Executor model %r failed to run step %r: %s — step marked as failed, pipeline will attempt recovery",
                self.executor_model,
                step.get("id", "unknown"),
                e,
            )
            return {
                "step_id": step.get("id", "unknown"),
                "success": False,
                "error": str(e),
                "model": self.executor_model,
            }
