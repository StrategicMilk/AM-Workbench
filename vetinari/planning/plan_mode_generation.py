"""Plan candidate generation methods for PlanModeEngine."""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from vetinari.boundary_guards import assert_dependency_success, require_nonempty
from vetinari.constants import MAX_TOKENS_PLAN_VARIANT
from vetinari.planning.plan_types import (
    DefinitionOfDone,
    DefinitionOfReady,
    Plan,
    PlanCandidate,
    PlanGenerationRequest,
    PlanRiskLevel,
    PlanStatus,
    StatusEnum,
    Subtask,
    TaskDomain,
)
from vetinari.privacy.envelope import PRIVACY_ENVELOPE_KEY, wrap_for_persistence
from vetinari.security.redaction import redact_text

logger = logging.getLogger(__name__)


def _text_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()


def _text_ref(label: str, value: str) -> str:
    return f"[REDACTED_{label.upper()} sha256={_text_sha256(value)}]"


def _safe_external_text(value: str, label: str) -> str:
    """Return text safe to log or disclose to an unknown inference boundary."""
    redacted = redact_text(value)
    if redacted != value:
        return _text_ref(label, value)
    return value


def _persistable_plan_copy(plan: Plan) -> Plan:
    """Build a privacy-safe copy for durable plan history writes."""
    persisted = copy.deepcopy(plan)
    goal_ref = _text_ref("goal", plan.goal)
    constraints_ref = _text_ref("constraints", plan.constraints) if plan.constraints else ""
    persisted.goal = goal_ref
    persisted.constraints = constraints_ref
    persisted.plan_justification = _safe_external_text(
        persisted.plan_justification.replace(plan.goal, goal_ref).replace(plan.constraints, constraints_ref)
        if plan.constraints
        else persisted.plan_justification.replace(plan.goal, goal_ref),
        "plan_justification",
    )

    replacements = {plan.goal: goal_ref}
    if plan.constraints:
        replacements[plan.constraints] = constraints_ref

    def safe_field(value: str, label: str) -> str:
        safe = value
        for raw, replacement in replacements.items():
            if raw:
                safe = safe.replace(raw, replacement)
        return _safe_external_text(safe, label)

    for candidate in persisted.plan_candidates:
        candidate.summary = safe_field(candidate.summary, "candidate_summary")
        candidate.description = safe_field(candidate.description, "candidate_description")
        candidate.justification = safe_field(candidate.justification, "candidate_justification")

    for subtask in persisted.subtasks:
        subtask.description = safe_field(subtask.description, "subtask_description")
        subtask.expected_output = safe_field(subtask.expected_output, "subtask_expected_output")
        subtask.prompt = safe_field(subtask.prompt, "subtask_prompt")
        subtask.decomposition_seed = safe_field(subtask.decomposition_seed, "subtask_decomposition_seed")

    persisted.plan_explanation_json = json.dumps(
        {
            "goal_sha256": _text_sha256(plan.goal),
            "constraints_sha256": _text_sha256(plan.constraints) if plan.constraints else None,
            PRIVACY_ENVELOPE_KEY: wrap_for_persistence(
                {"plan_id": plan.plan_id, "goal_sha256": _text_sha256(plan.goal)},
                privacy_class="operational",
                source="plan_mode.generate_plan",
                redaction_applied=True,
            )[PRIVACY_ENVELOPE_KEY],
        },
        sort_keys=True,
    )
    return persisted


class _PlanGenerationMixin:
    """Plan generation, domain inference, and subtask construction behavior."""

    if TYPE_CHECKING:
        _domain_templates: Any
        _persist_plan: Any
        dry_run_risk_threshold: Any

    def generate_plan(self, request: PlanGenerationRequest) -> Plan:
        """Generate a plan from a goal.

        Creates multiple plan candidates, evaluates them, and returns a
        Plan object ready for approval or execution. Consults WorkflowLearner
        for domain hints when available.

        Args:
            request: PlanGenerationRequest containing the goal, constraints,
                     domain hint, and other planning parameters.

        Returns:
            A fully constructed Plan with ranked subtasks and risk metadata.
        """
        logger.info("Generating plan for goal_ref=%s", _text_ref("goal", request.goal))

        # Consult WorkflowLearner for recommendations before planning
        workflow_hints: dict[str, Any] = {}
        try:
            from vetinari.learning.workflow_learner import get_workflow_learner

            workflow_hints = get_workflow_learner().get_recommendations(request.goal)
            if workflow_hints.get("confidence", 0) > 0.5:
                logger.info(
                    "WorkflowLearner recommends domain=%s, depth=%s, agents=%s",
                    workflow_hints.get("domain"),
                    workflow_hints.get("recommended_depth"),
                    workflow_hints.get("preferred_agents"),
                )
        except Exception as e:
            logger.warning("WorkflowLearner not available: %s", e)

        plan = Plan(goal=request.goal, constraints=request.constraints, dry_run=request.dry_run, plan_candidates=[])

        domain = request.domain_hint or self._infer_domain(request.goal)

        candidates = self._generate_candidates(
            goal=request.goal,
            constraints=request.constraints,
            domain=domain,
            max_candidates=request.max_candidates,
            depth_cap=request.plan_depth_cap,
        )

        plan.plan_candidates = candidates

        if candidates:
            best_candidate = min(candidates, key=lambda c: c.risk_score)
            plan.chosen_plan_id = best_candidate.plan_id
            plan.plan_justification = best_candidate.justification
            plan.risk_score = best_candidate.risk_score
            plan.risk_level = best_candidate.risk_level
            plan.subtasks = self._create_subtasks_from_candidate(best_candidate, plan.plan_id)
            plan.dependencies = best_candidate.dependencies

        self._finalize_generated_plan(plan, request.dry_run)
        persistence_job_id = "plan_persistence"
        failed_ids: list[str] = []
        if not self._persist_plan(_persistable_plan_copy(plan)):
            failed_ids.append(persistence_job_id)
        assert_dependency_success(persistence_job_id, failed_ids)

        logger.info(
            "Plan generated: %s, risk_score=%.2f, subtasks=%s, auto_approved=%s",
            plan.plan_id,
            plan.risk_score,
            len(plan.subtasks),
            plan.auto_approved,
        )

        persistence_dep_id = persistence_job_id
        assert_dependency_success(persistence_dep_id, failed_ids)
        return plan

    def _finalize_generated_plan(self, plan: Plan, dry_run: bool) -> None:
        plan.calculate_risk_score()
        plan.status = PlanStatus.DRAFT
        if dry_run and plan.risk_score <= self.dry_run_risk_threshold:
            evidence_field = plan.plan_justification
            require_nonempty(evidence_field, field_name="degraded_evidence")
            plan.auto_approved = True
            plan.status = PlanStatus.APPROVED
            plan.approved_by = "system_auto"
            plan.approved_at = datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _infer_domain(goal: str) -> TaskDomain:
        """Infer the domain from the goal text using keyword matching.

        Args:
            goal: Free-text goal description.

        Returns:
            The most likely TaskDomain for the goal.
        """
        goal_lower = goal.lower()

        if any(kw in goal_lower for kw in ["code", "implement", "build", "feature", "api", "function"]):
            return TaskDomain.CODING
        if any(kw in goal_lower for kw in ["etl", "data", "pipeline", "process", "transform"]):
            return TaskDomain.DATA_PROCESSING
        if any(kw in goal_lower for kw in ["infra", "deploy", "monitor", "logging", "ci/cd"]):
            return TaskDomain.INFRA
        if any(kw in goal_lower for kw in ["document", "docs", "write", "guide"]):
            return TaskDomain.DOCS
        if any(kw in goal_lower for kw in ["experiment", "model", "test", "benchmark", "evaluate"]):
            return TaskDomain.AI_EXPERIMENTS
        if any(kw in goal_lower for kw in ["research", "analyze", "study", "investigate"]):
            return TaskDomain.RESEARCH
        return TaskDomain.GENERAL

    def _generate_candidates(
        self,
        goal: str,
        constraints: str,
        domain: TaskDomain,
        max_candidates: int,
        depth_cap: int,
    ) -> list[PlanCandidate]:
        """Generate multiple plan candidates, using LLM when available.

        Args:
            goal: The goal description.
            constraints: Any constraints on the plan.
            domain: The inferred or specified task domain.
            max_candidates: Maximum number of candidates to generate.
            depth_cap: Maximum plan depth allowed.

        Returns:
            List of PlanCandidate objects sorted from lowest to highest risk.
        """
        templates = self._domain_templates.get(domain, self._domain_templates[TaskDomain.GENERAL])

        # Gather quality history for calibrated estimates
        quality_context = ""
        try:
            from vetinari.learning.quality_scorer import get_quality_scorer

            scorer = get_quality_scorer()
            for tpl in templates:
                task_type = tpl.get("task_type", domain.value) if isinstance(tpl, dict) else domain.value
                history = scorer.get_history(task_type=task_type)
                if history:
                    recent = history[:5]
                    avg_score = sum(h.overall_score for h in recent) / len(recent)
                    quality_context += f"\n- {task_type}: avg quality={avg_score:.2f} over {len(recent)} recent tasks"
        except Exception:  # Quality history is enrichment only; proceed without it
            logger.warning("Quality scorer history unavailable for domain %s", domain.value, exc_info=True)

        # Try LLM-powered candidate generation
        try:
            from vetinari.adapter_manager import get_adapter_manager
            from vetinari.adapters.base import InferenceRequest

            adapter = get_adapter_manager()
            quality_section = f"\n\nHistorical quality data:{quality_context}" if quality_context else ""
            prompt_text = (
                f"Generate {min(max_candidates, 3)} plan variants for this goal:\n"
                f"Goal: {_safe_external_text(goal, 'goal')}\n"
                f"Domain: {domain.value}\n"
                f"Constraints: {_safe_external_text(constraints, 'constraints') if constraints else 'none'}\n"
                f"{quality_section}\n\n"
                f"For each variant, provide on a single line: summary|risk(0.0-1.0)|hours|cost_usd|subtask_count\n"
                f"Variant 1 should be conservative (low risk), variant 2 balanced, variant 3 aggressive (fast but riskier)."
            )
            request = InferenceRequest(
                model_id="",  # Let adapter pick first available
                prompt=prompt_text,
                system_prompt="You are a project planner. Output exactly the requested format, one variant per line.",
                max_tokens=MAX_TOKENS_PLAN_VARIANT,
            )
            response = adapter.infer(request)
            if hasattr(response, "status") and response.status == "error":
                logger.debug("LLM inference returned error: %s", getattr(response, "error", "unknown"))
                raise RuntimeError(getattr(response, "error", "inference error"))
            content = response.output.strip() if hasattr(response, "output") else ""
            if content:
                return self._parse_llm_candidates(content, goal, domain, depth_cap, templates, max_candidates)
        except Exception as e:
            logger.warning("LLM candidate generation unavailable, using fallback: %s", e)

        # Fallback: hardcoded candidate generation
        return self._generate_fallback_candidates(goal, domain, templates, max_candidates, depth_cap)

    def _parse_llm_candidates(
        self,
        content: str,
        goal: str,
        domain: TaskDomain,
        depth_cap: int,
        templates: list,
        max_candidates: int,
    ) -> list[PlanCandidate]:
        """Parse LLM output into PlanCandidate objects.

        Args:
            content: Raw LLM output with one pipe-delimited variant per line.
            goal: The original goal string.
            domain: The task domain.
            depth_cap: Maximum plan depth.
            templates: Domain subtask templates for fallback subtask count.
            max_candidates: Maximum candidates to produce.

        Returns:
            List of parsed PlanCandidate objects.
        """
        candidates = []
        for i, line in enumerate(content.strip().split("\n")):
            if i >= min(max_candidates, 3):
                break
            parts = [p.strip() for p in line.split("|")]
            if len(parts) >= 5:
                try:
                    summary = parts[0]
                    _model_supplied_risk = max(0.0, min(1.0, float(parts[1])))
                    hours = max(0.5, float(parts[2]))
                    cost = max(1.0, float(parts[3]))
                    subtasks = max(1, int(float(parts[4])))
                except (ValueError, IndexError):
                    logger.warning(
                        "Could not parse plan cost/subtask line — skipping malformed entry, variant may use fallback values"
                    )
                    continue
            else:
                # Couldn't parse — use fallback values for this variant
                summary = f"Plan variant {i + 1} for: {goal[:50]}..."
                hours = 1.0 + i * 0.5
                cost = 10.0 * (1 + i * 0.3)
                subtasks = len(templates) + i * 2

            max_depth = min(depth_cap, 3 + i)
            risk = self._derive_candidate_risk(
                domain=domain,
                subtask_count=subtasks,
                max_depth=max_depth,
                variant_index=i,
            )
            candidate = PlanCandidate(
                plan_id=f"plan_{uuid.uuid4().hex[:8]}",
                plan_version=1,
                summary=summary,
                description=f"Implementation plan for: {goal}",
                justification=f"LLM-analyzed {domain.value} plan variant",
                risk_score=risk,
                estimated_duration_seconds=hours * 3600.0,
                estimated_cost=cost,
                subtask_count=subtasks,
                max_depth=max_depth,
                domains=[domain],
            )
            self._assign_risk_level(candidate)
            candidate.dependencies = self._generate_dependencies(subtasks)
            candidates.append(candidate)

        # If parsing failed entirely, fall back
        if not candidates:
            return self._generate_fallback_candidates(goal, domain, templates, max_candidates, depth_cap)
        candidates.sort(key=lambda candidate: candidate.risk_score)
        return candidates

    @staticmethod
    def _derive_candidate_risk(
        *,
        domain: TaskDomain,
        subtask_count: int,
        max_depth: int,
        variant_index: int,
    ) -> float:
        """Derive candidate risk from local plan shape, not model-supplied scores."""
        domain_base = {
            TaskDomain.CODING: 0.24,
            TaskDomain.DATA_PROCESSING: 0.30,
            TaskDomain.INFRA: 0.40,
            TaskDomain.AI_EXPERIMENTS: 0.38,
            TaskDomain.RESEARCH: 0.22,
            TaskDomain.DOCS: 0.16,
            TaskDomain.GENERAL: 0.25,
        }.get(domain, 0.25)
        breadth = min(0.25, max(0, subtask_count - 3) * 0.025)
        depth = min(0.20, max(0, max_depth - 2) * 0.05)
        aggression = min(0.15, max(0, variant_index) * 0.06)
        return round(max(0.0, min(1.0, domain_base + breadth + depth + aggression)), 3)

    def _generate_fallback_candidates(
        self,
        goal: str,
        domain: TaskDomain,
        templates: list,
        max_candidates: int,
        depth_cap: int,
    ) -> list[PlanCandidate]:
        """Generate candidates with hardcoded heuristics (no LLM required).

        Args:
            goal: The original goal string.
            domain: The task domain.
            templates: Domain subtask templates used for subtask count.
            max_candidates: Maximum candidates to generate.
            depth_cap: Maximum plan depth.

        Returns:
            List of PlanCandidate objects (up to min(max_candidates, 3)).
        """
        candidates = []
        for i in range(min(max_candidates, 3)):
            candidate = PlanCandidate(
                plan_id=f"plan_{uuid.uuid4().hex[:8]}",
                plan_version=1,
                summary=f"Plan variant {i + 1} for: {goal[:50]}...",
                description=f"Implementation plan for: {goal}",
                justification=f"Generated based on {domain.value} domain patterns",
                risk_score=0.15 + (i * 0.1),
                estimated_duration_seconds=3600.0 * (1 + i * 0.5),
                estimated_cost=10.0 * (1 + i * 0.3),
                subtask_count=len(templates) + i * 2,
                max_depth=min(depth_cap, 3 + i),
                domains=[domain],
            )
            self._assign_risk_level(candidate)
            candidate.dependencies = self._generate_dependencies(len(templates) + i * 2)
            candidates.append(candidate)
        return candidates

    @staticmethod
    def _assign_risk_level(candidate: PlanCandidate) -> None:
        """Set risk_level based on risk_score thresholds.

        Args:
            candidate: PlanCandidate to update in place.
        """
        if candidate.risk_score >= 0.75:
            candidate.risk_level = PlanRiskLevel.CRITICAL
        elif candidate.risk_score >= 0.5:
            candidate.risk_level = PlanRiskLevel.HIGH
        elif candidate.risk_score >= 0.25:
            candidate.risk_level = PlanRiskLevel.MEDIUM
        else:
            candidate.risk_level = PlanRiskLevel.LOW

    @staticmethod
    def _generate_dependencies(subtask_count: int) -> dict[str, list[str]]:
        """Generate sequential dependency chains for a set of subtasks.

        Every third subtask depends on the one before it; others have no
        dependencies. This produces a branching plan structure suitable
        for parallel execution.

        Args:
            subtask_count: Number of subtasks to generate dependencies for.

        Returns:
            Dict mapping subtask IDs to their dependency ID lists.
        """
        deps = {}
        for i in range(subtask_count):
            task_id = f"subtask_{i:03d}"
            if i > 0 and i % 3 == 0:
                deps[task_id] = [f"subtask_{i - 1:03d}"]
            else:
                deps[task_id] = []
        return deps

    def _create_subtasks_from_candidate(self, candidate: PlanCandidate, plan_id: str) -> list[Subtask]:
        """Create Subtask objects from a plan candidate's domain templates.

        Args:
            candidate: The chosen plan candidate.
            plan_id: The parent plan ID.

        Returns:
            List of Subtask objects with time/cost estimates distributed
            evenly across the template steps.
        """
        subtasks = []

        domain = candidate.domains[0] if candidate.domains else TaskDomain.GENERAL
        templates = self._domain_templates.get(domain, [])

        n_templates = len(templates) if templates else 1
        for i, template in enumerate(templates):
            subtask = Subtask(
                subtask_id=f"subtask_{i:03d}",
                plan_id=plan_id,
                description=template.get("description", f"Task {i + 1}"),
                domain=template.get("domain", domain),
                depth=0,
                status=StatusEnum.PENDING,
                definition_of_done=template.get("definition_of_done", DefinitionOfDone()),
                definition_of_ready=template.get("definition_of_ready", DefinitionOfReady()),
                time_estimate_seconds=candidate.estimated_duration_seconds / n_templates,
                cost_estimate=candidate.estimated_cost / n_templates,
            )
            subtasks.append(subtask)

        return subtasks
