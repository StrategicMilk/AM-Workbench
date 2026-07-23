"""DAPO reward data types and reward computation.

Contains ExecutionResult, RewardBreakdown, compute_dapo_reward, StageResult,
and TrainingResult. The TrainingStageOrchestrator lives in dapo.py to keep
both files under the 550-line limit.

Reward tiers (additive, sum to 1.0):
  Tier 1: Execution pass/fail     — 0.10 (free, instant)
  Tier 2: Test pass rate           — 0.25 (free, ~seconds)
  Tier 3: Static analysis score    — 0.15 (free, instant)
  Tier 4: Heuristic pre-screen     — 0.20 (~ms, cheap ML)
  Tier 5: LLM-as-judge score      — 0.20 (expensive, tokens)
  Efficiency bonus (no rework)     — 0.10
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


# Reward tier weights — must sum to 1.0.
# Tier 1 is binary (execution pass/fail); Tiers 2-5 are continuous.
TIER_EXECUTION_WEIGHT: float = 0.10  # Tier 1: execution pass/fail (free, instant)
TIER_TEST_WEIGHT: float = 0.25  # Tier 2: test pass rate (free, ~seconds)
TIER_STATIC_WEIGHT: float = 0.15  # Tier 3: static analysis score (free, instant)
TIER_HEURISTIC_WEIGHT: float = 0.20  # Tier 4: heuristic pre-screen (~ms, cheap ML)
TIER_LLM_JUDGE_WEIGHT: float = 0.20  # Tier 5: LLM-as-judge score (expensive, tokens)
TIER_EFFICIENCY_WEIGHT: float = 0.10  # Bonus: no rework needed


@dataclass(frozen=True, slots=True)
class DapoExecutionResult:
    """Result of code execution verification.

    Attributes:
        success: Whether the code executed without errors.
        error_message: Error message if execution failed, empty string on success.
        exit_code: Process exit code (0 for success).
    """

    success: bool = False
    error_message: str = ""
    exit_code: int = 0


@dataclass(frozen=True, slots=True)
class RewardBreakdown:
    """Detailed breakdown of the DAPO reward computation.

    Args:
        total_reward: Final reward value (0.0-1.0).
        tier1_execution: Tier 1 execution contribution.
        tier2_test: Tier 2 test pass rate contribution.
        tier3_static: Tier 3 static analysis contribution.
        tier4_heuristic: Tier 4 heuristic contribution.
        tier5_llm_judge: Tier 5 LLM judge contribution.
        efficiency_bonus: Efficiency bonus contribution.
        llm_judge_available: Whether the LLM judge score was present.
        hard_floor_applied: Whether the hard floor (code doesn't run) was applied.
    """

    total_reward: float = 0.0
    tier1_execution: float = 0.0
    tier2_test: float = 0.0
    tier3_static: float = 0.0
    tier4_heuristic: float = 0.0
    tier5_llm_judge: float = 0.0
    efficiency_bonus: float = 0.0
    llm_judge_available: bool = False
    hard_floor_applied: bool = False

    def __repr__(self) -> str:
        """Show key identifying fields for debugging."""
        return f"RewardBreakdown(total_reward={self.total_reward!r}, hard_floor_applied={self.hard_floor_applied!r})"

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dictionary.

        Returns:
            Dictionary representation.
        """
        return {
            "total_reward": round(self.total_reward, 4),
            "tier1_execution": round(self.tier1_execution, 4),
            "tier2_test": round(self.tier2_test, 4),
            "tier3_static": round(self.tier3_static, 4),
            "tier4_heuristic": round(self.tier4_heuristic, 4),
            "tier5_llm_judge": round(self.tier5_llm_judge, 4),
            "efficiency_bonus": round(self.efficiency_bonus, 4),
            "llm_judge_available": self.llm_judge_available,
            "hard_floor_applied": self.hard_floor_applied,
        }


def compute_dapo_reward(
    quality_result: dict[str, Any],
    execution_result: DapoExecutionResult | None = None,
) -> RewardBreakdown:
    """Compute tiered verifiable reward for DAPO training (RLVR paradigm).

    Reward tiers are additive: each tier contributes independently.
    Cheaper tiers always run; expensive tiers are gated by need.

    Args:
        quality_result: Dictionary with quality metrics (test_pass_rate,
            static_analysis_score, heuristic_score, score, rework_count).
        execution_result: Optional execution verification result.

    Returns:
        RewardBreakdown with total reward and per-tier contributions.
    """
    # Tier 1: Execution pass/fail (binary, free, instant)
    tier1_execution = 0.0
    if execution_result is not None:
        if execution_result.success:
            tier1_execution = TIER_EXECUTION_WEIGHT
        else:
            # Hard floor: code that doesn't run gets zero reward
            return RewardBreakdown(hard_floor_applied=True, total_reward=0.0)

    # Tier 2: Test pass rate (continuous, free, ~seconds)
    test_pass_rate = quality_result.get("test_pass_rate", 0.0)
    tier2_test = test_pass_rate * TIER_TEST_WEIGHT

    # Tier 3: Static analysis score (continuous, free, instant)
    static_score = quality_result.get("static_analysis_score", 0.0)
    tier3_static = static_score * TIER_STATIC_WEIGHT

    # Tier 4: Quality agent heuristic pre-screen (continuous, cheap ML)
    heuristic_score = quality_result.get("heuristic_score", 0.0)
    tier4_heuristic = heuristic_score * TIER_HEURISTIC_WEIGHT

    # Tier 5: LLM-as-judge score (continuous, expensive)
    llm_judge_available = "score" in quality_result and quality_result.get("score") is not None
    llm_judge_score = float(quality_result["score"]) if llm_judge_available else 0.0
    tier5_llm_judge = llm_judge_score * TIER_LLM_JUDGE_WEIGHT

    # Efficiency bonus: no rework needed.
    # Defect 17 fix: only award the bonus when the key is explicitly present
    # and set to 0.  Missing key means "not scored" — no bonus for unscored samples.
    efficiency_bonus = 0.0
    if "rework_count" in quality_result and quality_result["rework_count"] == 0:
        efficiency_bonus = TIER_EFFICIENCY_WEIGHT

    # Sum all tiers
    total_reward = min(
        1.0,
        tier1_execution + tier2_test + tier3_static + tier4_heuristic + tier5_llm_judge + efficiency_bonus,
    )

    return RewardBreakdown(
        total_reward=total_reward,
        tier1_execution=tier1_execution,
        tier2_test=tier2_test,
        tier3_static=tier3_static,
        tier4_heuristic=tier4_heuristic,
        tier5_llm_judge=tier5_llm_judge,
        efficiency_bonus=efficiency_bonus,
        llm_judge_available=llm_judge_available,
    )


@dataclass
class StageResult:
    """Result of a training pipeline stage.

    Args:
        success: Whether the stage completed successfully.
        stage_name: Name of the stage.
        output_model: Path to the output model (if applicable).
        error: Error message if the stage failed.
        metrics: Stage-specific metrics.
    """

    success: bool = False
    stage_name: str = ""
    output_model: str = ""
    error: str = ""
    metrics: dict[str, Any] = field(default_factory=dict)

    def __repr__(self) -> str:
        """Show key identifying fields for debugging."""
        return f"StageResult(stage_name={self.stage_name!r}, success={self.success!r})"


@dataclass
class DapoTrainingResult:
    """Result of the full training pipeline.

    Args:
        success: Whether all stages completed successfully.
        final_model: Path to the final trained model.
        stage_failed: Name of the stage that failed (if any).
        error: Error message if the pipeline failed.
        stage_results: Results from each stage.
    """

    success: bool = False
    final_model: str = ""
    stage_failed: str = ""
    error: str = ""
    stage_results: list[StageResult] = field(default_factory=list)

    def __repr__(self) -> str:
        """Show key identifying fields for debugging."""
        return f"TrainingResult(success={self.success!r}, stage_failed={self.stage_failed!r})"
