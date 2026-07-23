"""Word-count completeness checks backed by deterministic Thompson samples."""

from __future__ import annotations

from dataclasses import dataclass

from vetinari.workbench.thompson.sampler import ThompsonArm, select_arm


@dataclass(frozen=True, slots=True)
class WordCountCompletenessVerdict:
    """Completeness result for expected-vs-actual word counts."""

    complete: bool
    ratio: float
    selected_arm_id: str
    reason: str

    def __repr__(self) -> str:
        return (
            "WordCountCompletenessVerdict("
            f"complete={self.complete}, ratio={self.ratio!r}, "
            f"selected_arm_id={self.selected_arm_id!r}, reason={self.reason!r})"
        )


def evaluate_word_count_completeness(
    *,
    expected_words: int,
    observed_words: int,
    successful_samples: int,
    failed_samples: int,
    minimum_samples: int = 3,
    seed: int = 0,
) -> WordCountCompletenessVerdict:
    """Fail closed until enough deterministic samples support completeness.

    Returns:
        A completeness verdict with sample provenance.

    Raises:
        ValueError: if expected or observed word counts are invalid.
    """
    if expected_words < 1:
        raise ValueError("expected_words must be positive")
    if observed_words < 0:
        raise ValueError("observed_words must be non-negative")
    sample_count = successful_samples + failed_samples
    ratio = observed_words / expected_words
    if sample_count < minimum_samples:
        return WordCountCompletenessVerdict(False, ratio, "insufficient", "minimum_sample_count_not_met")
    arms = (
        ThompsonArm("complete", alpha=max(successful_samples, 1), beta=max(failed_samples, 1)),
        ThompsonArm("incomplete", alpha=max(failed_samples, 1), beta=max(successful_samples, 1)),
    )
    selected = select_arm(arms, seed=seed)
    complete = ratio >= 1.0 and selected.arm_id == "complete"
    return WordCountCompletenessVerdict(
        complete=complete,
        ratio=ratio,
        selected_arm_id=selected.arm_id,
        reason="complete" if complete else "word_count_or_sample_evidence_insufficient",
    )


__all__ = ["WordCountCompletenessVerdict", "evaluate_word_count_completeness"]
