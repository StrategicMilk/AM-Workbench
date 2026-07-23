"""Context window tester — needle-in-haystack effective window measurement.

Inserts a key fact at various positions within a long context and
verifies retrieval accuracy. Records the EFFECTIVE context window
(where accuracy drops below threshold) vs the DECLARED window.

Pipeline role: Quality Gate — measures real model capability vs spec claims.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from vetinari.learning.atomic_writers import write_json_atomic

logger = logging.getLogger(__name__)


# -- Configuration --
_DEFAULT_RESULTS_DIR = Path(".vetinari") / "testing" / "context_window_results"

NEEDLE_POSITIONS = [0.25, 0.50, 0.75, 0.90, 0.95]  # Fraction of declared window
DEFAULT_ACCURACY_THRESHOLD = 0.8  # Minimum accuracy to count as "effective"
NEEDLE_FACT = "The secret code is ALPHA-7492"
NEEDLE_QUERY = "What is the secret code?"
NEEDLE_ANSWER = "ALPHA-7492"

# Repeating filler text for haystack construction
_FILLER_SENTENCE = (
    "The weather today is partly cloudy with a chance of rain in the afternoon. "
    "Local temperatures are expected to reach a high of 72 degrees. "
    "Residents are advised to carry an umbrella just in case. "
)
_FILLER_TOKENS_APPROX = len(_FILLER_SENTENCE) // 4  # Rough token estimate
_SAFE_MODEL_ID_RE = re.compile(r"^[A-Za-z0-9._:-]+$")
_GLOB_OR_PATH_CHARS = frozenset("*?[]/\\")


def _safe_model_id_for_cache(model_id: str) -> str:
    """Return a filename-safe model id or reject ambiguous cache keys."""
    if not model_id or any(char in model_id for char in _GLOB_OR_PATH_CHARS):
        raise ValueError("model_id must not contain path separators or glob metacharacters")
    if not _SAFE_MODEL_ID_RE.fullmatch(model_id):
        raise ValueError("model_id contains unsupported cache filename characters")
    return model_id


@dataclass(frozen=True, slots=True)
class PositionResult:
    """Result from testing needle retrieval at one position."""

    position_pct: float  # 0.25, 0.50, etc.
    token_position: int  # Approximate token position of the needle
    retrieved: bool  # Whether the model retrieved the needle answer
    accuracy: float  # 1.0 if retrieved, 0.0 if not (per single test)

    def __repr__(self) -> str:
        return f"PositionResult(position_pct={self.position_pct!r}, retrieved={self.retrieved!r})"


@dataclass(frozen=True, slots=True)
class ContextWindowReport:
    """Results from a full needle-in-haystack test run."""

    model_id: str
    declared_window: int  # Tokens declared by the model spec
    effective_window: int  # Last position where retrieval succeeded
    position_results: tuple[PositionResult, ...] = ()
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def __repr__(self) -> str:
        return (
            f"ContextWindowReport(model={self.model_id!r}, "
            f"declared={self.declared_window}, effective={self.effective_window})"
        )


class ContextWindowTester:
    """Test effective context window using needle-in-haystack probes.

    Inserts a key fact at 25%, 50%, 75%, 90%, and 95% of the declared
    context window and tests whether the model can retrieve it.

    Args:
        results_dir: Directory for result JSON files.
        accuracy_threshold: Minimum accuracy to consider a position "effective".
        num_trials: Number of trials per position for statistical reliability.
    """

    def __init__(
        self,
        results_dir: Path | None = None,
        accuracy_threshold: float = DEFAULT_ACCURACY_THRESHOLD,
        num_trials: int = 1,
    ) -> None:
        self._results_dir = results_dir or _DEFAULT_RESULTS_DIR
        self._accuracy_threshold = accuracy_threshold
        self._num_trials = max(1, num_trials)

    def build_haystack(self, target_tokens: int, needle_position_pct: float) -> str:
        """Build a haystack text with a needle inserted at the target position.

        Args:
            target_tokens: Total target length in approximate tokens.
            needle_position_pct: Where to insert the needle (0.0-1.0).

        Returns:
            Haystack text with the needle fact embedded.
        """
        # Calculate how many filler repetitions we need
        total_chars = target_tokens * 4  # Rough chars-per-token estimate
        needle_char_pos = int(total_chars * needle_position_pct)

        # Build filler before needle
        before_chars = max(0, needle_char_pos)
        reps_before = (before_chars // len(_FILLER_SENTENCE)) + 1
        filler_before = (_FILLER_SENTENCE * reps_before)[:before_chars]

        # Build filler after needle
        after_chars = max(0, total_chars - needle_char_pos - len(NEEDLE_FACT))
        reps_after = (after_chars // len(_FILLER_SENTENCE)) + 1
        filler_after = (_FILLER_SENTENCE * reps_after)[:after_chars]

        return f"{filler_before}\n{NEEDLE_FACT}\n{filler_after}"

    def check_retrieval(self, response: str) -> bool:
        """Check if the model response contains the needle answer.

        Args:
            response: The model's response to the needle query.

        Returns:
            True if the response contains the expected answer.
        """
        return NEEDLE_ANSWER.lower() in response.lower()

    def run_needle_test(
        self,
        model_id: str,
        declared_window: int,
        inference_fn: Callable[[str], str],
    ) -> ContextWindowReport:
        """Run needle-in-haystack test at multiple positions.

        Args:
            model_id: Identifier for the model being tested.
            declared_window: The model's declared context window in tokens.
            inference_fn: Callable that takes a prompt and returns model output.

        Returns:
            ContextWindowReport with per-position results and effective window.
        """
        results: list[PositionResult] = []
        effective_window = 0
        contiguous_retrieval = True

        for position_pct in NEEDLE_POSITIONS:
            token_position = int(declared_window * position_pct)
            successes = 0

            for _ in range(self._num_trials):
                haystack = self.build_haystack(declared_window, position_pct)
                prompt = f"{haystack}\n\nQuestion: {NEEDLE_QUERY}"

                try:
                    response = inference_fn(prompt)
                    if self.check_retrieval(response):
                        successes += 1
                except Exception:
                    logger.warning(
                        "Inference failed at position %.0f%% for %s — counting as miss",
                        position_pct * 100,
                        model_id,
                    )

            accuracy = successes / self._num_trials
            retrieved = accuracy >= self._accuracy_threshold

            results.append(
                PositionResult(
                    position_pct=position_pct,
                    token_position=token_position,
                    retrieved=retrieved,
                    accuracy=accuracy,
                )
            )

            if retrieved and contiguous_retrieval:
                effective_window = token_position
            elif not retrieved:
                contiguous_retrieval = False

            logger.info(
                "Needle test %s @ %.0f%%: accuracy=%.2f retrieved=%s",
                model_id,
                position_pct * 100,
                accuracy,
                retrieved,
            )

        report = ContextWindowReport(
            model_id=model_id,
            declared_window=declared_window,
            effective_window=effective_window,
            position_results=tuple(results),
        )
        self._store_results(report)
        return report

    def _store_results(self, report: ContextWindowReport) -> None:
        """Persist context window results as JSON.

        Args:
            report: The ContextWindowReport to store.
        """
        self._results_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        model_key = _safe_model_id_for_cache(report.model_id)
        out_path = self._results_dir / f"context_window_{model_key}_{ts}.json"

        data = {
            "model_id": report.model_id,
            "declared_window": report.declared_window,
            "effective_window": report.effective_window,
            "timestamp": report.timestamp,
            "position_results": [
                {
                    "position_pct": r.position_pct,
                    "token_position": r.token_position,
                    "retrieved": r.retrieved,
                    "accuracy": r.accuracy,
                }
                for r in report.position_results
            ],
        }
        write_json_atomic(out_path, data)
        logger.info("Context window results stored at %s", out_path)


def get_effective_window(
    model_id: str,
    results_dir: Path | None = None,
) -> int | None:
    """Look up the most recent effective window measurement for a model.

    Args:
        model_id: The model identifier to look up.
        results_dir: Directory containing result JSON files.

    Returns:
        Effective window in tokens, or None if no measurement exists.
    """
    search_dir = results_dir or _DEFAULT_RESULTS_DIR
    if not search_dir.exists():
        return None

    # Find the most recent result file for this model
    model_key = _safe_model_id_for_cache(model_id)
    pattern = f"context_window_{model_key}_*.json"
    files = sorted(search_dir.glob(pattern), reverse=True)
    if not files:
        return None

    with open(files[0], encoding="utf-8") as f:
        data = json.load(f)
    return data.get("effective_window")
