"""Validation subsystem wiring.

Connects quality gates and the verification pipeline into the
Foreman->Worker->Inspector factory pipeline.

Quality gates run at pipeline stage transitions to prevent low-quality work
from propagating. The verification pipeline runs as part of Inspector
evaluation.

Pipeline role:
    Planning -> **Quality Gate** -> Execution -> **Quality Gate**
             -> Inspection -> Assembly
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

from vetinari.validation.quality_gates import GateCheckResult, GateResult, QualityGateRunner
from vetinari.validation.verification import (
    ValidationVerificationResult,
    VerificationLevel,
    VerificationStatus,
    get_verifier_pipeline,
)

logger = logging.getLogger(__name__)


@dataclass
class StageGateResult:
    """Aggregated outcome of running quality gates for a pipeline stage.

    Attributes:
        passed: True when all required gates passed.
        should_rework: True when at least one required gate failed, indicating
            the current stage output must be reworked before proceeding.
        gate_results: Individual check results produced by each configured gate.
        summary: Human-readable description of the overall gate outcome.
    """

    passed: bool
    should_rework: bool
    gate_results: list[GateCheckResult]
    summary: str

    def __repr__(self) -> str:
        return "StageGateResult(...)"


@dataclass
class VerificationSummary:
    """Aggregated outcome of running the verification pipeline on worker output.

    Attributes:
        passed: True only when verification can be accepted without rework.
            Warning-bearing summaries are real verifier output, but they are
            not accepted as passed.
        error_count: Total number of error-severity issues across all checks.
        warning_count: Total number of warning-severity issues across all checks.
        results: Per-check VerificationResult keyed by check name.
        recommendation: Disposition of the output: "accept", "rework", or "reject".
    """

    passed: bool
    error_count: int
    warning_count: int
    results: dict[str, ValidationVerificationResult]
    recommendation: str  # "accept", "rework", or "reject"

    def __repr__(self) -> str:
        return "VerificationSummary(...)"

    @property
    def user_recommendation(self) -> str:
        """Human-readable disposition for UI and CLI surfaces."""
        labels = {
            "accept": "Accept output",
            "rework": "Rework output before continuing",
            "reject": "Reject output and inspect verifier errors",
        }
        return labels.get(self.recommendation, "Inspect verification result before continuing")


def run_stage_gate(stage: str, artifacts: dict[str, Any]) -> StageGateResult:
    """Run all quality gates configured for the given pipeline stage.

    Executes QualityGateRunner.run_gate() for *stage* and aggregates the
    individual GateCheckResults into a StageGateResult. Any required gate
    that returns FAILED sets should_rework=True so the orchestrator knows
    the current stage must be repeated.

    A QualityGateResult event is published to the EventBus on completion
    (pass or fail). If the EventBus is unavailable the event is silently
    skipped after a warning log.

    Args:
        stage: Pipeline stage identifier, e.g. "post_planning" or
            "post_execution". Must match a key in
            QualityGateRunner.PIPELINE_GATES.
        artifacts: Dictionary of artifacts produced by the stage, passed
            directly to each gate's verification check.

    Returns:
        StageGateResult summarising whether all required gates passed,
        whether rework is needed, the individual check results, and a
        human-readable summary string.
    """
    runner = QualityGateRunner()
    gate_results: list[GateCheckResult] = runner.run_gate(stage, artifacts)

    required_failed = any(
        gr.result == GateResult.FAILED
        for gr in gate_results
        # run_gate only returns results for configured gates; treat every
        # returned result as potentially required (the runner already
        # filters by required flag internally).
    )

    passed = not required_failed
    should_rework = required_failed

    if gate_results:
        avg_score = sum(gr.score for gr in gate_results) / len(gate_results)
    else:
        avg_score = 0.0

    failed_names = [gr.gate_name for gr in gate_results if gr.result == GateResult.FAILED]
    if not gate_results:
        passed = False
        should_rework = True
        failed_names = [f"no_gates_configured:{stage}"]
        summary = f"No quality gates ran for stage '{stage}'"
    elif passed:
        summary = f"All quality gates passed for stage '{stage}'"
    else:
        summary = f"Quality gate(s) failed for stage '{stage}': {', '.join(failed_names)}"

    logger.info(
        "Stage gate '%s': passed=%s, gates=%d, avg_score=%.2f",
        stage,
        passed,
        len(gate_results),
        avg_score,
    )

    _publish_stage_gate_result(stage, passed, avg_score, failed_names)

    return StageGateResult(
        passed=passed,
        should_rework=should_rework,
        gate_results=gate_results,
        summary=summary,
    )


def _publish_stage_gate_result(stage: str, passed: bool, score: float, failed_names: list[str]) -> None:
    try:
        from vetinari.events import QualityGateResult as QualityGateResultEvent
        from vetinari.events import get_event_bus

        get_event_bus().publish(
            QualityGateResultEvent(
                event_type="QualityGateResult",
                timestamp=time.time(),
                task_id=stage,
                passed=passed,
                score=score,
                issues=failed_names,
            )
        )
    except Exception:
        logger.warning("Could not emit QualityGateResult event for stage '%s'; EventBus unavailable", stage)


def verify_worker_output(
    content: str,
    level: VerificationLevel = VerificationLevel.STANDARD,
) -> VerificationSummary:
    """Run the verification pipeline on Worker agent output.

    Uses the get_verifier_pipeline() singleton so no new pipeline object is
    created per call. If *level* differs from the singleton's configured
    level the singleton is still used — callers that need strict isolation
    should construct a VerificationPipeline directly.

    The recommendation is derived from error, warning, and evidence counts:
    - "reject" when one or more errors occur, or no check produced real output
    - "rework" when warnings occur without FAILED checks
    - "accept" when at least one check passes with no failures or warnings

    Args:
        content: The string output produced by the Worker agent (code,
            prose, JSON, etc.) to be verified.
        level: Desired verification strictness. Informational only when
            using the shared singleton; the singleton's own level governs
            which verifiers run.

    Returns:
        VerificationSummary with pass/fail disposition, aggregated issue
        counts, per-check results, and a recommendation string.
    """
    pipeline = get_verifier_pipeline()
    results: dict[str, ValidationVerificationResult] = pipeline.verify(content)

    error_count = sum(r.error_count for r in results.values())
    warning_count = sum(r.warning_count for r in results.values())
    has_failure = any(r.status == VerificationStatus.FAILED for r in results.values())
    has_warning = warning_count > 0 or any(r.status == VerificationStatus.WARNING for r in results.values())
    has_positive_evidence = any(r.status == VerificationStatus.PASSED for r in results.values())

    passed = not has_failure and error_count == 0 and not has_warning and has_positive_evidence

    if has_failure or error_count > 0:
        recommendation = "reject"
    elif has_warning:
        recommendation = "rework"
    elif not has_positive_evidence:
        recommendation = "reject"
    else:
        recommendation = "accept"

    logger.info(
        "Worker output verification: passed=%s, errors=%d, warnings=%d, recommendation=%s",
        passed,
        error_count,
        warning_count,
        recommendation,
    )

    return VerificationSummary(
        passed=passed,
        error_count=error_count,
        warning_count=warning_count,
        results=results,
        recommendation=recommendation,
    )


def wire_validation_subsystem() -> None:
    """Register and warm up the validation subsystem.

    Calls the get_verifier_pipeline() singleton to ensure it is initialised
    before the first request arrives, and logs that validation wiring is
    complete. This function is the single call site that activates the
    subsystem at application startup.
    """
    get_verifier_pipeline()
    logger.info("Validation subsystem wiring complete — quality gates and verification pipeline ready")
