"""Output review and final assembly helpers for pipeline quality."""

from __future__ import annotations

import logging
from typing import Any, cast

from vetinari.guards import GateError
from vetinari.ontology import QUALITY_THRESHOLD_PASS
from vetinari.types import AgentType

from .pipeline_quality_contracts import _PipelineQualityOwner

logger = logging.getLogger(__name__)


def _review_context(exec_results: dict[str, Any], context: dict[str, Any] | None) -> dict[str, Any]:
    """Build the Inspector review context from task artifacts and project metadata."""
    task_results = exec_results.get("task_results", {})
    review_ctx: dict[str, Any] = {
        "artifacts": [str(v) for v in task_results.values() if v][:5],
        "focus": "all",
        "mode": "code_review",
    }
    if context:
        for key in ("required_features", "things_to_avoid", "expected_outputs", "tech_stack", "category"):
            if key in context:
                review_ctx[key] = context[key]
    return review_ctx


def _fallback_review() -> dict[str, Any]:
    return {
        "verdict": "inconclusive",
        "quality_score": 0.5,
        "passed": False,
        "summary": "Review skipped (quality agent unavailable)",
    }


class PipelineQualityReviewMixin:
    """Inspector review and worker final-assembly methods."""

    def _run_inspector_review(
        self,
        exec_results: dict[str, Any],
        goal: str,
        context: dict[str, Any] | None,
    ) -> tuple[dict[str, Any], dict[str, Any]] | None:
        """Execute InspectorAgent and return its review with the review context."""
        owner = cast(_PipelineQualityOwner, self)
        quality = owner._get_agent(AgentType.INSPECTOR.value)
        if not quality:
            return None
        from vetinari.agents.contracts import AgentTask

        review_ctx = _review_context(exec_results, context)
        eval_task = AgentTask(
            task_id="review-0",
            agent_type=AgentType.INSPECTOR,
            description=f"Review outputs for goal: {goal}",
            prompt=f"Review outputs for goal: {goal}",
            context=review_ctx,
        )
        result = quality.execute(eval_task)
        if not result.success:
            return None
        review = result.output if isinstance(result.output, dict) else {}
        if "passed" not in review:
            review["passed"] = len(review.get("issues", [])) == 0
        return review, review_ctx

    @staticmethod
    def _apply_prevention_rules(exec_results: dict[str, Any], review: dict[str, Any]) -> None:
        """Reject otherwise-passing output when a prevention rule matches."""
        if not review.get("passed"):
            return
        try:
            from vetinari.analytics.failure_registry import get_failure_registry

            output_text = str(exec_results.get("task_results", {}))[:5000]
            for rule in get_failure_registry().get_prevention_rules():
                if rule.matches(output_text):
                    review["passed"] = False
                    review.setdefault("issues", []).append(
                        f"Prevention rule {rule.rule_id} matched: {rule.description}"
                    )
                    logger.warning(
                        "[PreventionRule] Rule %s (%s) matched output - rejecting", rule.rule_id, rule.category
                    )
                    break
        except Exception as exc:
            logger.error("Prevention rule check failed - failure registry unavailable", exc_info=True)
            raise GateError("prevention_rules", "failure registry unavailable", exc) from exc

    def _record_review_quality(
        self,
        exec_results: dict[str, Any],
        goal: str,
        review: dict[str, Any],
        context: dict[str, Any] | None,
    ) -> None:
        """Record scorer, feedback-loop, unknown-family, and prompt-evolver signals."""
        model_id = context.get("model_id", "default") if context else "default"
        task_type = context.get("task_type", "general") if context else "general"
        try:
            from vetinari.learning.quality_scorer import get_quality_scorer

            quality_result = get_quality_scorer().score(
                task_id="review-0",
                model_id=model_id,
                task_type=task_type,
                task_description=goal,
                output=str(exec_results.get("task_results", {})),
            )
            review["quality_score"] = quality_result.overall_score
        except Exception as exc:
            logger.warning("Quality scoring failed during output review - quality_score unchanged: %s", exc)
        self._record_review_feedback(review, context, model_id, task_type)

    @staticmethod
    def _record_review_feedback(
        review: dict[str, Any],
        context: dict[str, Any] | None,
        model_id: str,
        task_type: str,
    ) -> None:
        """Record non-blocking review telemetry in learning and analytics subsystems."""
        try:
            from vetinari.learning.feedback_loop import get_feedback_loop

            get_feedback_loop().record_outcome(
                task_id="review-0",
                model_id=model_id,
                task_type=task_type,
                quality_score=review.get("quality_score", 0.5),
                success=review.get("passed", False),
            )
        except Exception as exc:
            logger.warning("Feedback loop record_outcome failed during output review - feedback not recorded: %s", exc)
        try:
            from vetinari.analytics.wiring import record_unknown_family_task_result

            record_unknown_family_task_result(
                model_id=model_id,
                architecture=model_id,
                quality_score=review.get("quality_score", 0.5),
            )
        except Exception:
            logger.warning("Unknown-family task recording skipped - non-fatal")
        try:
            from vetinari.learning.prompt_evolver import get_prompt_evolver

            variant_id = context.get("prompt_variant_id") if context else None
            agent_type = context.get("agent_type", "worker") if context else "worker"
            if variant_id and variant_id not in ("none", "default"):
                get_prompt_evolver().record_result(
                    agent_type=agent_type,
                    variant_id=variant_id,
                    quality=review.get("quality_score", 0.5),
                )
        except Exception as exc:
            logger.warning("PromptEvolver quality update failed during output review - non-fatal: %s", exc)

    @staticmethod
    def _record_training_sample(
        exec_results: dict[str, Any],
        goal: str,
        review: dict[str, Any],
        context: dict[str, Any] | None,
    ) -> None:
        """Persist the reviewed output as training data with rejection metadata."""
        try:
            from vetinari.learning.training_data import get_training_collector

            rejection_kwargs: dict[str, Any] = {}
            if not review.get("passed"):
                rejection_issues = review.get("issues", [])
                rejection_kwargs["rejection_reason"] = (
                    "; ".join(str(i) for i in rejection_issues[:3])
                    if rejection_issues
                    else review.get("summary", "Inspector rejected output")
                )
                rejection_kwargs["rejection_category"] = review.get("verdict", "quality_rejection")
                rejection_kwargs["inspector_feedback"] = review.get("summary", "")
            get_training_collector().record(
                task=goal,
                prompt=goal,
                response=str(exec_results.get("task_results", {}))[:2000],
                score=review.get("quality_score", 0.5),
                model_id=context.get("model_id", "default") if context else "default",
                task_type=context.get("task_type", "general") if context else "general",
                latency_ms=context.get("latency_ms", 1) if context else 1,
                tokens_used=context.get("tokens_used", 1) if context else 1,
                success=review.get("passed", False),
                **rejection_kwargs,
            )
        except Exception as exc:
            logger.warning("Training data collection failed during output review - record not saved: %s", exc)

    @staticmethod
    def _try_self_refinement(
        exec_results: dict[str, Any],
        goal: str,
        review: dict[str, Any],
        review_ctx: dict[str, Any],
        context: dict[str, Any] | None,
    ) -> None:
        """Attempt one self-refinement path for rejected output."""
        if review.get("passed", True):
            return
        try:
            from vetinari.learning.self_refinement import get_self_refiner

            refinement = get_self_refiner().refine(
                task_description=goal,
                initial_output=str(exec_results.get("task_results", {})),
                task_type=review_ctx.get("mode", "general"),
                model_id=context.get("model_id", "default") if context else "default",
                importance=0.8,
                initial_quality=review.get("quality_score", 0.5),
            )
            if refinement.improved:
                review["refinement_applied"] = True
                review["refinement_rounds"] = refinement.rounds_used
                if refinement.final_quality >= QUALITY_THRESHOLD_PASS:
                    review["passed"] = True
                    review["quality_score"] = refinement.final_quality
        except Exception:
            logger.warning("Self-refinement failed during output review - non-fatal", exc_info=True)

    @staticmethod
    def _record_review_failure(review: dict[str, Any]) -> None:
        """Record final Inspector rejection in the failure registry."""
        if review.get("passed", True):
            return
        try:
            from vetinari.analytics.wiring import record_failure

            rejection_issues = review.get("issues", [])
            record_failure(
                category="inspector_rejection",
                severity="warning",
                description=review.get("summary", "Inspector rejected output"),
                root_cause="; ".join(str(i) for i in rejection_issues[:3]),
                affected_components=["inspector", "worker"],
            )
        except Exception:
            logger.warning("Failure registry logging skipped during output review - non-fatal")

    def _review_outputs(
        self,
        exec_results: dict[str, Any],
        goal: str,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Use InspectorAgent to review execution outputs for quality."""
        try:
            inspector_result = self._run_inspector_review(exec_results, goal, context)
            if inspector_result is None:
                return _fallback_review()
            review, review_ctx = inspector_result
            self._apply_prevention_rules(exec_results, review)
            self._record_review_quality(exec_results, goal, review, context)
            self._record_training_sample(exec_results, goal, review, context)
            self._try_self_refinement(exec_results, goal, review, review_ctx, context)
            self._record_review_failure(review)
            return review
        except GateError:
            raise
        except Exception as exc:
            logger.warning("Output review failed: %s", exc)
            return _fallback_review()

    def _assemble_final_output(
        self,
        exec_results: dict[str, Any],
        review_result: dict[str, Any],
        goal: str,
    ) -> str:
        """Use Worker synthesis mode to assemble a final coherent output."""
        try:
            owner = cast(_PipelineQualityOwner, self)
            operations = owner._get_agent(AgentType.WORKER.value)
            if operations:
                from vetinari.agents.contracts import AgentTask

                task_results = exec_results.get("task_results", {})
                sources = [{"agent": k, "artifact": str(v)[:500]} for k, v in task_results.items() if v]
                sources.append({"agent": "review", "artifact": str(review_result)[:200]})
                synth_task = AgentTask(
                    task_id="assemble-0",
                    agent_type=AgentType.WORKER,
                    description=f"Final assembly for goal: {goal}",
                    prompt=f"Final assembly for goal: {goal}",
                    context={"sources": sources, "type": "final_report", "mode": "synthesis"},
                )
                result = operations.execute(synth_task)
                if result.success and result.output:
                    return cast(str, result.output.get("synthesized_artifact", str(result.output)))
        except Exception as exc:
            logger.warning("Final assembly failed: %s", exc)

        task_results = exec_results.get("task_results", {})
        parts = [f"# Task {k}\n{v}" for k, v in task_results.items() if v]
        return "\n\n".join(parts) if parts else f"Completed: {goal}"
