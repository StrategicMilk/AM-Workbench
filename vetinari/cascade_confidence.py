"""Confidence estimation helpers for cascade routing."""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

_UNCERTAINTY_PATTERNS = (
    r"\bi('m| am) not sure\b",
    r"\bi don'?t know\b",
    r"\bit('s| is) unclear\b",
    r"\buncertain\b",
    r"\bI cannot (determine|say|tell)\b",
    r"\bI'?m unable to\b",
    r"\bI lack (the |)information\b",
    r"\bcannot (answer|provide|help)\b",
)
_REFUSAL_PATTERNS = (
    r"\bI can'?t (help|do|provide)\b",
    r"\bI (am|'m) not able to\b",
    r"\bThis (is|seems) beyond\b",
)
_DEFAULT_HEURISTIC_WEIGHT = 0.4
_DEFAULT_LLM_WEIGHT = 0.6


def _confidence_blend_weights() -> tuple[float, float]:
    try:
        from vetinari.config.ml_config import get_ml_config

        raw = get_ml_config().get("cascade_confidence", {})
    except Exception:
        logger.warning("Cascade confidence config unavailable; using default blend weights", exc_info=True)
        raw = {}
    heuristic = _validated_weight(raw, "heuristic_weight", _DEFAULT_HEURISTIC_WEIGHT)
    llm = _validated_weight(raw, "llm_weight", _DEFAULT_LLM_WEIGHT)
    total = heuristic + llm
    if total <= 0:
        return _DEFAULT_HEURISTIC_WEIGHT, _DEFAULT_LLM_WEIGHT
    return heuristic / total, llm / total


def _validated_weight(raw: Any, key: str, default: float) -> float:
    if not isinstance(raw, dict):
        return default
    value = raw.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        return default
    return float(value)


def heuristic_confidence(response_text: str, task_description: str = "") -> float:
    """Estimate confidence from response text, optionally using LLM judgment.

    Args:
        response_text: Model response text to score.
        task_description: Optional task context for LLM-conditioned scoring.

    Returns:
        Confidence score in the inclusive ``0.0`` to ``1.0`` range.
    """
    if not response_text:
        return 0.0

    text = response_text.strip()
    heuristic_score = _base_heuristic_score(text)
    if not task_description:
        return heuristic_score

    try:
        from vetinari.llm_helpers import score_confidence_via_llm

        llm_score = score_confidence_via_llm(task_description, text[:500])
        if llm_score is not None:
            heuristic_weight, llm_weight = _confidence_blend_weights()
            blended = heuristic_score * heuristic_weight + llm_score * llm_weight
            logger.debug(
                "Confidence blend: heuristic=%.2f, llm=%.2f, weights=(%.2f, %.2f), blended=%.2f",
                heuristic_score,
                llm_score,
                heuristic_weight,
                llm_weight,
                blended,
            )
            return round(blended, 3)
    except Exception:
        logger.warning("LLM confidence scoring unavailable; using heuristic score only for routing")
    return heuristic_score


def _base_heuristic_score(text: str) -> float:
    """Return the local confidence heuristic score for response text."""
    score = 1.0
    if len(text) < 20:
        score -= 0.4
    if any(re.search(pattern, text, re.IGNORECASE) for pattern in _UNCERTAINTY_PATTERNS):
        score -= 0.35
    if any(re.search(pattern, text, re.IGNORECASE) for pattern in _REFUSAL_PATTERNS):
        score -= 0.15
    if text and text[-1] not in ".!?\"'`":
        score -= 0.05
    sentences = re.split(r"[.!?]+", text)
    if len(sentences) > 3:
        unique = len({sentence.strip().lower() for sentence in sentences if sentence.strip()})
        if unique < len(sentences) * 0.5:
            score -= 0.2
    return max(0.0, min(1.0, score))
