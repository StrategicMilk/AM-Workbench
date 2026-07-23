"""Worker-internal self-reflection — draft, evaluate, refine loop.

Before Worker output reaches the Inspector, self-reflection lets the Worker
critique and improve its own output. This catches obvious issues early,
reducing Inspector rejection rate and pipeline iteration count.

Three strategies:
- SIMPLE: no reflection, pass through directly (default)
- DRAFT_REFINE: evaluate then refine in a loop until acceptable
- TREE_OF_THOUGHT: generate multiple candidates, evaluate each, pick best

This is distinct from pipeline-level self-refinement (Stage 5.5 in
pipeline_stages.py). Self-reflection runs inside the Worker's execute()
call, before output leaves the agent boundary.
"""

from __future__ import annotations

import enum
import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

from vetinari.agents.contracts import AgentResult, AgentTask
from vetinari.boundary_guards import account_evidence_drop

logger = logging.getLogger(__name__)


class ReflectionStrategy(enum.Enum):
    """How the Worker self-reflects before submitting output."""

    SIMPLE = "simple"  # No reflection — pass through
    DRAFT_REFINE = "draft_refine"  # Draft → evaluate → refine loop
    TREE_OF_THOUGHT = "tree_of_thought"  # Multiple candidates → evaluate → pick best


MAX_REFLECTION_ITERATIONS = 5  # Hard cap on evaluate-refine cycles
KAIZEN_ITERATION_THRESHOLD = 4  # Above this = model under-qualified signal
TREE_OF_THOUGHT_CANDIDATES = 3  # Candidate count for tree-of-thought strategy


@dataclass(frozen=True, slots=True)
class SelfReflectionResult:
    """Outcome of a self-reflection loop.

    Attributes:
        original_output: The initial handler output before reflection.
        refined_output: The output after reflection (same as original if unchanged).
        iterations_used: Number of evaluate-refine cycles performed.
        strategy: Which reflection strategy was used.
        is_improved: Whether the refined output differs from the original.
        evaluation_notes: Issues found during self-evaluation rounds.
    """

    original_output: Any
    refined_output: Any
    iterations_used: int
    strategy: ReflectionStrategy
    is_improved: bool
    evaluation_notes: list[str] = field(default_factory=list)

    def __repr__(self) -> str:
        return (
            f"SelfReflectionResult(strategy={self.strategy.value!r}, "
            f"iterations={self.iterations_used}, improved={self.is_improved})"
        )


class Evaluator(Protocol):
    """Evaluates agent output — typically wraps the agent's own verify()."""

    def __call__(self, output: Any) -> tuple[bool, list[str]]:
        """Check if output is acceptable.

        Args:
            output: The agent output to evaluate.

        Returns:
            Tuple of (is_acceptable, list_of_issues).
        """
        ...


class Refiner(Protocol):
    """Re-executes a task with self-evaluation feedback injected."""

    def __call__(self, task: AgentTask, prior_result: AgentResult, feedback: list[str]) -> AgentResult:
        """Refine a prior result using evaluation feedback.

        Args:
            task: The original task.
            prior_result: The result being refined.
            feedback: Issues found during evaluation.

        Returns:
            A new AgentResult incorporating the feedback.
        """
        ...


def get_reflection_strategy(task: AgentTask) -> ReflectionStrategy:
    """Determine reflection strategy from task context.

    The Foreman sets ``task.context["reflection_strategy"]`` in its plan.
    Defaults to SIMPLE (no reflection) if not specified.

    Args:
        task: The task being executed.

    Returns:
        The reflection strategy to use.
    """
    raw = task.context.get("reflection_strategy", "simple")
    try:
        return ReflectionStrategy(raw)
    except ValueError:
        logger.warning(
            "Unknown reflection strategy %r for task %s — falling back to SIMPLE",
            raw,
            task.task_id,
        )
        return ReflectionStrategy.SIMPLE


def reflect(
    task: AgentTask,
    initial_result: AgentResult,
    strategy: ReflectionStrategy,
    evaluator: Evaluator,
    refiner: Refiner,
) -> SelfReflectionResult:
    """Run the self-reflection loop on a Worker result.

    Dispatches to the appropriate strategy implementation. SIMPLE is a
    no-op passthrough; DRAFT_REFINE iterates evaluate-refine until
    acceptable or capped; TREE_OF_THOUGHT generates multiple candidates
    and picks the best.

    Args:
        task: Original task for context.
        initial_result: First-pass result from the handler.
        strategy: Which reflection strategy to use.
        evaluator: Callable that checks if output is acceptable.
        refiner: Callable that re-executes with feedback.

    Returns:
        SelfReflectionResult with the original and (possibly refined) output.
    """
    if strategy == ReflectionStrategy.SIMPLE:
        return SelfReflectionResult(
            original_output=initial_result.output,
            refined_output=initial_result.output,
            iterations_used=0,
            strategy=strategy,
            is_improved=False,
        )

    if strategy == ReflectionStrategy.DRAFT_REFINE:
        return _reflect_draft_refine(task, initial_result, evaluator, refiner)

    if strategy == ReflectionStrategy.TREE_OF_THOUGHT:
        return _reflect_tree_of_thought(task, initial_result, evaluator, refiner)

    # Defensive fallback for any future strategy values
    logger.warning("Unhandled reflection strategy %s — passing through", strategy.value)
    return SelfReflectionResult(
        original_output=initial_result.output,
        refined_output=initial_result.output,
        iterations_used=0,
        strategy=strategy,
        is_improved=False,
    )


# -- Strategy implementations ------------------------------------------------


def _reflect_draft_refine(
    task: AgentTask,
    initial_result: AgentResult,
    evaluator: Evaluator,
    refiner: Refiner,
) -> SelfReflectionResult:
    """Evaluate output, refine if issues found, repeat until acceptable or capped.

    Args:
        task: Original task for re-execution context.
        initial_result: First-pass handler result.
        evaluator: Checks if output is acceptable.
        refiner: Re-executes with feedback.

    Returns:
        SelfReflectionResult tracking the refinement process.
    """
    current_result = initial_result
    all_notes: list[str] = []
    iterations_used = 0

    for iteration in range(1, MAX_REFLECTION_ITERATIONS + 1):
        is_acceptable, issues = evaluator(current_result.output)
        iterations_used = iteration

        if is_acceptable:
            logger.info(
                "Self-reflection accepted at iteration %d for task %s",
                iteration,
                task.task_id,
            )
            break

        all_notes.extend(issues)
        logger.info(
            "Self-reflection iteration %d found %d issue(s) for task %s",
            iteration,
            len(issues),
            task.task_id,
        )

        current_result = refiner(task, current_result, issues)
    else:
        logger.warning(
            "Self-reflection hit %d iterations for task %s — possible model under-qualification",
            MAX_REFLECTION_ITERATIONS,
            task.task_id,
        )

    _report_to_kaizen(task, iterations_used)

    return SelfReflectionResult(
        original_output=initial_result.output,
        refined_output=current_result.output,
        iterations_used=iterations_used,
        strategy=ReflectionStrategy.DRAFT_REFINE,
        is_improved=current_result.output != initial_result.output,
        evaluation_notes=all_notes,
    )


def _reflect_tree_of_thought(
    task: AgentTask,
    initial_result: AgentResult,
    evaluator: Evaluator,
    refiner: Refiner,
) -> SelfReflectionResult:
    """Generate multiple candidates, evaluate each, select the one with fewest issues.

    Produces TREE_OF_THOUGHT_CANDIDATES alternatives by refining the initial
    result with varied feedback prompts, then picks the best scoring candidate.

    Args:
        task: Original task for context.
        initial_result: First-pass handler result.
        evaluator: Checks if output is acceptable.
        refiner: Re-executes with feedback.

    Returns:
        SelfReflectionResult with the best candidate selected.
    """
    # (result, issue_count, issues) — lower issue_count is better
    candidates: list[tuple[AgentResult, int, list[str]]] = []

    # Evaluate the initial draft
    is_acceptable, initial_issues = evaluator(initial_result.output)
    candidates.append((initial_result, len(initial_issues), initial_issues))

    if is_acceptable:
        _report_to_kaizen(task, 1)
        return SelfReflectionResult(
            original_output=initial_result.output,
            refined_output=initial_result.output,
            iterations_used=1,
            strategy=ReflectionStrategy.TREE_OF_THOUGHT,
            is_improved=False,
            evaluation_notes=initial_issues,
        )

    # Generate alternative candidates with different feedback angles
    for i in range(TREE_OF_THOUGHT_CANDIDATES - 1):
        feedback = [*initial_issues, f"Alternative approach #{i + 2} requested — try a different angle"]
        candidate_result = refiner(task, initial_result, feedback)
        candidate_acceptable, candidate_issues = evaluator(candidate_result.output)
        candidates.append((candidate_result, len(candidate_issues), candidate_issues))

        if candidate_acceptable:
            break  # Found an acceptable candidate early

    # Pick the candidate with fewest issues
    candidates.sort(key=lambda c: c[1])
    best_result, _, _ = candidates[0]
    total_iterations = len(candidates)

    _report_to_kaizen(task, total_iterations)

    all_notes: list[str] = []
    for _, _, issues in candidates:
        all_notes.extend(issues)

    return SelfReflectionResult(
        original_output=initial_result.output,
        refined_output=best_result.output,
        iterations_used=total_iterations,
        strategy=ReflectionStrategy.TREE_OF_THOUGHT,
        is_improved=best_result.output != initial_result.output,
        evaluation_notes=all_notes,
    )


# -- Kaizen reporting --------------------------------------------------------


def _report_to_kaizen(task: AgentTask, iterations: int) -> None:
    """Log self-reflection metrics for kaizen weekly aggregator pickup.

    Iterations at or above KAIZEN_ITERATION_THRESHOLD trigger a warning
    indicating the model may be under-qualified for the task type.

    Args:
        task: Task that was reflected on.
        iterations: Number of reflection iterations used.
    """
    if iterations >= KAIZEN_ITERATION_THRESHOLD:
        logger.warning(
            "Task %s required %d self-reflection iterations (threshold: %d) — "
            "model may be under-qualified for this task type",
            task.task_id,
            iterations,
            KAIZEN_ITERATION_THRESHOLD,
        )

    logger.info(
        "Self-reflection complete for task %s: iterations=%d, threshold_exceeded=%s",
        task.task_id,
        iterations,
        iterations >= KAIZEN_ITERATION_THRESHOLD,
    )
    persist_fn = task.context.get("kaizen_persist_fn")
    if persist_fn is not None:
        report_kaizen_finding(
            finding={
                "id": f"self-reflection:{task.task_id}",
                "iterations": iterations,
                "threshold_exceeded": iterations >= KAIZEN_ITERATION_THRESHOLD,
            },
            persist_fn=persist_fn,
            logger=logger,
        )


def report_kaizen_finding(
    *,
    finding: dict[str, Any],
    persist_fn: Any,
    logger: logging.Logger | None = None,
) -> None:
    """Persist a kaizen finding or fail closed with evidence-drop accounting.

    Raises:
        RuntimeError: If no persistence function is available.
        Exception: Re-raises persistence failures after accounting for the drop.
    """
    active_logger = logger or globals()["logger"]
    evidence_ref = str(finding.get("id") or "kaizen_finding")
    if persist_fn is None:
        account_evidence_drop(
            logger=active_logger,
            evidence_ref=evidence_ref,
            reason="kaizen persist function unavailable",
        )
        raise RuntimeError("kaizen persist function unavailable")
    try:
        persist_fn(finding)
    except Exception:
        account_evidence_drop(
            logger=active_logger,
            evidence_ref=evidence_ref,
            reason="kaizen persist failed",
        )
        raise
