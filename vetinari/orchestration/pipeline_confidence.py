"""Pipeline confidence routing - applies confidence-based post-generation actions.

Takes a ConfidenceResult and the generated output, then executes the
appropriate action: proceed, refine, best-of-n selection, or defer to human.

Pipeline position: Execution -> **Confidence Routing** -> Quality Gate.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from vetinari.awareness.confidence import ConfidenceComputer, ConfidenceResult
from vetinari.types import ConfidenceAction, ConfidenceLevel

logger = logging.getLogger(__name__)


def apply_confidence_routing(
    output: str,
    confidence: ConfidenceResult,
    refine_fn: Callable[[str], str] | None = None,
    confidence_fn: Callable[[str], ConfidenceResult] | None = None,
    sample_fn: Callable[[], list[tuple[str, list[float]]]] | None = None,
    defer_fn: Callable[[str, ConfidenceResult], str | None] | None = None,
) -> tuple[str, ConfidenceResult]:
    """Route output based on its confidence assessment.

    This is the real routing logic that replaces fake self-reflection and
    longest-output-as-best-of-n. Each action has a concrete implementation
    or raises cleanly if the required callback isn't provided.

    Args:
        output: The generated output text.
        confidence: Confidence assessment of the output.
        refine_fn: Called for REFINE action; takes output, returns refined output.
            If None, output passes through with a warning logged.
        confidence_fn: Optional callback to reassess confidence after refinement.
        sample_fn: Called for BEST_OF_N action; returns list of (output, logprobs) tuples.
            If None, output passes through with a warning logged.
        defer_fn: Called for DEFER_TO_HUMAN; takes output and confidence, returns
            approved output (or None if deferred to queue).
            If None, output passes through with a warning logged.

    Returns:
        Tuple of (final_output, final_confidence). For PROCEED, these are
        unchanged. For other actions, confidence may be re-assessed.
    """
    action = confidence.action

    if action == ConfidenceAction.PROCEED:
        logger.debug("Confidence routing: PROCEED (score=%.3f)", confidence.score)
        return output, confidence

    if action == ConfidenceAction.REFINE:
        _require_callback("refine_fn", refine_fn, action)
        return _handle_refine(output, confidence, refine_fn, confidence_fn)

    if action == ConfidenceAction.BEST_OF_N:
        _require_callback("sample_fn", sample_fn, action)
        return _handle_best_of_n(output, confidence, sample_fn)

    if action == ConfidenceAction.DEFER_TO_HUMAN:
        _require_callback("defer_fn", defer_fn, action)
        return _handle_defer(output, confidence, defer_fn)

    # Exhaustive match; should never reach here
    logger.warning("Unknown confidence action %s; proceeding with original output", action)
    return output, confidence


def _require_callback(callback_name: str, callback: object | None, action: ConfidenceAction) -> None:
    if callback is None:
        raise ValueError(f"{callback_name} is required for {action.value} confidence routing")


def _handle_refine(
    output: str,
    confidence: ConfidenceResult,
    refine_fn: Callable[[str], str] | None,
    confidence_fn: Callable[[str], ConfidenceResult] | None,
) -> tuple[str, ConfidenceResult]:
    """Handle the REFINE action by triggering the self-refinement loop.

    Args:
        output: The original generated output.
        confidence: The confidence assessment that triggered REFINE.
        refine_fn: Callback that takes output and returns a refined version.
        confidence_fn: Optional callback to reassess refined output.

    Returns:
        Tuple of (refined_output, updated_confidence). If refine_fn is None,
        the original output and confidence are returned unchanged.
    """
    logger.info("Confidence routing: REFINE; triggering self-refinement (score=%.3f)", confidence.score)
    refined = refine_fn(output)
    if confidence_fn is None:
        return refined, ConfidenceResult(
            score=confidence.score,
            level=confidence.level,
            action=ConfidenceAction.PROCEED,
            explanation=confidence.explanation,
            factors={**confidence.factors, "refined": 1.0},
            source=confidence.source,
            unknown_situation=confidence.unknown_situation,
            metadata={
                **confidence.metadata,
                "pre_refinement_level": confidence.level.value,
                "refinement_confidence_status": "unmeasured",
            },
        )
    measured = confidence_fn(refined)
    return refined, ConfidenceResult(
        score=measured.score,
        level=measured.level,
        action=measured.action,
        explanation=measured.explanation,
        factors={**measured.factors, "refined": 1.0},
        source=measured.source,
        unknown_situation=measured.unknown_situation,
        metadata={
            **measured.metadata,
            "pre_refinement_level": confidence.level.value,
            "refinement_confidence_status": "measured",
        },
    )


def _handle_best_of_n(
    output: str,
    confidence: ConfidenceResult,
    sample_fn: Callable[[], list[tuple[str, list[float]]]] | None,
) -> tuple[str, ConfidenceResult]:
    """Handle BEST_OF_N - sample multiple outputs and pick the best by confidence score.

    Args:
        output: The original generated output (used as fallback if sampling fails).
        confidence: The confidence assessment that triggered BEST_OF_N.
        sample_fn: Callback that returns list of (output, logprobs) tuples.

    Returns:
        Tuple of (best_output, best_confidence) selected by highest logprob score.
        Falls back to original output if sample_fn is None or returns no candidates.
    """
    logger.info("Confidence routing: BEST_OF_N - sampling alternatives (score=%.3f)", confidence.score)
    candidates = sample_fn()

    if not candidates:
        logger.warning("BEST_OF_N sampling returned no candidates - using original output")
        return output, confidence

    # Select by highest mean logprob (confidence score), NOT longest output
    computer = ConfidenceComputer()
    best_output = output
    best_confidence = confidence
    for candidate_output, candidate_logprobs in candidates:
        candidate_confidence = computer.compute(candidate_logprobs)
        if candidate_confidence.score > best_confidence.score:
            best_output = candidate_output
            best_confidence = candidate_confidence

    logger.info(
        "BEST_OF_N selected: score=%.3f (was %.3f) from %d candidates",
        best_confidence.score,
        confidence.score,
        len(candidates),
    )
    return best_output, best_confidence


def _handle_defer(
    output: str,
    confidence: ConfidenceResult,
    defer_fn: Callable[[str, ConfidenceResult], str | None] | None,
) -> tuple[str, ConfidenceResult]:
    """Handle DEFER_TO_HUMAN - escalate to human via approval queue.

    Args:
        output: The original generated output.
        confidence: The confidence assessment that triggered DEFER_TO_HUMAN.
        defer_fn: Callback that takes output and confidence, returns approved output
            or None if deferred to the queue.

    Returns:
        Tuple of (output, confidence). If human approves, returns their output
        with HIGH confidence. If deferred to queue, returns original output with
        a deferred marker in metadata.
    """
    logger.info("Confidence routing: DEFER_TO_HUMAN - escalating (score=%.3f)", confidence.score)
    approved_output = defer_fn(output, confidence)

    if approved_output is None:
        # Deferred to queue - return original with a note
        deferred_confidence = ConfidenceResult(
            score=confidence.score,
            level=confidence.level,
            action=ConfidenceAction.DEFER_TO_HUMAN,
            explanation="Deferred to human approval queue - awaiting decision",
            factors=confidence.factors,
            source=confidence.source,
            unknown_situation=confidence.unknown_situation,
            metadata={**confidence.metadata, "deferred": True},
        )
        return output, deferred_confidence

    # Human approved - proceed with confidence
    approved_confidence = ConfidenceResult(
        score=1.0,  # Human approval = max confidence
        level=ConfidenceLevel.HIGH,
        action=ConfidenceAction.PROCEED,
        explanation="Human-approved output - proceeding with full confidence",
        factors={"human_approved": 1.0},
        source="human_approval",
    )
    return approved_output, approved_confidence
