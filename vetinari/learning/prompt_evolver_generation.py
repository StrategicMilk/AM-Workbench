"""Prompt Evolver generation helpers — LLM/operator variant generation and advanced evolution.

Extracted from PromptEvolver to keep that class within the 550-line limit.
Functions accept an ``evolver: PromptEvolver`` as their first argument and
operate on its internal state directly.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from vetinari.learning.prompt_evolver import PromptEvolver

from vetinari.constants import DEFAULT_TEMPERATURE, INFERENCE_STATUS_OK, MAX_TOKENS_PROMPT_MUTATION
from vetinari.types import PromptVersionStatus

logger = logging.getLogger(__name__)


__all__ = [
    "evolve_per_level",
    "generate_variant_from_trace",
    "generate_variant_llm",
    "get_baseline_quality",
    "get_operator_selector",
    "get_prompt_mutator",
    "record_improvement",
    "synthesize_scope_guidelines",
    "update_improvement_observation",
    "update_operator_feedback",
]


def generate_variant_llm(evolver: PromptEvolver, agent_type: str, baseline_prompt: str) -> str | None:
    """Generate a variant using LLM inference (fallback path).

    Args:
        evolver: PromptEvolver instance with _adapter_manager, _lock, _variants, _save_variants.
        agent_type: The agent type.
        baseline_prompt: The baseline prompt.

    Returns:
        The generated variant text, or None on failure.
    """
    from vetinari.learning.prompt_evolver_models import PromptVariant

    if not evolver._adapter_manager:
        return None

    try:
        from vetinari.adapters.base import InferenceRequest

        model_id = str(
            getattr(evolver._adapter_manager, "prompt_mutation_model_id", "")
            or getattr(evolver._adapter_manager, "default_model_id", "")
            or getattr(evolver, "_prompt_mutation_model_id", "")
            or ""
        ).strip()
        if not model_id:
            logger.warning("LLM variant generation skipped for %s: no prompt mutation model configured", agent_type)
            return None
        req = InferenceRequest(
            model_id=model_id,
            prompt=f"""You are a prompt engineer. Improve this agent system prompt to be more effective.

AGENT TYPE: {agent_type}

CURRENT PROMPT:
{baseline_prompt}

Generate an improved version that:
1. Is more specific and actionable
2. Gives clearer output format instructions
3. Reduces ambiguity
4. Maintains the same core role

Respond with ONLY the improved prompt text, no explanations.""",
            system_prompt="You are an expert AI prompt engineer.",
            max_tokens=MAX_TOKENS_PROMPT_MUTATION,
            temperature=DEFAULT_TEMPERATURE,
        )
        resp = evolver._adapter_manager.infer(req)
        if resp.status == INFERENCE_STATUS_OK and resp.output.strip():
            with evolver._lock:
                variant_id = f"{agent_type}_v{len(evolver._variants.get(agent_type, [])) + 1}"
                variant = PromptVariant(
                    variant_id=variant_id,
                    agent_type=agent_type,
                    prompt_text=resp.output.strip(),
                )
                if agent_type not in evolver._variants:
                    evolver._variants[agent_type] = []
                evolver._variants[agent_type].append(variant)
                evolver._save_variants()
            logger.info("[PromptEvolver] Generated variant %s for %s via LLM", variant_id, agent_type)
            return resp.output.strip()
    except Exception as exc:
        logger.warning("LLM variant generation failed for %s — returning None: %s", agent_type, exc)
    return None


def get_operator_selector(evolver: PromptEvolver):
    """Return the shared OperatorSelector singleton, caching on the evolver.

    Args:
        evolver: PromptEvolver instance with _operator_selector attribute.

    Returns:
        The shared OperatorSelector instance.
    """
    if evolver._operator_selector is None:
        from vetinari.learning.operator_selector import get_operator_selector as _get

        evolver._operator_selector = _get()
    return evolver._operator_selector


def get_prompt_mutator(evolver: PromptEvolver):
    """Lazy-load the PromptMutator singleton onto the evolver.

    Args:
        evolver: PromptEvolver instance with _prompt_mutator attribute.

    Returns:
        The PromptMutator instance.
    """
    if evolver._prompt_mutator is None:
        from vetinari.learning.prompt_mutator import PromptMutator

        evolver._prompt_mutator = PromptMutator()
    return evolver._prompt_mutator


def update_operator_feedback(evolver: PromptEvolver, variant_id: str, quality_delta: float) -> None:
    """Feed quality delta back to the OperatorSelector for the variant's operator.

    Args:
        evolver: PromptEvolver instance with _variant_operators and _improvement_log.
        variant_id: The variant that was tested.
        quality_delta: Quality improvement (positive = better).
    """
    op_info = evolver._variant_operators.get(variant_id)
    if op_info is None:
        return  # Not an operator-generated variant (LLM fallback path)

    operator, agent_type, mode = op_info
    try:
        selector = get_operator_selector(evolver)
        selector.update(operator, agent_type, mode, quality_delta)
        logger.info(
            "[PromptEvolver] Updated operator %s feedback for %s/%s: delta=%.3f",
            operator.value,
            agent_type,
            mode,
            quality_delta,
        )
    except Exception as exc:
        logger.warning("Failed to update operator feedback for %s — skipping: %s", variant_id, exc)

    update_improvement_observation(evolver, variant_id, quality_delta)


def record_improvement(evolver: PromptEvolver, variant_id: str, operator: Any, agent_type: str, mode: str) -> None:
    """Create an ImprovementRecord in the ImprovementLog for a new variant.

    Each operator application is tracked as a kaizen improvement so the
    PDCA cycle can observe and evaluate the mutation's impact.

    Args:
        evolver: PromptEvolver instance with _improvement_log and _variant_improvements.
        variant_id: The variant that was created.
        operator: The MutationOperator that produced the variant.
        agent_type: The agent type the variant targets.
        mode: The agent mode context.
    """
    if evolver._improvement_log is None:
        return

    try:
        baseline_val = get_baseline_quality(evolver, agent_type)
        improvement_id = evolver._improvement_log.propose(
            hypothesis=(f"Operator {operator.value} improves prompt quality for {agent_type}/{mode}"),
            metric="prompt_quality",
            baseline=baseline_val,
            target=baseline_val + evolver.MIN_IMPROVEMENT,
            applied_by="PromptEvolver",
            rollback_plan=f"Deprecate variant {variant_id} and revert to baseline",
        )
        evolver._improvement_log.activate(improvement_id)
        evolver._variant_improvements[variant_id] = improvement_id
        logger.info("[PromptEvolver] Created improvement %s for variant %s", improvement_id, variant_id)
    except Exception as exc:
        logger.warning("Failed to record improvement for variant %s — skipping: %s", variant_id, exc)


def get_baseline_quality(evolver: PromptEvolver, agent_type: str) -> float:
    """Get the current baseline quality for an agent type.

    Args:
        evolver: PromptEvolver instance with _variants.
        agent_type: The agent type to look up.

    Returns:
        The baseline average quality, or 0.65 if no baseline exists.
    """
    variants = evolver._variants.get(agent_type, [])
    baseline = next(
        (v for v in variants if v.is_baseline and v.status == PromptVersionStatus.PROMOTED.value),
        None,
    )
    if baseline and baseline.trials > 0:
        return baseline.avg_quality
    return 0.65  # Default prior


def update_improvement_observation(evolver: PromptEvolver, variant_id: str, quality_delta: float) -> None:
    """Record an observation on the linked ImprovementRecord after A/B test.

    Args:
        evolver: PromptEvolver instance with _improvement_log and _variant_improvements.
        variant_id: The variant whose A/B test completed.
        quality_delta: Quality improvement (positive = better).
    """
    if evolver._improvement_log is None:
        return

    improvement_id = evolver._variant_improvements.get(variant_id)
    if improvement_id is None:
        return

    try:
        agent_type = evolver._variant_operators.get(variant_id, (None, "", ""))[1]
        baseline_quality = get_baseline_quality(evolver, agent_type)
        actual_quality = baseline_quality + quality_delta
        evolver._improvement_log.observe(
            improvement_id=improvement_id,
            metric_value=actual_quality,
            sample_size=1,
        )
        evolver._improvement_log.evaluate(improvement_id)
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "[PromptEvolver] Recorded observation for improvement %s: actual=%.3f",
                improvement_id,
                actual_quality,
            )
    except Exception as exc:
        logger.warning("Failed to update improvement observation for %s — skipping: %s", variant_id, exc)


def generate_variant_from_trace(evolver: PromptEvolver, trace: dict[str, Any]) -> str | None:
    """Derive an improved prompt variant from a failed execution trace.

    Extracts agent_type and prompt from the trace, then calls evolver.generate_variant.
    Returns None when the trace lacks required fields.

    Args:
        evolver: PromptEvolver instance used to call generate_variant.
        trace: A trace dict with at least "prompt" and optionally "agent_type" and "mode".

    Returns:
        Evolved prompt text, or None if generation fails.
    """
    baseline = trace.get("prompt", "")
    if not baseline:
        return None
    agent_type = trace.get("agent_type", "general")
    mode = trace.get("mode", "default")
    return evolver.generate_variant(agent_type, baseline, mode)


def evolve_per_level(
    evolver: PromptEvolver,
    agent_type: str,
    mode: str = "default",
    failed_traces: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Run one evolution step for a specific agent type and mode.

    Selects the promoted baseline for agent_type, optionally uses failed_traces
    for trace-guided mutation, then falls back to blind mutation when trace
    evolution returns the same text as the baseline.

    Agent-specific context hints are injected into the baseline before mutation:
    Worker prompts receive the mode name; Inspector prompts receive a reminder
    about reviewing Worker output.

    Args:
        evolver: PromptEvolver instance used to call generate_variant and generate_variant_from_trace.
        agent_type: Agent type string (e.g. AgentType.WORKER.value).
        mode: Execution mode context forwarded to the mutation operator.
        failed_traces: Optional list of failed trace dicts to guide mutation.

    Returns:
        Dict with keys: variant_id (str or None) and evolved_prompt (str or None).
    """
    variants = evolver._variants.get(agent_type, [])
    baseline_variant = next(
        (v for v in variants if v.is_baseline and v.status == PromptVersionStatus.PROMOTED.value),
        None,
    )
    if baseline_variant is None:
        return {"variant_id": None, "evolved_prompt": None}

    baseline_prompt = baseline_variant.prompt_text

    if agent_type == "WORKER":
        hint_seed = f"{baseline_prompt}\n\n[Mode: {mode}]"
    elif agent_type == "INSPECTOR":
        hint_seed = f"{baseline_prompt}\n\n[Review Worker output carefully for {mode} mode failures.]"
    else:
        hint_seed = baseline_prompt

    if failed_traces:
        trace = failed_traces[0]
        enriched_trace = {**trace, "prompt": hint_seed, "agent_type": agent_type, "mode": mode}
        trace_result = evolver.generate_variant_from_trace(enriched_trace)
        if trace_result and trace_result != baseline_prompt:
            variant_id = f"{agent_type}_{mode}_evolved"
            return {"variant_id": variant_id, "evolved_prompt": trace_result}

    evolved = evolver.generate_variant(agent_type, hint_seed, mode)
    variant_id = f"{agent_type}_{mode}_mutated"
    return {"variant_id": variant_id, "evolved_prompt": evolved}


def synthesize_scope_guidelines(evolver: PromptEvolver, agent_type: str, mode: str = "default") -> str:
    """Generate scope guidelines from recent failed traces for an agent/mode pair.

    Reads recent traces from the training collector, filters to failures
    that have categorised issues, and maps those issues to actionable guideline
    strings.  Returns an empty string when fewer than 3 failed traces are
    available or when no issues are categorised.

    Args:
        evolver: PromptEvolver instance (unused directly; provided for API consistency).
        agent_type: Agent type string to filter traces for.
        mode: Execution mode context for labelling the output.

    Returns:
        A newline-joined string of guidelines, or "" when not enough evidence.
    """
    try:
        from vetinari.learning.training_data import get_training_collector

        collector = get_training_collector()
        # get_recent_traces only accepts (limit, failed_only) — filter by
        # agent_type and mode from the returned dicts afterwards.
        raw_traces = collector.get_recent_traces(limit=50, failed_only=True)
        traces = [t for t in raw_traces if t.get("agent_type") == agent_type and t.get("mode", "default") == mode]
    except Exception as exc:
        logger.warning(
            "Could not load recent traces for %s/%s — returning empty guidelines: %s",
            agent_type,
            mode,
            exc,
        )
        return ""

    failed = [t for t in traces if not t.get("inspector_verdict", {}).get("passed", True)]
    if len(failed) < 3:
        return ""

    _ISSUE_GUIDELINES: dict[str, str] = {
        "incomplete": "Always produce complete, fully-formed output — do not truncate.",
        "format": "Follow the requested output format strictly.",
        "quality": "Ensure output meets the quality bar before responding.",
        "scope": "Stay within the defined scope — do not add unrequested work.",
        "hallucin": "Only assert facts you are confident are correct.",
    }

    guidelines: list[str] = []
    for trace in failed:
        issues = trace.get("inspector_verdict", {}).get("issues", [])
        for issue in issues:
            issue_lower = issue.lower()
            for keyword, guideline in _ISSUE_GUIDELINES.items():
                if keyword in issue_lower and guideline not in guidelines:
                    guidelines.append(guideline)

    if not guidelines:
        return ""

    header = f"Scope guidelines for {agent_type} ({mode} mode):"
    return "\n".join([header] + [f"- {g}" for g in guidelines])
