"""Default specialist model bindings for Workbench agents."""

from __future__ import annotations

from vetinari.workbench.specialists.cards import SpecialistModelCard, SpecialistTask


def default_specialist_cards() -> tuple[SpecialistModelCard, ...]:
    """Return the built-in specialist bindings used by agents as typed tools."""
    return (
        _card(
            "specialist.failure-cause.v1",
            SpecialistTask.FAILURE_CAUSE_CLASSIFICATION,
            allowed_callers=("inspector.run-review", "workbench.failure-intelligence"),
            fallback_behavior="preserve_unknown_failure_label",
            known_failure_modes=("novel_failure_mode", "insufficient_trace_context"),
            threshold=0.8,
        ),
        _card(
            "specialist.prompt-injection.v1",
            SpecialistTask.PROMPT_INJECTION_DETECTION,
            allowed_callers=("gateway.policy", "inspector.security"),
            fallback_behavior="block_and_request_manual_review",
            known_failure_modes=("obfuscated_instruction", "benign_security_discussion"),
            threshold=0.9,
        ),
        _card(
            "specialist.source-quality.v1",
            SpecialistTask.SOURCE_QUALITY_CLASSIFICATION,
            allowed_callers=("workbench.source-health", "inspector.evidence-review"),
            fallback_behavior="require_human_source_review",
            known_failure_modes=("stale_source_metadata", "ambiguous_primary_source"),
            threshold=0.84,
        ),
        _card(
            "specialist.retrieval-reranker.v1",
            SpecialistTask.RETRIEVAL_RERANKING,
            allowed_callers=("workbench.rag-debugger", "workbench.retrieval"),
            fallback_behavior="use_default_retrieval_order",
            known_failure_modes=("sparse_query_context", "domain_shifted_corpus"),
            threshold=0.78,
        ),
        _card(
            "specialist.plan-quality.v1",
            SpecialistTask.PLAN_QUALITY_DISCRIMINATION,
            allowed_callers=("foreman.plan-review", "inspector.plan-quality"),
            fallback_behavior="route_to_plan_reviewer",
            known_failure_modes=("underspecified_acceptance", "stale_plan_context"),
            threshold=0.86,
        ),
        _card(
            "specialist.route-classifier.v1",
            SpecialistTask.ROUTE_CLASSIFICATION,
            allowed_callers=("foreman.router", "workbench.route-policy"),
            fallback_behavior="use_existing_router_policy",
            known_failure_modes=("multi_intent_request", "missing_tool_health_context"),
            threshold=0.82,
        ),
    )


def _card(
    card_id: str,
    task: SpecialistTask,
    *,
    allowed_callers: tuple[str, ...],
    fallback_behavior: str,
    known_failure_modes: tuple[str, ...],
    threshold: float,
) -> SpecialistModelCard:
    slug = task.value.replace("_", "-")
    return SpecialistModelCard(
        card_id=card_id,
        model_ref=f"model-registry:{card_id}",
        task=task,
        task_contract=f"typed specialist contract for {task.value}",
        input_schema_ref=f"schema:workbench-specialist-input:{slug}",
        output_schema_ref=f"schema:workbench-specialist-output:{slug}",
        confidence_calibration_ref=f"calibration:{slug}@v1",
        abstain_threshold=threshold,
        allowed_callers=allowed_callers,
        fallback_behavior=fallback_behavior,
        eval_suite_ref=f"eval-suite:{slug}@v1",
        known_failure_modes=known_failure_modes,
        safety_ref=f"safety:{slug}-boundary",
        budget_ref="budget:utility-model-small",
        authority_ref="authority:agent-specialist-model-tooling",
        provenance_ref=f"spine:{slug}-specialist",
        persisted_state_ref=f"state:{slug}-specialist",
    )


__all__ = ["default_specialist_cards"]
