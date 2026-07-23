"""N-gram heuristic scoring for test-time compute."""

from __future__ import annotations

import logging
import math

from .test_time_compute_models import ComputeStepScore

logger = logging.getLogger(__name__)
_ENTROPY_LOW_THRESHOLD: float = 0.5
_ENTROPY_HIGH_THRESHOLD: float = 4.5


class NGramHeuristicScorer:
    """Scores intermediate reasoning steps using an n-gram coherence heuristic.

    This is NOT a process reward model. It computes a deterministic score
    from two signals over the candidate step and the preceding context:

      - Bigram overlap between the step and the accumulated context.
      - Shannon entropy over the step's character distribution.

    The final score is a weighted blend (0.6 * overlap + 0.4 * entropy_score)
    clamped to [0.0, 1.0]. There is no learned reward model loaded and no
    value head trained on preference data — the scorer is strictly heuristic.

    The name `PRMScorer` is reserved for a future implementation that loads
    an actual process-reward-model head. Follow-up ownership is tracked in
    the private program finding registry.

    Args:
        coherence_threshold: Steps scoring below this are considered
            low-quality and eligible for pruning (0.0-1.0).
    """

    def __init__(self, coherence_threshold: float = 0.5) -> None:
        self._threshold = coherence_threshold

    def score_steps(self, steps: list[str]) -> list[ComputeStepScore]:
        """Score each step independently, passing the preceding steps as context.

        Args:
            steps: Ordered list of reasoning step strings.

        Returns:
            List of StepScore objects, one per input step.
        """
        context = ""
        scored: list[ComputeStepScore] = []
        for step in steps:
            s = self.score_step(step, context)
            reasoning = self._describe_score(s, step)
            scored.append(ComputeStepScore(step_text=step, score=s, reasoning=reasoning))
            context = f"{context} {step}".strip()
        return scored

    def score_step(self, step: str, context: str = "") -> float:
        """Score a single step given the preceding context.

        Args:
            step: The reasoning step text to evaluate.
            context: All prior steps concatenated (may be empty string).

        Returns:
            Quality score in [0.0, 1.0].
        """
        if not step.strip():
            return 0.0
        return self._compute_coherence(step, context)

    def prune_low_quality(
        self,
        steps: list[str],
        threshold: float | None = None,
    ) -> list[str]:
        """Remove steps that score below the coherence threshold.

        Args:
            steps: Ordered list of reasoning step strings.
            threshold: Override the instance-level threshold for this call.

        Returns:
            Filtered list containing only steps that pass the threshold.
        """
        cutoff = threshold if threshold is not None else self._threshold
        scored = self.score_steps(steps)
        kept = [ss.step_text for ss in scored if ss.score >= cutoff]
        pruned = len(steps) - len(kept)
        if pruned:
            logger.info("[NGramHeuristicScorer] Pruned %d/%d low-quality steps", pruned, len(steps))
        return kept

    def _compute_coherence(self, text: str, context: str) -> float:
        """Compute a coherence score using n-gram overlap and entropy.

        Strategy:
        - N-gram overlap: fraction of text bigrams that also appear in context.
          High overlap = the step continues existing threads (good).
        - Entropy: Shannon entropy over character frequencies. Very low = trivial;
          very high = random noise. We reward the middle ground.
        - Final score = 0.6 * overlap + 0.4 * entropy_score

        Args:
            text: The step text to evaluate.
            context: All prior text (may be empty).

        Returns:
            Coherence score in [0.0, 1.0].
        """
        # --- n-gram overlap ---
        overlap_score = self._ngram_overlap(text, context) if context else 0.5

        # --- character-level Shannon entropy ---
        entropy = self._char_entropy(text)
        if entropy < _ENTROPY_LOW_THRESHOLD:
            entropy_score = entropy / _ENTROPY_LOW_THRESHOLD * 0.3
        elif entropy > _ENTROPY_HIGH_THRESHOLD:
            excess = entropy - _ENTROPY_HIGH_THRESHOLD
            entropy_score = max(0.0, 1.0 - excess * 0.3)
        else:
            # Linear ramp from 0.3 at low threshold to 1.0 at mid, back to 0.7 at high
            mid = (_ENTROPY_LOW_THRESHOLD + _ENTROPY_HIGH_THRESHOLD) / 2
            if entropy <= mid:
                entropy_score = 0.3 + 0.7 * (entropy - _ENTROPY_LOW_THRESHOLD) / (mid - _ENTROPY_LOW_THRESHOLD)
            else:
                entropy_score = 1.0 - 0.3 * (entropy - mid) / (_ENTROPY_HIGH_THRESHOLD - mid)

        coherence = 0.6 * overlap_score + 0.4 * entropy_score
        return round(min(1.0, max(0.0, coherence)), 4)

    @staticmethod
    def _ngram_overlap(text: str, context: str, n: int = 2) -> float:
        """Fraction of text n-grams that appear in context.

        Args:
            text: The text to analyse.
            context: Reference text.
            n: N-gram size.

        Returns:
            Overlap ratio in [0.0, 1.0].
        """
        text_lower = text.lower()
        context_lower = context.lower()

        text_grams = {text_lower[i : i + n] for i in range(len(text_lower) - n + 1)}
        if not text_grams:
            return 0.5

        context_grams = {context_lower[i : i + n] for i in range(len(context_lower) - n + 1)}
        if not context_grams:
            return 0.5  # No context yet — neutral score

        overlap = len(text_grams & context_grams) / len(text_grams)
        # Clamp: very high overlap (copying) is also penalised slightly
        return min(1.0, overlap * 1.5) if overlap < 0.8 else 0.6 + overlap * 0.1

    @staticmethod
    def _char_entropy(text: str) -> float:
        """Shannon entropy over character frequency distribution.

        Args:
            text: Input text.

        Returns:
            Entropy in bits (unbounded above, typically 0-5 for natural text).
        """
        if not text:
            return 0.0
        freq: dict[str, int] = {}
        for ch in text:
            freq[ch] = freq.get(ch, 0) + 1
        total = len(text)
        return -sum((c / total) * math.log2(c / total) for c in freq.values())

    @staticmethod
    def _describe_score(score: float, step: str) -> str:
        """Generate a human-readable explanation for a step score.

        Args:
            score: Computed coherence score.
            step: The step text.

        Returns:
            Short reasoning string.
        """
        length = len(step.strip())
        if score >= 0.8:
            return f"High coherence (score={score:.2f}): step is well-structured and {length} chars"
        if score >= 0.5:
            return f"Moderate coherence (score={score:.2f}): step is adequate at {length} chars"
        return f"Low coherence (score={score:.2f}): step may be too short, repetitive, or noisy ({length} chars)"
