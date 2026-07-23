"""Difficulty calibration for Vetinari model routing.

Compares predicted task difficulty with observed outcome signals and feeds
calibration data back into model routing.

Writer: base_agent_completion.py calls record_difficulty_feedback() after each task.
Reader: model_router_scoring.py:assess_difficulty() uses get_calibration_bias() to
adjust its heuristic output based on historical prediction accuracy.

This module is step 6b in the completion pipeline: after episodic memory is
recorded, difficulty predictions are calibrated against observed signals so
that future model routing uses empirically corrected difficulty estimates.
"""

from __future__ import annotations

import json
import logging
import threading
from typing import TypedDict

from vetinari.boundary_guards import account_evidence_drop
from vetinari.database import get_connection
from vetinari.learning.episode_memory import get_episode_memory

logger = logging.getLogger(__name__)


# Module-level lock protects DB writes. Reads are safe without a lock
# because the database runs in WAL mode (readers don't block writers).
_write_lock = threading.Lock()

# Calibration bias is clamped to this range to prevent extreme drift from
# a small number of unusual episodes dominating the adjustment.
_MAX_CALIBRATION_BIAS = 0.3


class DifficultySignals(TypedDict, total=False):
    """Observed execution signals used to compute actual task difficulty.

    All fields are optional — absent fields are excluded from the computation
    so that partial signal sets still produce valid calibration data.
    """

    retries: int  # How many times the task was retried before success/failure
    rejections: int  # How many inspection rejections the output received
    duration_ms: float  # Wall-clock execution time in milliseconds
    quality_score: float  # Inspector quality score in the range [0.0, 1.0]
    success: bool  # Whether the task ultimately succeeded


def compute_observed_difficulty(signals: DifficultySignals) -> float:
    """Map observed execution signals to a 0.0-1.0 difficulty score.

    Combines retry count, rejection count, duration, quality, and success
    outcome into a single scalar. Each signal is additive with a cap so no
    single factor dominates.

    Args:
        signals: Observed execution outcome signals for a completed task.

    Returns:
        Observed difficulty score in the range [0.0, 1.0], where 1.0 is
        the hardest possible difficulty.
    """
    # Start from 0.3 baseline — all tasks have inherent difficulty of 0.3 in the
    # absence of negative signals. Signals are additive on top of this baseline.
    score = 0.3

    retries = signals.get("retries", 0) or 0
    if retries > 0:
        score += min(retries * 0.15, 0.3)

    rejections = signals.get("rejections", 0) or 0
    if rejections > 0:
        score += min(rejections * 0.1, 0.2)

    duration_ms = signals.get("duration_ms", 0.0) or 0.0
    if duration_ms > 120_000:  # Over 2 minutes is very slow
        score += 0.2
    elif duration_ms > 30_000:  # Over 30 seconds is slow
        score += 0.1

    quality_score = signals.get("quality_score")
    if quality_score is not None and quality_score < 0.5:
        score += 0.1

    success = signals.get("success")
    if success is False:
        score += 0.15

    return min(max(score, 0.0), 1.0)


def record_difficulty_feedback(
    task_type: str,
    predicted: float,
    signals: DifficultySignals,
    episode_id: str | None = None,
) -> None:
    """Record predicted vs observed difficulty to calibrate future routing.

    Computes the observed difficulty from execution signals, calculates the
    prediction delta, and writes all three values into the episode's metadata
    in the database. This data is consumed by get_calibration_bias() to
    adjust future difficulty estimates for the same task type.

    Args:
        task_type: The task type string (e.g. ``"coding"``, ``"security"``).
        predicted: The predicted difficulty score produced by assess_difficulty().
        signals: Observed execution signals from the completed task.
        episode_id: Optional episode ID from get_episode_memory().record().
            When provided, the episode's metadata row is updated in place.

    Raises:
        Exception: Propagates persistence failures after accounting the
            rejected calibration evidence.
    """
    observed = compute_observed_difficulty(signals)
    delta = observed - predicted

    logger.debug(
        "Difficulty feedback for %s: predicted=%.2f observed=%.2f delta=%+.2f",
        task_type,
        predicted,
        observed,
        delta,
    )

    if episode_id is None:
        return

    with _write_lock:
        try:
            conn = get_connection()
            row = conn.execute(
                "SELECT metadata FROM episode_memory_store WHERE episode_id = ?",
                (episode_id,),
            ).fetchone()
            if row is None:
                meta = _load_canonical_episode_metadata(episode_id)
                if meta is None:
                    logger.debug(
                        "Episode %s not found; difficulty feedback not persisted",
                        episode_id,
                    )
                    return
            else:
                meta = json.loads(row[0] or "{}")
            meta["predicted_difficulty"] = round(predicted, 3)
            meta["observed_difficulty"] = round(observed, 3)
            meta["difficulty_delta"] = round(delta, 3)
            meta.update({k: v for k, v in signals.items() if v is not None})
            if row is not None:
                conn.execute(
                    "UPDATE episode_memory_store SET metadata = ? WHERE episode_id = ?",
                    (json.dumps(meta), episode_id),
                )
                conn.commit()
            _update_canonical_episode_metadata(episode_id, meta)
        except Exception:
            account_evidence_drop(episode_id, "difficulty_feedback", logger=logger)
            logger.error(
                "Failed to persist difficulty feedback for episode %s; calibration data was not accepted",
                episode_id,
                exc_info=True,
            )
            raise


def _load_canonical_episode_metadata(episode_id: str) -> dict[str, object] | None:
    episodic = get_episode_memory()._episodic
    if episodic.get(episode_id) is None:
        return None
    return episodic._store.get_episode_metadata(episode_id)


def _update_canonical_episode_metadata(episode_id: str, metadata: dict[str, object]) -> None:
    episodic = get_episode_memory()._episodic
    episodic._store.update_episode_metadata(episode_id, metadata)


def get_calibration_bias(task_type: str, window: int = 50) -> float:
    """Return the mean difficulty prediction error for a task type.

    Queries recent episodes that have difficulty calibration metadata and
    computes the mean delta between observed and predicted difficulty. A
    positive bias means past predictions underestimated difficulty, so the
    caller should nudge scores upward.

    Args:
        task_type: Task type string to compute calibration for.
        window: Maximum number of recent episodes to consider.

    Returns:
        Mean difficulty delta clamped to [-0.3, 0.3]. Returns 0.0 when no
        calibration data exists or on query error.
    """
    try:
        conn = get_connection()
        if _table_exists(conn, "episode_memory_store"):
            rows = conn.execute(
                "SELECT metadata FROM episode_memory_store WHERE task_type = ? ORDER BY created_at DESC LIMIT ?",
                (task_type, window),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT metadata_json FROM memory_episodes WHERE task_type = ? ORDER BY created_at DESC LIMIT ?",
                (task_type, window),
            ).fetchall()
    except Exception:
        logger.warning(
            "Calibration bias query failed for task_type %s — using uncalibrated difficulty",
            task_type,
            exc_info=True,
        )
        return 0.0

    deltas: list[float] = []
    for (metadata_str,) in rows:
        try:
            meta = json.loads(metadata_str or "{}")
            delta = meta.get("difficulty_delta")
            if isinstance(delta, (int, float)):
                deltas.append(float(delta))
        except (json.JSONDecodeError, TypeError):
            logger.warning("Skipping malformed difficulty metadata record: %s", metadata_str[:100])
            continue

    if not deltas:
        return 0.0

    mean_delta = sum(deltas) / len(deltas)
    return min(max(mean_delta, -_MAX_CALIBRATION_BIAS), _MAX_CALIBRATION_BIAS)


def _table_exists(conn, name: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
        (name,),
    ).fetchone()
    return row is not None
