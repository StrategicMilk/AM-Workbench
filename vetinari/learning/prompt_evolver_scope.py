"""Trace and scope-aware prompt evolution helpers."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from vetinari.boundary_guards import require_nonempty
from vetinari.learning.prompt_evolver_models import PromptVariant
from vetinari.types import AgentType, PromptVersionStatus

if TYPE_CHECKING:
    from vetinari.learning.prompt_evolver import PromptEvolver

logger = logging.getLogger(__name__)


def _generate_variant_from_trace(
    evolver: PromptEvolver,
    agent_type: str,
    baseline_prompt: str,
    failed_trace: dict[str, Any],
) -> str | None:
    """Generate a targeted prompt variant by diagnosing a failed trace."""
    try:
        from vetinari.learning.prompt_optimizer import get_prompt_optimizer

        optimizer = get_prompt_optimizer()
        experiment = optimizer.optimize_via_trace(
            agent_type=agent_type,
            baseline_instruction=baseline_prompt,
            failed_trace=failed_trace,
        )

        if experiment is None or experiment.instruction == baseline_prompt:
            return evolver.generate_variant(agent_type, baseline_prompt)

        new_instruction = str(experiment.instruction)
        with evolver._lock:
            existing = evolver._variants.get(agent_type, [])
            variant_id = f"{agent_type}_trace_v{len(existing) + 1}"
            variant = PromptVariant(
                variant_id=variant_id,
                agent_type=agent_type,
                prompt_text=new_instruction,
            )
            if agent_type not in evolver._variants:
                evolver._variants[agent_type] = []
            evolver._variants[agent_type].append(variant)
            evolver._save_variants()

        logger.info(
            "[PromptEvolver] Trace-based variant %s created for %s (diagnosis: %s)",
            variant_id,
            agent_type,
            experiment.trace_diagnosis or "unknown",
        )
        return new_instruction
    except Exception:
        logger.warning(
            "Trace-based variant generation failed for %s -- falling back to blind mutation",
            agent_type,
            exc_info=True,
        )
        return evolver.generate_variant(agent_type, baseline_prompt)


def _evolve_per_level(
    evolver: PromptEvolver,
    agent_type: str,
    level: str = "default",
    failed_traces: list[dict[str, Any]] | None = None,
) -> dict[str, str | None]:
    """Evolve the prompt for an agent and execution-level context."""
    variants = evolver._variants.get(agent_type, [])
    baseline_variant = next(
        (
            variant
            for variant in variants
            if variant.is_baseline and variant.status == PromptVersionStatus.PROMOTED.value
        ),
        None,
    )

    if baseline_variant is None:
        return {"variant_id": None, "evolved_prompt": None}

    baseline_text = baseline_variant.prompt_text
    hint = evolver._build_level_hint(agent_type, level)
    seeded_prompt = f"{baseline_text}\n\n{hint}" if hint else baseline_text

    if failed_traces:
        trace = failed_traces[0]
        evolved = evolver.generate_variant_from_trace(
            agent_type=agent_type,
            baseline_prompt=seeded_prompt,
            failed_trace=trace,
        )
        if evolved and evolved != baseline_text and evolved != seeded_prompt:
            variant_id = require_nonempty(f"{agent_type}_{level}_evolved", field_name="variant_id")
            return {"variant_id": variant_id, "evolved_prompt": evolved}
        evolved = evolver.generate_variant(agent_type, seeded_prompt, mode=level)
        variant_id = require_nonempty(f"{agent_type}_{level}_mutated", field_name="variant_id")
        return {"variant_id": variant_id, "evolved_prompt": evolved}

    evolved = evolver.generate_variant(agent_type, seeded_prompt, mode=level)
    variant_id = require_nonempty(f"{agent_type}_{level}_mutated", field_name="variant_id")
    return {"variant_id": variant_id, "evolved_prompt": evolved}


def _build_level_hint(agent_type: str, level: str) -> str:
    """Build an agent- and level-specific hint to seed prompt mutation."""
    if agent_type == AgentType.WORKER.value:
        return (
            f"You are operating in '{level}' mode. "
            f"Tailor your output to satisfy the '{level}' task requirements precisely."
        )
    if agent_type == AgentType.INSPECTOR.value:
        return (
            f"You are evaluating Worker outputs in '{level}' mode. "
            "Focus on completeness and correctness of Worker-produced content."
        )
    return f"Context: operating in '{level}' scope."


def _synthesize_scope_guidelines(evolver: PromptEvolver, agent_type: str, level: str = "default") -> str:
    """Synthesize failure-informed guidelines for an agent scope."""
    del evolver
    try:
        from vetinari.learning.training_data import get_training_collector

        collector = get_training_collector()
        traces = collector.get_recent_traces(limit=20, failed_only=True)
    except Exception as exc:
        logger.warning(
            "Could not load failed traces for %s/%s -- returning empty guidelines: %s",
            agent_type,
            level,
            exc,
        )
        return ""

    issue_counts = _count_issue_categories(traces)
    if sum(issue_counts.values()) < 3:
        return ""

    guideline_map: dict[str, str] = {
        "incomplete_output": "Always produce a complete response -- do not stop early or omit sections.",
        "format_error": "Follow the specified output format exactly. Validate structure before submitting.",
        "reasoning_error": "Think step by step and decompose the problem before answering.",
        "off_topic": "Stay strictly within scope. Address every required component.",
        "unclassified_failure": "Treat uncategorized inspector failures as blocking feedback: restate the issue, correct it, and verify the final output against the inspector note.",
    }

    lines = [
        f"Scope guidelines for {agent_type} in '{level}' mode",
        "Common failure patterns observed -- address each in your response:",
    ]
    for category, count in sorted(issue_counts.items(), key=lambda row: -row[1]):
        guideline = guideline_map.get(category)
        if guideline:
            lines.append(f"  - ({count}x) {guideline}")

    if len(lines) == 2:
        return ""
    return "\n".join(lines)


def _count_issue_categories(traces: list[dict[str, Any]]) -> dict[str, int]:
    """Count canonical prompt-failure issue categories from failed traces."""
    issue_counts: dict[str, int] = {}
    for trace in traces:
        verdict = trace.get("inspector_verdict")
        if not isinstance(verdict, dict):
            continue
        issues = verdict.get("issues")
        if not isinstance(issues, list):
            continue
        for issue in issues:
            issue_lower = str(issue).lower()
            if "incomplete" in issue_lower:
                issue_counts["incomplete_output"] = issue_counts.get("incomplete_output", 0) + 1
            elif "format" in issue_lower or "parse" in issue_lower:
                issue_counts["format_error"] = issue_counts.get("format_error", 0) + 1
            elif "reasoning" in issue_lower or "wrong" in issue_lower:
                issue_counts["reasoning_error"] = issue_counts.get("reasoning_error", 0) + 1
            elif "off" in issue_lower or "topic" in issue_lower:
                issue_counts["off_topic"] = issue_counts.get("off_topic", 0) + 1
            else:
                issue_counts["unclassified_failure"] = issue_counts.get("unclassified_failure", 0) + 1
    return issue_counts
