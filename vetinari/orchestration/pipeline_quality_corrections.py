"""Goal verification correction-loop helpers for pipeline quality."""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING, Any, cast

from vetinari.types import AgentType

from .pipeline_quality_contracts import _PipelineQualityOwner

if TYPE_CHECKING:
    from vetinari.validation.goal_verifier import GoalVerificationReport

logger = logging.getLogger(__name__)


def _resolve_correction_agent(agent_type_str: str) -> AgentType | None:
    """Resolve a correction task agent label to an AgentType."""
    try:
        return AgentType[agent_type_str]
    except KeyError:
        logger.warning("Exception handled by  resolve correction agent fallback", exc_info=True)
        return AgentType.WORKER
    except Exception:
        logger.warning("Exception handled by  resolve correction agent fallback", exc_info=True)
        return None


class PipelineQualityCorrectionMixin:
    """Goal-verification correction loop methods."""

    def _execute_single_correction(
        self,
        task_dict: dict[str, Any],
        round_num: int,
        context: dict[str, Any],
    ) -> str | None:
        """Run one corrective task and return its output when successful."""
        from vetinari.agents.contracts import AgentTask

        owner = cast(_PipelineQualityOwner, self)
        agent_type_str = task_dict.get("assigned_agent", AgentType.WORKER.value).upper()
        description = task_dict.get("description", "Corrective task")
        task_id = f"correction-r{round_num}-{uuid.uuid4().hex[:8]}"
        agent_type_enum = _resolve_correction_agent(agent_type_str)
        if agent_type_enum is None:
            logger.warning("[CorrectionLoop] Cannot resolve AgentType '%s', skipping task", agent_type_str)
            return None

        agent = owner._get_agent(agent_type_str)
        if agent is None:
            logger.warning("[CorrectionLoop] Agent '%s' unavailable, skipping task", agent_type_str)
            return None

        task = AgentTask(
            task_id=task_id,
            agent_type=agent_type_enum,
            description=description,
            prompt=description,
            context={**context, "correction_round": round_num, "task_details": task_dict.get("details")},
        )
        try:
            result = agent.execute(task)
        except Exception as exc:
            logger.error("[CorrectionLoop] Task %s raised exception: %s", task_id, exc)
            return None
        if result.success and result.output:
            output_str = result.output if isinstance(result.output, str) else str(result.output)
            logger.info("[CorrectionLoop] Task %s completed (agent=%s)", task_id, agent_type_str)
            self._record_successful_correction(task_id, description, task_dict, output_str, context)
            return output_str

        logger.warning("[CorrectionLoop] Task %s failed: %s", task_id, result.errors)
        self._record_failed_correction(task_id, agent_type_str, result.errors)
        return None

    @staticmethod
    def _record_successful_correction(
        task_id: str,
        description: str,
        task_dict: dict[str, Any],
        output_str: str,
        context: dict[str, Any],
    ) -> None:
        """Record learning artifacts from a successful correction task."""
        try:
            from vetinari.learning.training_data import get_training_collector

            get_training_collector().record_preference_pair(
                task=description,
                rejected_response=str(task_dict.get("details", "")),
                accepted_response=output_str,
                task_type="correction",
                model_id=context.get("model_id", "default"),
                pair_type="dpo",
            )
        except Exception:
            logger.warning("DPO preference pair recording failed for correction task %s - non-fatal", task_id)
        try:
            from vetinari.learning.feedback_loop import get_feedback_loop

            get_feedback_loop().record_outcome(
                task_id=task_id,
                model_id=context.get("model_id", "default"),
                task_type="correction",
                quality_score=0.7,
                success=True,
            )
        except Exception as exc:
            logger.warning("Feedback loop record_outcome failed for correction task %s: %s", task_id, exc)

    @staticmethod
    def _record_failed_correction(task_id: str, agent_type_str: str, errors: Any) -> None:
        """Record a correction-task rejection in the feedback loop."""
        try:
            from vetinari.learning.feedback_loop import get_feedback_loop

            get_feedback_loop().record_quality_rejection(
                agent_type=agent_type_str,
                mode="correction",
                violation_description=f"Correction task failed: {errors}",
            )
        except Exception as exc:
            logger.warning("Feedback loop record_quality_rejection failed for correction task %s: %s", task_id, exc)

    def _run_correction_round(
        self,
        corrective_tasks: list[dict[str, Any]],
        round_num: int,
        context: dict[str, Any],
    ) -> list[str]:
        """Execute all corrective tasks for one correction round."""
        round_outputs: list[str] = []
        for task_dict in corrective_tasks:
            output = self._execute_single_correction(task_dict, round_num, context)
            if output is not None:
                round_outputs.append(output)
        return round_outputs

    @staticmethod
    def _verify_corrections(
        verifier: Any,
        *,
        project_id: str,
        goal: str,
        final_output: str,
        required_features: list[str],
        things_to_avoid: list[str],
        task_outputs: list[dict[str, Any]],
    ) -> GoalVerificationReport:
        """Run GoalVerifier after a correction round."""
        return verifier.verify(
            project_id=project_id,
            goal=goal,
            final_output=final_output,
            required_features=required_features,
            things_to_avoid=things_to_avoid,
            task_outputs=task_outputs,
        )

    def _execute_corrections(
        self,
        corrective_tasks: list[dict[str, Any]],
        plan: dict[str, Any],
        goal: str,
        context: dict[str, Any] | None = None,
        max_rounds: int | None = None,
    ) -> GoalVerificationReport:
        """Execute corrective tasks from GoalVerifier and re-verify."""
        from vetinari.validation import get_goal_verifier

        owner = cast(_PipelineQualityOwner, self)
        max_round_count = max_rounds if max_rounds is not None else owner.correction_loop_max_rounds
        context = context or {}
        project_id = plan.get("project_id", "unknown")
        required_features: list[str] = plan.get("required_features", [])
        things_to_avoid: list[str] = plan.get("things_to_avoid", [])
        task_outputs: list[dict[str, Any]] = plan.get("task_outputs", [])
        final_output: str = plan.get("final_output", "")
        verifier = get_goal_verifier()
        report: GoalVerificationReport | None = None

        for round_num in range(1, max_round_count + 1):
            logger.info(
                "[CorrectionLoop] Round %d/%d - executing %d corrective task(s)",
                round_num,
                max_round_count,
                len(corrective_tasks),
            )
            round_outputs = self._run_correction_round(corrective_tasks, round_num, context)
            if round_outputs:
                final_output = final_output + "\n" + "\n".join(round_outputs)
                task_outputs.extend({"output": output, "round": round_num} for output in round_outputs)

            report = self._verify_corrections(
                verifier,
                project_id=project_id,
                goal=goal,
                final_output=final_output,
                required_features=required_features,
                things_to_avoid=things_to_avoid,
                task_outputs=task_outputs,
            )
            logger.info(
                "[CorrectionLoop] Round %d verification: score=%.2f, compliant=%s",
                round_num,
                report.compliance_score,
                report.fully_compliant,
            )
            if report.fully_compliant:
                logger.info("[CorrectionLoop] Verification passed after round %d", round_num)
                return report
            corrective_tasks = report.get_corrective_tasks()
            if not corrective_tasks:
                logger.info("[CorrectionLoop] No further corrective tasks - stopping after round %d", round_num)
                break

        if report is not None:
            return report
        return self._verify_corrections(
            verifier,
            project_id=project_id,
            goal=goal,
            final_output=final_output,
            required_features=required_features,
            things_to_avoid=things_to_avoid,
            task_outputs=task_outputs,
        )
