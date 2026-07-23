"""Session state extraction — captures key decisions and outputs from pipeline stages.

This is step 1 of the three-tier compaction strategy: extract structured state
from pipeline output text using pattern matching. No LLM call required.

Roles in the pipeline: after each stage (planning, execution, review, assembly)
the caller passes the stage's raw output text here. The extractor identifies
decision sentences, produced artifacts, and any quality metrics embedded in the
text, and returns a frozen ``SessionState`` value object.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from vetinari.context.window_manager import count_tokens

logger = logging.getLogger(__name__)


# ── Pattern constants ──────────────────────────────────────────────────

# Words that signal a decision sentence. Order matters — more specific first.
_DECISION_KEYWORDS: tuple[str, ...] = (
    "decided",
    "chose",
    "selected",
    "will use",
    "switched",
    "approved",
    "rejected",
    "picked",
    "using",
)

# File-path heuristic: relative paths that look like Python/config/data files
_FILE_PATH_PATTERN = re.compile(r"\b(?:[a-zA-Z_][a-zA-Z0-9_]*/){1,5}[a-zA-Z_][a-zA-Z0-9_.]+\.[a-z]{1,5}\b")

# Quality-score patterns: "score: 0.85", "quality: 70%", "confidence: 0.9"
_SCORE_PATTERN = re.compile(
    r"(?P<metric>[a-z_]+)\s*[:\s=]+\s*(?P<value>\d+(?:\.\d+)?)(?:\s*%)?",
    re.IGNORECASE,
)

# Recognise code-block markers that indicate produced output
_CODE_BLOCK_PATTERN = re.compile(r"```[a-z]*\n[\s\S]+?```", re.MULTILINE)

# URL pattern (simple — captures http/https links)
_URL_PATTERN = re.compile(r"https?://[^\s\)\"']+")

# Verbs that indicate something was created or modified
_ARTIFACT_VERBS = re.compile(
    r"\b(?:created?|wrote|generated|modified|updated|produced|saved)\s+([^\n.]{3,80})",
    re.IGNORECASE,
)

# Known quality metric names — only these become quality_scores entries
_QUALITY_METRIC_NAMES: frozenset[str] = frozenset({
    "score",
    "quality",
    "confidence",
    "accuracy",
    "precision",
    "recall",
    "f1",
    "coverage",
    "completeness",
    "relevance",
})


# ── SessionState value object ──────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class SessionState:
    """Immutable snapshot of a pipeline stage's key outputs.

    Represents what happened during one pipeline stage without re-running
    any inference. All fields are populated by ``SessionStateExtractor.extract``
    from plain text analysis.

    Attributes:
        task_id: Unique identifier for the task this stage belongs to.
        stage: Pipeline stage name (e.g. ``"planning"``, ``"execution"``).
        key_decisions: Sentences from the stage output that contain decision
            language (chose, decided, selected, etc.).
        outputs_produced: File paths, URLs, code-block markers, and other
            artifact references found in the output text.
        quality_scores: Mapping of metric name to numeric score (0.0-1.0 or
            percentage normalised to 0.0-1.0) found in the output text.
        model_used: Model identifier that produced the stage output.
        token_count: Estimated token count of the original stage output text.
        timestamp: Unix timestamp (seconds) when extraction was performed.
    """

    task_id: str
    stage: str
    key_decisions: list[str]
    outputs_produced: list[str]
    quality_scores: dict[str, float]
    model_used: str
    token_count: int = 0
    timestamp: float = 0.0

    def __repr__(self) -> str:
        return (
            f"SessionState(task_id={self.task_id!r}, stage={self.stage!r}, "
            f"decisions={len(self.key_decisions)}, outputs={len(self.outputs_produced)})"
        )


# ── SessionStateExtractor ──────────────────────────────────────────────


class SessionStateExtractor:
    """Extracts structured session state from pipeline stage output text.

    Compiles all regex patterns once on construction and reuses them across
    many ``extract()`` calls. The extractor is stateless between calls —
    each invocation returns a new ``SessionState`` without mutating internal
    state.

    Intended to be used as a singleton via ``get_session_state_extractor()``.
    """

    def __init__(self, *, clock: Callable[[], float] | None = None) -> None:
        self._clock = clock or time.time
        # Pre-compile decision-keyword pattern to match full sentences
        kw_alts = "|".join(re.escape(kw) for kw in _DECISION_KEYWORDS)
        self._decision_sentence_re = re.compile(
            rf"[^.!?\n]*\b(?:{kw_alts})\b[^.!?\n]*[.!?]?",
            re.IGNORECASE,
        )
        logger.debug("SessionStateExtractor: compiled %d decision keyword patterns", len(_DECISION_KEYWORDS))

    # ── Public API ─────────────────────────────────────────────────────

    def extract(
        self,
        text: str,
        task_id: str,
        stage: str,
        model_id: str = "",
        metadata: dict[str, Any] | None = None,  # reserved for future extension
    ) -> SessionState:
        """Extract structured state from a pipeline stage's output text.

        Runs all pattern extractors over *text* and assembles the results into
        a frozen ``SessionState``. Never makes an LLM call.

        Args:
            text: Raw output text produced by the pipeline stage.
            task_id: Unique task identifier (e.g. ``"task-42"``).
            stage: Name of the pipeline stage (e.g. ``"planning"``,
                ``"execution"``, ``"review"``, ``"assembly"``).
            model_id: Identifier of the model that produced *text*.
            metadata: Optional extra context; reserved for future extension
                and currently unused.

        Returns:
            A frozen ``SessionState`` containing extracted decisions,
            outputs, quality scores, and token count.
        """
        if not text:
            logger.debug(
                "SessionStateExtractor.extract: empty text for task %s stage %s — returning empty state",
                task_id,
                stage,
            )
            return SessionState(
                task_id=task_id,
                stage=stage,
                key_decisions=[],
                outputs_produced=[],
                quality_scores={},
                model_used=model_id,
                token_count=0,
                timestamp=self._clock(),
            )

        decisions = self._extract_decisions(text)
        outputs = self._extract_outputs(text)
        scores = self._extract_quality_scores(text)
        tokens = self._estimate_tokens(text)

        logger.debug(
            "SessionStateExtractor: task=%s stage=%s decisions=%d outputs=%d scores=%d tokens=%d",
            task_id,
            stage,
            len(decisions),
            len(outputs),
            len(scores),
            tokens,
        )

        return SessionState(
            task_id=task_id,
            stage=stage,
            key_decisions=decisions,
            outputs_produced=outputs,
            quality_scores=scores,
            model_used=model_id,
            token_count=tokens,
            timestamp=self._clock(),
        )

    # ── Private extraction helpers ─────────────────────────────────────

    def _extract_decisions(self, text: str) -> list[str]:
        """Find sentences that contain decision-signalling keywords.

        Scans the text for sentences (split on ``.``, ``!``, ``?``, or
        newline) that include at least one keyword from ``_DECISION_KEYWORDS``.
        Results are deduplicated and stripped of leading/trailing whitespace.

        Args:
            text: The stage output text to scan.

        Returns:
            List of unique decision sentences, order-preserved.
        """
        matches = self._decision_sentence_re.findall(text)
        seen: set[str] = set()
        decisions: list[str] = []
        for raw in matches:
            clean = raw.strip()
            if clean and clean not in seen:
                seen.add(clean)
                decisions.append(clean)
        return decisions

    @staticmethod
    def _extract_outputs(text: str) -> list[str]:
        """Identify artifact references embedded in the stage output text.

        Looks for:
        - Relative file paths (e.g. ``vetinari/context/session_state.py``)
        - HTTP/HTTPS URLs
        - Code block markers (``` ... ```)
        - Artifact-verb phrases (``created``, ``generated``, ``saved`` + noun)

        Args:
            text: The stage output text to scan.

        Returns:
            Deduplicated list of artifact references found, order-preserved.
        """
        seen: set[str] = set()
        outputs: list[str] = []

        def _add(item: str) -> None:
            stripped = item.strip()
            if stripped and stripped not in seen:
                seen.add(stripped)
                outputs.append(stripped)

        for match in _FILE_PATH_PATTERN.finditer(text):
            _add(match.group())

        for match in _URL_PATTERN.finditer(text):
            _add(match.group())

        # Count code blocks rather than dumping their full content
        code_blocks = _CODE_BLOCK_PATTERN.findall(text)
        for i, _ in enumerate(code_blocks, start=1):
            _add(f"<code-block-{i}>")

        for match in _ARTIFACT_VERBS.finditer(text):
            _add(match.group(1).strip())

        return outputs

    @staticmethod
    def _extract_quality_scores(text: str) -> dict[str, float]:
        """Parse numeric quality metrics embedded in the stage output text.

        Recognises patterns such as ``score: 0.85``, ``quality: 70%``, and
        ``confidence: 0.9``. Only metrics whose names appear in
        ``_QUALITY_METRIC_NAMES`` are retained to avoid noise. Percentage
        values are normalised to the 0.0-1.0 range.

        Args:
            text: The stage output text to scan.

        Returns:
            Mapping of lowercase metric name to float score in [0.0, 1.0].
        """
        scores: dict[str, float] = {}
        for match in _SCORE_PATTERN.finditer(text):
            metric = match.group("metric").lower()
            if metric not in _QUALITY_METRIC_NAMES:
                continue
            try:
                raw_value = float(match.group("value"))
            except ValueError as exc:
                logger.warning(
                    "Skipping unparsable quality score %r in session-state text: %s",
                    match.group("value"),
                    exc,
                )
                continue
            # Normalise percentages - values > 1.0 are assumed to be 0-100 scale
            value = raw_value / 100.0 if raw_value > 1.0 else raw_value
            # Last occurrence wins so callers can override earlier values
            scores[metric] = round(value, 4)
        return scores

    def _estimate_tokens(self, text: str) -> int:
        """Count tokens in *text* through the shared exact-count path.

        Delegates to ``count_tokens`` from ``vetinari.context.window_manager``
        so that token-counting logic stays in one place.

        Args:
            text: Text whose token count is to be estimated.

        Returns:
            Estimated number of tokens.
        """
        return count_tokens(text)


# ── Module-level singleton ─────────────────────────────────────────────

# Who writes: get_session_state_extractor() (lazy initialisation, first caller wins)
# Who reads: any module that needs to extract state from stage output
# Lifecycle: created once per process; safe to share across threads (extractor is stateless)
# Lock: none required — Python GIL protects the None check in practice; double-init is harmless
_extractor: SessionStateExtractor | None = None
_extractor_lock = threading.Lock()


def get_session_state_extractor() -> SessionStateExtractor:
    """Get or create the singleton ``SessionStateExtractor``.

    Uses lazy initialisation — the extractor is compiled on first call and
    reused for all subsequent calls. Thread-safe under the GIL for the
    typical single-process server use case.

    Returns:
        The process-wide ``SessionStateExtractor`` instance.
    """
    global _extractor
    if _extractor is None:
        with _extractor_lock:
            if _extractor is None:
                _extractor = SessionStateExtractor()
    return _extractor


__all__ = [
    "SessionState",
    "SessionStateExtractor",
    "get_session_state_extractor",
]
