"""Promotion, benchmark, and shadow-test gates for prompt evolution."""

from __future__ import annotations

import logging
from collections import deque
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from vetinari.learning.prompt_evolver_models import PromptVariant
from vetinari.types import PromptVersionStatus

if TYPE_CHECKING:
    from vetinari.learning.prompt_evolver import PromptEvolver

logger = logging.getLogger(__name__)


def _promote_variant_to_method_library(
    evolver: PromptEvolver,
    agent_type: str,
    variant_id: str,
    *,
    method_library: Any | None,
    project_id: str,
    provenance_ref: str,
    consent_ref: str,
    safety_ref: str,
    confidence: float,
) -> Any:
    """Persist a promoted prompt variant as a measured MethodLibrary card."""
    from vetinari.workbench.method_library import MethodLibrary, MethodPromotionRejected

    with evolver._lock:
        variants = evolver._variants.get(agent_type, [])
        variant = next((row for row in variants if row.variant_id == variant_id), None)
        baseline = next((row for row in variants if row.is_baseline), None)
        if variant is None:
            raise MethodPromotionRejected(f"prompt variant {variant_id!r} is not registered for {agent_type!r}")
        if variant.status != PromptVersionStatus.PROMOTED.value:
            raise MethodPromotionRejected(f"prompt variant {variant_id!r} is not promoted")
        baseline_score = baseline.avg_quality if baseline is not None and baseline.trials else 0.0
        quality_score = variant.avg_quality if variant.trials else 0.0
        prompt_text = variant.prompt_text

    library = method_library if method_library is not None else MethodLibrary()
    return library.record_prompt_method_card(
        project_id=project_id,
        agent_type=agent_type,
        variant_id=variant_id,
        prompt_text=prompt_text,
        quality_score=quality_score,
        baseline_score=baseline_score,
        provenance_ref=provenance_ref,
        consent_ref=consent_ref,
        safety_ref=safety_ref,
        confidence=confidence,
        promoted_by="prompt_evolver",
    )


def _check_promotion(evolver: PromptEvolver, agent_type: str) -> None:
    """Evaluate testing variants against statistics, benchmark, and shadow gates."""
    variants = evolver._variants.get(agent_type, [])
    baseline = next(
        (
            variant
            for variant in variants
            if variant.is_baseline and variant.status == PromptVersionStatus.PROMOTED.value
        ),
        None,
    )
    baseline_quality = baseline.avg_quality if baseline and baseline.trials > 0 else 0.65
    baseline_scores = list(evolver._score_history.get(baseline.variant_id if baseline else "", []))

    for variant in variants:
        if variant.status != PromptVersionStatus.TESTING.value or variant.trials < evolver.MIN_TRIALS:
            continue

        variant_scores = list(evolver._score_history.get(variant.variant_id, []))
        mean_diff = variant.avg_quality - baseline_quality

        if mean_diff >= evolver.MIN_IMPROVEMENT:
            significant, effect_size = evolver._test_significance(baseline_scores, variant_scores)
            if significant and effect_size >= evolver.MIN_EFFECT_SIZE:
                _start_shadow_test_if_benchmark_passes(evolver, agent_type, baseline, variant, effect_size)
            else:
                logger.debug(
                    "[PromptEvolver] %s improvement not significant (significant=%s, d=%.3f)",
                    variant.variant_id,
                    significant,
                    effect_size,
                )
        elif variant.avg_quality < baseline_quality - 0.1:
            variant.status = PromptVersionStatus.DEPRECATED.value
            evolver._update_operator_feedback(variant.variant_id, variant.avg_quality - baseline_quality)
            logger.info("[PromptEvolver] Deprecated %s for %s", variant.variant_id, agent_type)

    evolver._save_variants()


def _start_shadow_test_if_benchmark_passes(
    evolver: PromptEvolver,
    agent_type: str,
    baseline: PromptVariant | None,
    variant: PromptVariant,
    effect_size: float,
) -> None:
    """Create the shadow test for a statistically promising candidate."""
    if not evolver._validate_variant_with_benchmark(variant):
        logger.info(
            "[PromptEvolver] %s passed stats but FAILED benchmark validation -- not promoting",
            variant.variant_id,
        )
        return

    try:
        from vetinari.learning.shadow_testing import get_shadow_test_runner

        runner = get_shadow_test_runner()
        test_id = runner.create_test(
            description=f"Prompt evolution: {variant.variant_id} vs baseline for {agent_type}",
            production_config={"variant_id": baseline.variant_id if baseline else "default"},
            candidate_config={"variant_id": variant.variant_id, "agent_type": agent_type},
        )
        variant.metadata = getattr(variant, "metadata", {}) or {}
        variant.metadata["shadow_test_id"] = test_id
        variant.status = PromptVersionStatus.SHADOW_TESTING.value
        logger.info(
            "[PromptEvolver] %s for %s passed stats+benchmark -- entering shadow testing (effect_size=%.3f)",
            variant.variant_id,
            agent_type,
            effect_size,
        )
    except Exception as exc:
        logger.warning(
            "[PromptEvolver] Shadow test creation failed for %s; promotion blocked: %s",
            variant.variant_id,
            exc,
        )
        variant.metadata = getattr(variant, "metadata", {}) or {}
        variant.metadata["promotion_blocked"] = "shadow_testing_unavailable"


def _check_shadow_test_results(evolver: PromptEvolver) -> None:
    """Finalize variants that are waiting on shadow-test results."""
    try:
        from vetinari.learning.shadow_testing import get_shadow_test_runner

        runner = get_shadow_test_runner()
    except Exception as exc:
        logger.warning("[PromptEvolver] Cannot check shadow results -- runner unavailable: %s", exc)
        return

    with evolver._lock:
        changed = False
        for agent_type, variants in evolver._variants.items():
            baseline = next(
                (variant for variant in variants if variant.status == PromptVersionStatus.PROMOTED.value),
                None,
            )
            for variant in variants:
                if variant.status != PromptVersionStatus.SHADOW_TESTING.value:
                    continue

                test_id = variant.metadata.get("shadow_test_id")
                if not test_id:
                    continue

                result = runner.evaluate(test_id)
                decision = result.get("decision", "")
                if decision == "insufficient_data":
                    continue

                if decision in ("promote", "promoted"):
                    variant.status = PromptVersionStatus.PROMOTED.value
                    variant.promoted_at = datetime.now(timezone.utc).isoformat()
                    evolver._update_operator_feedback(variant.variant_id, result.get("quality_delta", 0.0))
                    if baseline and variant.variant_id != baseline.variant_id:
                        baseline.status = PromptVersionStatus.DEPRECATED.value
                    logger.info(
                        "[PromptEvolver] Shadow test PASSED - promoting %s for %s",
                        variant.variant_id,
                        agent_type,
                    )
                    changed = True
                elif decision in ("reject", "rejected", "not_found"):
                    variant.status = PromptVersionStatus.DEPRECATED.value
                    logger.info(
                        "[PromptEvolver] Shadow test FAILED (decision=%s) - deprecating %s for %s",
                        decision,
                        variant.variant_id,
                        agent_type,
                    )
                    changed = True

        if changed:
            evolver._save_variants()


def _validate_variant_with_benchmark(evolver: PromptEvolver, variant: PromptVariant) -> bool:
    """Return whether the variant passes the fail-closed benchmark gate."""
    try:
        from vetinari.benchmarks.suite import BenchmarkSuite

        suite = BenchmarkSuite()
        result = suite.run_agent(variant.agent_type)

        if result.cases_run == 0:
            logger.warning("[PromptEvolver] No benchmark cases for %s; blocking promotion", variant.agent_type)
            return False

        pass_rate = result.cases_passed / max(result.cases_run, 1)
        avg_score = result.avg_score

        logger.debug(
            "[PromptEvolver] Benchmark validation for %s: pass_rate=%.3f, avg_score=%.3f, threshold=%s",
            variant.variant_id,
            pass_rate,
            avg_score,
            evolver.BENCHMARK_PASS_THRESHOLD,
        )

        if pass_rate < evolver.BENCHMARK_PASS_THRESHOLD:
            return False

        if variant.variant_id not in evolver._score_history:
            evolver._score_history[variant.variant_id] = deque(maxlen=500)
        evolver._score_history[variant.variant_id].append(avg_score)

        return True
    except ImportError:
        logger.warning("[PromptEvolver] Benchmark suite not available; blocking promotion (fail closed)")
        return False
    except Exception:
        logger.exception("[PromptEvolver] Benchmark validation error -- blocking promotion (fail closed)")
        return False
