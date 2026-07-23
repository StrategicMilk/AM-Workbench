"""Fixture-backed RAG faithfulness calibration helpers."""

from __future__ import annotations

from dataclasses import dataclass
from statistics import fmean


class FaithfulnessCalibrationError(ValueError):
    """Raised when faithfulness calibration evidence is missing or insufficient."""


@dataclass(frozen=True, slots=True)
class FaithfulnessCalibrationExample:
    """One annotated faithfulness example."""

    example_id: str
    score: float
    faithful: bool

    def __post_init__(self) -> None:
        if not self.example_id.strip():
            raise ValueError("example_id must be non-empty")
        if not 0.0 <= self.score <= 1.0:
            raise ValueError("score must be between 0 and 1")


@dataclass(frozen=True, slots=True)
class FaithfulnessCalibration:
    """Calibrated threshold and provenance for RAG faithfulness."""

    threshold: float
    source: str
    example_count: int
    positive_count: int
    negative_count: int

    def __repr__(self) -> str:
        return (
            "FaithfulnessCalibration("
            f"threshold={self.threshold!r}, source={self.source!r}, "
            f"example_count={self.example_count}, positive_count={self.positive_count}, "
            f"negative_count={self.negative_count})"
        )


def calibrate_faithfulness_threshold(
    examples: tuple[FaithfulnessCalibrationExample, ...],
    *,
    minimum_examples: int = 4,
    source: str = "fixture_backed_annotation",
) -> FaithfulnessCalibration:
    """Calibrate a threshold from labeled examples and record its source.

    Returns:
        The calibrated threshold and provenance counts.

    Raises:
        FaithfulnessCalibrationError: if examples are insufficient or do not
            include both positive and negative labels.
    """
    if len(examples) < minimum_examples:
        raise FaithfulnessCalibrationError("minimum annotated example count not met")
    positives = tuple(example.score for example in examples if example.faithful)
    negatives = tuple(example.score for example in examples if not example.faithful)
    if not positives or not negatives:
        raise FaithfulnessCalibrationError("calibration requires positive and negative examples")
    threshold = (min(positives) + max(negatives)) / 2.0
    if max(negatives) >= min(positives):
        threshold = fmean((fmean(positives), fmean(negatives)))
    return FaithfulnessCalibration(
        threshold=max(0.0, min(1.0, float(threshold))),
        source=source,
        example_count=len(examples),
        positive_count=len(positives),
        negative_count=len(negatives),
    )


__all__ = [
    "FaithfulnessCalibration",
    "FaithfulnessCalibrationError",
    "FaithfulnessCalibrationExample",
    "calibrate_faithfulness_threshold",
]
