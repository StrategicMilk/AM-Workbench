"""Lightweight CI benchmark suite for Vetinari.

Contains three fast probes that require no live LLM calls.  Extracted from
``runner.py`` to keep that module under the 550-line file limit.

All names are re-exported from ``runner.py`` so existing import paths remain
valid.
"""

from __future__ import annotations

import logging
from typing import Any

from vetinari.clock import utc_now_iso

logger = logging.getLogger(__name__)


# Pass/fail thresholds for each CI probe.
_CI_THRESHOLDS: dict[str, float] = {
    "plan_latency_ms": 500.0,  # Must respond within 500 ms
    "decomposition_score": 0.5,  # TaskBench quality must be >= 0.50
    "token_optimization_ratio": 1.01,  # Ratio <= 1.01 (no pathological expansion, LLM-free CI)
}


def _ci_plan_latency() -> dict[str, Any]:
    """Measure plan-generation overhead without a live LLM.

    Attempts to instantiate the planning engine and call ``generate_plan()``
    on a trivial goal.  Falls back gracefully if the engine is unavailable.
    Pass threshold: elapsed time < 500 ms.

    Returns:
        Score entry dict with keys ``name``, ``score``, ``threshold``, ``passed``.
    """
    import time

    threshold = _CI_THRESHOLDS["plan_latency_ms"]
    start = time.monotonic()
    error: str | None = None
    try:
        from vetinari.planning.plan_mode import PlanModeEngine
        from vetinari.planning.plan_types import PlanGenerationRequest

        engine = PlanModeEngine()
        engine.generate_plan(PlanGenerationRequest(goal="build a hello-world web server"))
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        logger.warning("PlanningEngine unavailable for overhead measurement", exc_info=True)
    elapsed_ms = (time.monotonic() - start) * 1000

    result = {
        "name": "plan_latency_ms",
        "score": round(elapsed_ms, 2),
        "threshold": threshold,
        "passed": error is None and elapsed_ms < threshold,
    }
    if error:
        result["error"] = error
    return result


def _ci_decomposition_score() -> dict[str, Any]:
    """Evaluate task decomposition quality using one TaskBench case.

    Runs the TaskBenchAdapter and scores the result using the standard
    TaskBench rubric. If the planner path is unavailable, the adapter returns
    an unavailable result instead of scoring expected-output fixtures.
    Pass threshold: score >= 0.50.

    Returns:
        Score entry dict with keys ``name``, ``score``, ``threshold``, ``passed``.
    """
    threshold = _CI_THRESHOLDS["decomposition_score"]
    try:
        from vetinari.benchmarks.taskbench import TaskBenchAdapter

        adapter = TaskBenchAdapter()
        cases = adapter.load_cases(limit=1)
        if not cases:
            return {"name": "decomposition_score", "score": 0.0, "threshold": threshold, "passed": False}
        result = adapter.run_case(cases[0], "ci-decomp")
        score = adapter.evaluate(result)
    except Exception as exc:
        logger.warning("CI decomposition score failed: %s", exc)
        score = 0.0
        provenance = {"exc_type": type(exc).__name__, "exc_msg": str(exc)}
    else:
        provenance = None
    result = {
        "name": "decomposition_score",
        "score": round(float(score), 4),
        "threshold": threshold,
        "passed": float(score) >= threshold,
    }
    if provenance is not None:
        result["provenance"] = provenance
    return result


def _ci_token_optimization() -> dict[str, Any]:
    """Verify the token optimizer does not pathologically expand tokens.

    Runs ``TokenOptimizer.prepare_prompt()`` on a fixed verbose prompt and
    measures the word-count ratio of the optimised prompt to the raw prompt.
    A ratio <= 1.01 passes (no more than 1% expansion).

    Returns:
        Score entry dict with keys ``name``, ``score``, ``threshold``, ``passed``.
    """
    threshold = _CI_THRESHOLDS["token_optimization_ratio"]
    sample = (
        "Please carefully and thoroughly analyse the following extremely verbose "
        "description of a very simple task: build a web server."
    )
    raw_tokens = len(sample.split())
    error: str | None = None
    try:
        from vetinari.token_optimizer import TokenOptimizer

        optimizer = TokenOptimizer()
        optimised_result = optimizer.prepare_prompt(sample)
        optimised_prompt = (
            optimised_result.get("prompt", sample) if isinstance(optimised_result, dict) else str(optimised_result)
        )
        opt_tokens = len(optimised_prompt.split())
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        logger.warning("Token optimizer unavailable for CI benchmark", exc_info=True)
        opt_tokens = raw_tokens

    ratio = opt_tokens / max(raw_tokens, 1)
    result = {
        "name": "token_optimization_ratio",
        "score": round(ratio, 4),
        "threshold": threshold,
        "passed": error is None and ratio <= threshold,
    }
    if error:
        result["error"] = error
    return result


def run_ci_benchmarks() -> dict[str, Any]:
    """Run the lightweight CI benchmark suite.

    Executes three fast probes that require no live LLM calls:

    - **plan_latency_ms**: Measures planning engine response time for a trivial
      decompose request.  Pass threshold: < 500 ms.
    - **decomposition_score**: One TaskBench case, scored with the standard
      rubric. Unavailable planner execution fails closed. Pass threshold: >= 0.50.
    - **token_optimization_ratio**: Checks that ``TokenOptimizer`` does not
      pathologically expand a prompt.  Pass threshold: ratio <= 1.01.

    Returns:
        Dictionary with schema::

            {
                "timestamp": "<ISO 8601 string>",
                "suite": "ci",
                "scores": [
                    {
                        "name": str,
                        "score": float,
                        "threshold": float,
                        "passed": bool,
                    },
                    ...
                ],
                "overall_passed": bool,
            }
    """
    timestamp = utc_now_iso()
    scores: list[dict[str, Any]] = [
        _ci_plan_latency(),
        _ci_decomposition_score(),
        _ci_token_optimization(),
    ]
    overall_passed = all(s["passed"] for s in scores)
    logger.info(
        "CI benchmarks: %d/%d passed, overall_passed=%s",
        sum(1 for s in scores if s["passed"]),
        len(scores),
        overall_passed,
    )
    return {
        "timestamp": timestamp,
        "suite": "ci",
        "scores": scores,
        "overall_passed": overall_passed,
    }
