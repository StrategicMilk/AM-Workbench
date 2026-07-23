"""Test-Time Compute Scaling for Vetinari.

===========================================
Implements three levels of test-time compute scaling:

  Level 1: Best-of-N — generate N candidates and pick the best
  Level 2: Heuristic-guided — score intermediate reasoning steps and prune
           low-quality paths using an n-gram coherence + character-entropy
           heuristic (NGramHeuristicScorer). This is NOT a process reward
           model; see LATER-WAVE-REGISTRY for the reserved PRMScorer name.
  Level 3: MCTS — Monte Carlo Tree Search over decomposition candidates
            using UCB1 selection, expansion, simulation, and backpropagation

Usage::

    from vetinari.optimization.test_time_compute import TestTimeComputeScaler

    scaler = TestTimeComputeScaler()

    # Auto-select level based on task complexity (1-10 scale)
    level = scaler.auto_select_level(task_complexity=6)  # -> 2

    # Scale compute for a task
    result = scaler.scale(
        task="Implement a Redis cache with TTL support",
        level=2,
        evaluate_fn=lambda s: 0.9 if "ttl" in s.lower() else 0.5,
    )
    logger.debug(result.quality_estimate)
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from importlib import import_module
from typing import Any

from vetinari.exceptions import ConfigurationError

from .test_time_compute_mcts import MCTSPlanner
from .test_time_compute_models import ComputeResult, ComputeStepScore, MCTSNode, StepScore
from .test_time_compute_scorer import NGramHeuristicScorer

logger = logging.getLogger(__name__)
__all__ = [
    "ComputeResult",
    "ComputeStepScore",
    "MCTSNode",
    "MCTSPlanner",
    "NGramHeuristicScorer",
    "StepScore",
    "TestTimeComputeScaler",
    "get_test_time_scaler",
]


# UCB1 exploration constant: sqrt(2) is the theoretical optimum

# Minimum visit count before a node is eligible for UCB1 selection

# Entropy bounds for coherence scoring


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# NGramHeuristicScorer
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# MCTSPlanner
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# TestTimeComputeScaler
# ---------------------------------------------------------------------------


class TestTimeComputeScaler:
    """Orchestrates test-time compute scaling across 3 levels.

    Level 1: Best-of-N (existing) — generate N responses, pick best
    Level 2: Heuristic-guided — score reasoning steps with
             NGramHeuristicScorer, prune low-quality paths
    Level 3: MCTS — tree search over decomposition candidates

    Args:
        ngram_heuristic_scorer: Optional NGramHeuristicScorer instance.
            Created on first use if None.
        mcts_planner: Optional MCTSPlanner instance. Created on first use if None.
    """

    # Complexity thresholds for auto_select_level
    _L2_THRESHOLD: int = 4  # complexity >= 4 → Level 2
    _L3_THRESHOLD: int = 8  # complexity >= 8 → Level 3

    def __init__(
        self,
        ngram_heuristic_scorer: NGramHeuristicScorer | None = None,
        mcts_planner: MCTSPlanner | None = None,
    ) -> None:
        self._scorer = ngram_heuristic_scorer
        self._mcts = mcts_planner

    @property
    def ngram_heuristic_scorer(self) -> NGramHeuristicScorer:
        """Lazy-initialised n-gram heuristic scorer.

        Returns:
            NGramHeuristicScorer instance.
        """
        if self._scorer is None:
            self._scorer = NGramHeuristicScorer()
        return self._scorer

    @property
    def mcts_planner(self) -> MCTSPlanner:
        """Lazy-initialised MCTSPlanner.

        Returns:
            MCTSPlanner instance.
        """
        if self._mcts is None:
            self._mcts = MCTSPlanner()
        return self._mcts

    def scale(
        self,
        task: str,
        level: int,
        evaluate_fn: Callable[[str], float] | None = None,
        n: int = 3,
    ) -> ComputeResult:
        """Run the specified compute level and return the result.

        Args:
            task: Task description to process.
            level: Compute level 1, 2, or 3.
            evaluate_fn: Optional quality-scoring callable (required for L3;
                used as heuristic in L2 if provided).
            n: Number of candidates for Level 1 Best-of-N.

        Returns:
            ComputeResult with result string, quality estimate, and budget info.

        Raises:
            ValueError: If an unsupported level is requested.
        """
        if level == 1:
            return self._level1_best_of_n(task, n, evaluate_fn)
        if level == 2:
            return self._level2_prm(task, evaluate_fn)
        if level == 3:
            return self._level3_mcts(task, evaluate_fn)
        raise ConfigurationError(f"Unsupported compute level: {level}. Must be 1, 2, or 3.")

    def auto_select_level(self, task_complexity: int) -> int:
        """Recommend a compute level based on task complexity.

        Args:
            task_complexity: Integer complexity rating from 1 (trivial) to
                10 (extremely complex).

        Returns:
            Recommended level: 1, 2, or 3.
        """
        if task_complexity >= self._L3_THRESHOLD:
            return 3
        if task_complexity >= self._L2_THRESHOLD:
            return 2
        return 1

    # ------------------------------------------------------------------
    # Level implementations
    # ------------------------------------------------------------------

    def _level1_best_of_n(
        self,
        task: str,
        n: int,
        evaluate_fn: Callable[[str], float] | None,
    ) -> ComputeResult:
        """Level 1: generate N candidate responses, return the best.

        Attempts to delegate to BestOfNSelector from best_of_n module if
        available; falls back to an inline heuristic.

        Args:
            task: Task description.
            n: Number of candidates to generate.
            evaluate_fn: Optional scoring function.

        Returns:
            ComputeResult at level 1.
        """
        try:
            best_of_n: Any = import_module("vetinari.optimization.best_of_n")
            best_of_n_selector = best_of_n.BestOfNSelector

            selector = best_of_n_selector()
            candidates = selector.generate_candidates(task, n=n)
            best, score = selector.select_best(candidates, evaluate_fn=evaluate_fn)
            return ComputeResult(
                level_used=1,
                result=best,
                quality_estimate=score,
                steps_evaluated=n,
                computation_budget_used=float(n),
            )
        except ImportError:
            logger.debug("test_time_compute optional dependencies unavailable")

        # Inline fallback: no model-backed candidate generator is available, so
        # evaluate the original task only instead of fabricating candidates.
        candidates = [task]
        if evaluate_fn is not None:
            scored = [(c, evaluate_fn(c)) for c in candidates]
        else:
            scored = [(c, self.ngram_heuristic_scorer.score_step(c)) for c in candidates]

        best_candidate, best_score = max(scored, key=lambda x: x[1])
        logger.info("[L1] Best-of-%d selected candidate with score=%.3f", n, best_score)
        return ComputeResult(
            level_used=1,
            result=best_candidate,
            quality_estimate=best_score,
            steps_evaluated=len(candidates),
            computation_budget_used=float(len(candidates)),
        )

    def _level2_prm(
        self,
        task: str,
        evaluate_fn: Callable[[str], float] | None,
    ) -> ComputeResult:
        """Level 2: decompose task, score steps with PRM, prune and combine.

        Args:
            task: Task description.
            evaluate_fn: Optional external scoring function.

        Returns:
            ComputeResult at level 2.
        """
        # Decompose task into steps using MCTS heuristic decomposer
        raw_steps = self.mcts_planner._decompose_step(task)
        scored = self.ngram_heuristic_scorer.score_steps(raw_steps)
        kept = self.ngram_heuristic_scorer.prune_low_quality(raw_steps)

        if not kept:
            kept = raw_steps  # Fall back to all steps if everything was pruned

        combined = " -> ".join(kept)
        if evaluate_fn is not None:
            quality = evaluate_fn(combined)
        else:
            quality = sum(ss.score for ss in scored) / max(len(scored), 1)

        logger.info("[L2] PRM kept %d/%d steps, quality=%.3f", len(kept), len(raw_steps), quality)
        return ComputeResult(
            level_used=2,
            result=combined,
            quality_estimate=float(quality),
            steps_evaluated=len(raw_steps),
            computation_budget_used=float(len(raw_steps)) * 2.0,
        )

    def _level3_mcts(
        self,
        task: str,
        evaluate_fn: Callable[[str], float] | None,
    ) -> ComputeResult:
        """Level 3: MCTS search over decomposition space.

        Args:
            task: Task description.
            evaluate_fn: Scoring function. Defaults to PRM coherence if None.

        Returns:
            ComputeResult at level 3.
        """
        eff_evaluate = evaluate_fn if evaluate_fn is not None else self.ngram_heuristic_scorer.score_step
        path = self.mcts_planner.search(task, eff_evaluate)
        result = " -> ".join(path) if path else task
        quality = eff_evaluate(path[-1] if path else task)
        iterations = self.mcts_planner._max_iter

        logger.info("[L3] MCTS found path of %d steps, quality=%.3f", len(path), quality)
        return ComputeResult(
            level_used=3,
            result=result,
            quality_estimate=float(quality),
            steps_evaluated=iterations,
            computation_budget_used=float(iterations),
        )

    @staticmethod
    def _generate_simple_candidates(task: str, n: int) -> list[str]:
        """Generate simple textual variants of the task as L1 candidates.

        Args:
            task: Original task description.
            n: Number of candidates.

        Returns:
            List of n candidate strings.
        """
        variants: list[str] = [task]
        prefixes = [
            "Step by step: ",
            "Carefully consider and then: ",
            "Breaking it down: ",
            "Systematically: ",
        ]
        for i in range(n - 1):
            prefix = prefixes[i % len(prefixes)]
            variants.append(f"{prefix}{task}")
        return variants[:n]


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def get_test_time_scaler(**kwargs: Any) -> TestTimeComputeScaler:
    """Convenience factory returning a TestTimeComputeScaler.

    Args:
        **kwargs: Forwarded to TestTimeComputeScaler.__init__.

    Returns:
        A new TestTimeComputeScaler instance.
    """
    return TestTimeComputeScaler(**kwargs)
