"""High-level API for retrieving relevant past experiences.

Wraps EpisodeMemory with planning-oriented retrieval:
- Recall by task similarity
- Recall failure patterns for avoidance
- Recall successful strategies for reuse
- Adaptive retrieval, only on low confidence, for efficiency
- Importance scoring: recency * quality_impact * novelty

Simplifies the interface for plan generators and prompt assemblers
that need past experience context.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from numbers import Real
from typing import Any

from vetinari.boundary_guards import require_nonempty
from vetinari.types import ConfidenceLevel
from vetinari.validation import NumericValidationError, validate_numeric_signal

logger = logging.getLogger(__name__)


_RETRIEVAL_CONFIDENCE_LEVELS: frozenset[ConfidenceLevel] = frozenset({
    ConfidenceLevel.LOW,
    ConfidenceLevel.VERY_LOW,
})
_MAX_RECALL_RESULTS = 50


class EpisodicRecallError(RuntimeError):
    """Raised when episodic recall cannot safely produce trustworthy context."""


def _validate_non_empty_text(value: str, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip()


def _validate_recall_limit(k: int) -> int:
    signal = validate_numeric_signal(
        k,
        field_name="k",
        minimum=1,
        maximum=_MAX_RECALL_RESULTS,
        source="episodic_recall",
    )
    if not signal.value.is_integer():
        raise NumericValidationError("k must be an integer")
    return int(signal.value)


def _validate_confidence_level(confidence_level: ConfidenceLevel | str) -> ConfidenceLevel:
    if isinstance(confidence_level, ConfidenceLevel):
        return confidence_level
    if isinstance(confidence_level, str):
        try:
            return ConfidenceLevel(confidence_level)
        except ValueError as exc:
            raise ValueError(f"unknown confidence_level: {confidence_level}") from exc
    raise ValueError("confidence_level must be a ConfidenceLevel or known confidence string")


def _episode_timestamp_seconds(value: Any, *, default: float) -> float:
    if value is None:
        return default
    if isinstance(value, bool):
        raise ValueError("episode timestamp must be a real Unix timestamp or ISO datetime string")
    if isinstance(value, Real):
        return validate_numeric_signal(
            value,
            field_name="episode_timestamp",
            minimum=0,
            maximum=default + 86_400,
            source="episodic_recall",
        ).value
    if isinstance(value, str):
        text = require_nonempty(value, field_name="episode_timestamp")
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return validate_numeric_signal(
            parsed.timestamp(),
            field_name="episode_timestamp",
            minimum=0,
            maximum=default + 86_400,
            source="episodic_recall",
        ).value
    raise ValueError("episode timestamp must be a real Unix timestamp or ISO datetime string")


def _episode_quality_score(value: Any) -> float:
    if value is None:
        raise NumericValidationError("episode_quality_score is required")
    return validate_numeric_signal(
        value,
        field_name="episode_quality_score",
        source="episodic_recall",
    ).value


def _raise_recall_failure(operation: str, exc: Exception) -> None:
    logger.error(
        "Episodic recall failed during %s",
        operation,
        exc_info=(type(exc), exc, exc.__traceback__),
    )
    raise EpisodicRecallError(
        f"episodic recall failed during {operation}; recover memory backend before using history context"
    ) from exc


def _memory_context_metadata(ep: Any) -> dict[str, Any] | None:
    """Return provenance and trust controls for an episode, or None if unsafe."""
    metadata = getattr(ep, "metadata", {})
    if not isinstance(metadata, dict):
        metadata = {}
    if metadata.get("requires_memory_firewall") or metadata.get("candidate_only"):
        return None
    trust_status = str(metadata.get("trust_status") or metadata.get("candidate_status") or "verified")
    if trust_status.lower() in {"untrusted", "quarantined", "blocked", "candidate"}:
        return None
    return {
        "memory_episode_id": getattr(ep, "episode_id", ""),
        "memory_provenance": metadata.get("provenance") or getattr(ep, "provenance", "episode_memory"),
        "memory_trust_status": trust_status,
    }


def recall_for_planning(
    goal: str,
    task_type: str = "general",
    k: int = 3,
) -> list[dict[str, Any]]:
    """Recall relevant past episodes for plan generation context.

    Returns successful episodes matching the goal description, formatted
    as planning context dicts with task_summary, output_summary, and quality.

    Args:
        goal: Goal or task description used as the recall query.
        task_type: Task category filter for the memory backend.
        k: Maximum number of successful episodes to return.

    Returns:
        Episode summaries suitable for injection into planning context.

    Raises:
        ValueError: If text inputs are blank or the recall limit is invalid.
        EpisodicRecallError: If the memory backend or recalled episode data
            cannot produce trustworthy context.
    """
    goal = _validate_non_empty_text(goal, field_name="goal")
    task_type = _validate_non_empty_text(task_type, field_name="task_type")
    k = _validate_recall_limit(k)

    try:
        from vetinari.learning.episode_memory import get_episode_memory

        episodes = get_episode_memory().recall(
            query=goal[:300],
            k=k,
            successful_only=True,
            task_type=task_type,
        )
        contexts: list[dict[str, Any]] = []
        for ep in episodes:
            metadata = _memory_context_metadata(ep)
            if metadata is None:
                continue
            contexts.append({
                "task_summary": ep.task_summary,
                "output_summary": ep.output_summary,
                "quality_score": _episode_quality_score(getattr(ep, "quality_score", None)),
                "agent_type": ep.agent_type,
                "model_id": ep.model_id,
                **metadata,
            })
        return contexts
    except Exception as exc:
        _raise_recall_failure("planning context recall", exc)


def recall_failure_patterns(
    agent_type: str,
    task_type: str,
) -> list[str]:
    """Recall common failure patterns to inject as avoidance context.

    Args:
        agent_type: Agent type whose prior failures should be recalled.
        task_type: Task category to match against prior failures.

    Returns:
        Failure pattern summaries from the memory backend.

    Raises:
        ValueError: If either filter is blank.
        EpisodicRecallError: If the memory backend cannot provide trusted
            failure-pattern context.
    """
    agent_type = _validate_non_empty_text(agent_type, field_name="agent_type")
    task_type = _validate_non_empty_text(task_type, field_name="task_type")

    try:
        from vetinari.learning.episode_memory import get_episode_memory

        return get_episode_memory().get_failure_patterns(agent_type, task_type)
    except Exception as exc:
        _raise_recall_failure("failure pattern recall", exc)


def recall_few_shot_examples(
    task_type: str,
    k: int = 3,
) -> list[dict[str, str]]:
    """Recall top-scoring examples for few-shot prompt construction.

    Args:
        task_type: Task category used to select examples.
        k: Maximum number of examples to return.

    Returns:
        Few-shot example dictionaries with input and output text.

    Raises:
        ValueError: If ``task_type`` is blank or the recall limit is invalid.
        EpisodicRecallError: If training examples cannot be loaded safely.
    """
    task_type = _validate_non_empty_text(task_type, field_name="task_type")
    k = _validate_recall_limit(k)

    try:
        from vetinari.learning.training_data import get_training_collector

        return get_training_collector().export_few_shot_examples(task_type, k=k)
    except Exception as exc:
        _raise_recall_failure("few-shot recall", exc)


def recall_similar_episodes(
    task_description: str,
    confidence_level: ConfidenceLevel | str = ConfidenceLevel.MEDIUM,
    k: int = 3,
) -> list[dict[str, Any]]:
    """Retrieve similar past episodes with adaptive retrieval and importance scoring.

    Args:
        task_description: Current task description used as the recall query.
        confidence_level: Current confidence level. Medium and high confidence
            skip memory retrieval for efficiency.
        k: Maximum number of scored episodes to return.

    Returns:
        Scored episode context dictionaries, or an empty list when confidence
        is above the retrieval threshold.

    Raises:
        ValueError: If inputs are blank, confidence is unknown, or episode
            timestamps are malformed.
        EpisodicRecallError: If the memory backend or episode quality signals
            cannot produce trusted context.
    """
    task_description = _validate_non_empty_text(task_description, field_name="task_description")
    confidence_level = _validate_confidence_level(confidence_level)
    k = _validate_recall_limit(k)

    if confidence_level not in _RETRIEVAL_CONFIDENCE_LEVELS:
        logger.info(
            "Episodic retrieval skipped; confidence=%s is above retrieval threshold",
            confidence_level.value,
        )
        return []

    try:
        from vetinari.learning.episode_memory import get_episode_memory

        episodes = get_episode_memory().recall(
            query=task_description[:300],
            k=k * 2,
        )

        now = time.time()
        scored = []
        for ep in episodes:
            metadata = _memory_context_metadata(ep)
            if metadata is None:
                continue
            timestamp = _episode_timestamp_seconds(getattr(ep, "timestamp", None), default=now)
            age_hours = max(1.0, (now - timestamp) / 3600)
            recency = 1.0 / (1.0 + age_hours / 24.0)
            quality_score = _episode_quality_score(getattr(ep, "quality_score", None))
            quality_impact = abs(quality_score - 0.5) * 2
            importance = recency * quality_impact

            scored.append({
                "task_summary": getattr(ep, "task_summary", ""),
                "approach": getattr(ep, "output_summary", ""),
                "quality_score": quality_score,
                "errors": getattr(ep, "error_message", ""),
                "model_id": getattr(ep, "model_id", ""),
                "agent_type": getattr(ep, "agent_type", ""),
                "importance_score": round(importance, 4),
                **metadata,
            })

        scored.sort(key=lambda x: x["importance_score"], reverse=True)
        return scored[:k]
    except Exception as exc:
        _raise_recall_failure("similar episode recall", exc)
