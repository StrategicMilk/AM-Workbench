"""LLMLingua-style prompt compression using character n-gram entropy (Story 43).

Compresses prompts by removing low-information-density phrases while
preserving structural elements (headers, code blocks, explicit preserve
patterns).  Uses character n-gram Shannon entropy as a proxy for token-level
perplexity — high-entropy segments carry more information and are retained;
low-entropy segments are candidates for removal.

No external ML dependencies required.  Pure Python implementation.
"""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass, replace

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_NGRAM_SIZE: int = 3  # character n-gram length for entropy computation
_MIN_SEGMENT_LENGTH: int = 5  # segments shorter than this are always kept


@dataclass(frozen=True, slots=True)
class TextSegment:
    """A single segment produced by splitting a prompt for compression.

    Attributes:
        text: The segment text content.
        start_pos: Byte/character offset in the original text.
        end_pos: End offset (exclusive).
        is_structural: True if this segment must not be removed.
        entropy_score: Character n-gram Shannon entropy (higher = more info).
    """

    text: str
    start_pos: int
    end_pos: int
    is_structural: bool = False
    entropy_score: float = 0.0

    def __repr__(self) -> str:
        """Show key identifying fields for debugging."""
        return (
            f"TextSegment(start_pos={self.start_pos!r},"
            f" end_pos={self.end_pos!r},"
            f" is_structural={self.is_structural!r}, entropy_score={self.entropy_score!r})"
        )


class PerplexityCompressor:
    """Compresses prompts by removing low-information tokens.

    Uses character n-gram entropy as a proxy for perplexity scoring.
    Preserves structural elements (headers, code blocks, key terms)
    regardless of their information score.

    Args:
        preserve_patterns: Additional regex patterns whose matching lines are
            always preserved.  Headers and fenced code blocks are always
            preserved regardless of this parameter.
    """

    # Structural patterns that are unconditionally preserved
    _HEADER_RE = re.compile(r"^#{1,6}\s+", re.MULTILINE)
    _CODE_FENCE_RE = re.compile(r"^```", re.MULTILINE)

    def __init__(self, preserve_patterns: list[str] | None = None) -> None:
        self._preserve_patterns: list[re.Pattern[str]] = []
        for pat in preserve_patterns or []:
            try:
                self._preserve_patterns.append(re.compile(pat))
            except re.error as exc:
                logger.warning("PerplexityCompressor: invalid preserve pattern %r: %s", pat, exc)

    # ── Public API ────────────────────────────────────────────────────

    def compress(self, text: str, target_ratio: float = 0.7) -> str:
        """Compress *text* to approximately *target_ratio* of its original length.

        Splits text into segments, scores each by entropy, and removes the
        lowest-scoring non-structural segments until the length target is met.

        Args:
            text: The prompt text to compress.
            target_ratio: Target length as a fraction of the original (0-1).
                Values >= 1.0 return the original text unchanged.

        Returns:
            Compressed prompt string.
        """
        if not text or target_ratio >= 1.0:
            return text

        target_len = max(1, int(len(text) * target_ratio))
        segments = self._split_into_segments(text)

        if not segments:
            return text

        # Score non-structural segments
        segments = [
            replace(seg, entropy_score=self._compute_entropy(seg.text)) if not seg.is_structural else seg
            for seg in segments
        ]

        # Sort removable segments by entropy ascending (lowest info first)
        removable = [s for s in segments if not s.is_structural and len(s.text.strip()) >= _MIN_SEGMENT_LENGTH]
        removable.sort(key=lambda s: s.entropy_score)

        # Mark segments for removal until we reach the target length
        removed: set[int] = set()
        current_len = sum(len(s.text) for s in segments)

        for seg in removable:
            if current_len <= target_len:
                break
            removed.add(id(seg))
            current_len -= len(seg.text)

        result_parts = [s.text for s in segments if id(s) not in removed]
        compressed = "".join(result_parts)

        logger.debug(
            "PerplexityCompressor: %d -> %d chars (%.1f%% of original)",
            len(text),
            len(compressed),
            100.0 * len(compressed) / max(1, len(text)),
        )
        return compressed

    @staticmethod
    def _compute_entropy(text: str) -> float:
        """Compute character n-gram Shannon entropy for *text*.

        Higher entropy indicates more varied/informative content.

        Args:
            text: Input string.

        Returns:
            Shannon entropy value (bits).  Returns 0.0 for very short strings.
        """
        if len(text) < _NGRAM_SIZE:
            return 0.0

        t = text.lower()
        ngrams: dict[str, int] = {}
        total = 0
        for i in range(len(t) - _NGRAM_SIZE + 1):
            ng = t[i : i + _NGRAM_SIZE]
            ngrams[ng] = ngrams.get(ng, 0) + 1
            total += 1

        if total == 0:
            return 0.0

        entropy = 0.0
        for count in ngrams.values():
            prob = count / total
            entropy -= prob * math.log2(prob)
        return entropy

    def _split_into_segments(self, text: str) -> list[TextSegment]:
        """Split *text* into segments preserving structure.

        Code blocks (``` … ```) are treated as atomic structural units.
        All other content is split line-by-line.

        Args:
            text: The full prompt text.

        Returns:
            Ordered list of :class:`TextSegment` objects covering the full text.
        """
        segments: list[TextSegment] = []

        # Identify fenced code block ranges first
        code_block_ranges: list[tuple[int, int]] = []
        fence_positions: list[int] = [m.start() for m in self._CODE_FENCE_RE.finditer(text)]
        for i in range(0, len(fence_positions) - 1, 2):
            start = fence_positions[i]
            # Find end of closing fence line
            end_line_end = text.find("\n", fence_positions[i + 1])
            end = end_line_end + 1 if end_line_end != -1 else len(text)
            code_block_ranges.append((start, end))

        def _in_code_block(pos: int) -> bool:
            return any(s <= pos < e for s, e in code_block_ranges)

        # Walk lines
        pos = 0
        for line in text.splitlines(keepends=True):
            line_start = pos
            line_end = pos + len(line)

            if _in_code_block(line_start):
                # Merge into preceding code-block segment or start new one
                if segments and segments[-1].is_structural and _in_code_block(segments[-1].start_pos):
                    segments[-1] = replace(
                        segments[-1],
                        text=f"{segments[-1].text}{line}",
                        end_pos=line_end,
                    )
                else:
                    segments.append(
                        TextSegment(
                            text=line,
                            start_pos=line_start,
                            end_pos=line_end,
                            is_structural=True,
                        ),
                    )
            else:
                structural = self._is_structural_line(line)
                segments.append(
                    TextSegment(
                        text=line,
                        start_pos=line_start,
                        end_pos=line_end,
                        is_structural=structural,
                    ),
                )

            pos = line_end

        # Handle any trailing text not ending with newline
        if pos < len(text):
            remainder = text[pos:]
            segments.append(
                TextSegment(
                    text=remainder,
                    start_pos=pos,
                    end_pos=len(text),
                    is_structural=self._is_structural_line(remainder),
                ),
            )

        return segments

    def _is_structural(self, segment: TextSegment) -> bool:
        """Return True if *segment* must be preserved unconditionally.

        Checks for headers, code fences, and user-supplied preserve patterns.

        Args:
            segment: The segment to evaluate.

        Returns:
            True if structural.
        """
        return self._is_structural_line(segment.text)

    def _is_structural_line(self, line: str) -> bool:
        """Return True if *line* is a structural element that must be preserved.

        Args:
            line: A single line of text (may include trailing newline).

        Returns:
            True if the line is a header, code fence, or matches any preserve
            pattern.
        """
        stripped = line.strip()
        if not stripped:
            return False
        if self._HEADER_RE.match(stripped):
            return True
        if stripped.startswith("```"):
            return True
        return any(pat.search(stripped) for pat in self._preserve_patterns)


# ---------------------------------------------------------------------------
# Convenience function
# ---------------------------------------------------------------------------


def compress_for_rag(context: str, max_tokens: int = 2048) -> str:
    """Compress RAG-retrieved context to fit within a token budget.

    Estimates token count as ``len(text) // 4`` and compresses accordingly.
    Uses a module-level :class:`PerplexityCompressor` with default settings.

    Args:
        context: The RAG-retrieved context string to compress.
        max_tokens: Maximum token budget.  Content is compressed until the
            estimated token count is within this limit.

    Returns:
        Compressed context string, or the original if already within budget.
    """
    estimated_tokens = len(context) // 4
    if estimated_tokens <= max_tokens:
        return context

    target_ratio = (max_tokens * 4) / max(1, len(context))
    target_ratio = max(0.1, min(target_ratio, 1.0))

    compressor = _get_default_compressor()
    return compressor.compress(context, target_ratio=target_ratio)


_default_compressor: PerplexityCompressor | None = None


def _get_default_compressor() -> PerplexityCompressor:
    """Return a lazily-created module-level :class:`PerplexityCompressor`.

    Returns:
        Singleton :class:`PerplexityCompressor` instance.
    """
    global _default_compressor
    if _default_compressor is None:
        _default_compressor = PerplexityCompressor()
    return _default_compressor
