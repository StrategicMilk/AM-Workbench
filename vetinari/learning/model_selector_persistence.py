"""Thompson Sampling persistence helpers — state load/save for ThompsonSamplingSelector.

Extracted from model_selector.py to keep that file within the 550-line limit.
Functions accept a ``selector: ThompsonSamplingSelector`` as first argument and
operate on its ``_arms``, ``_lock``, and helper methods.

Uses TYPE_CHECKING to import ThompsonSamplingSelector for annotations only —
at runtime the parameter is typed as ``Any`` to avoid circular imports.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from vetinari.constants import VETINARI_STATE_DIR

logger = logging.getLogger(__name__)


if TYPE_CHECKING:
    from vetinari.learning.model_selector import ThompsonSamplingSelector


__all__ = [
    "get_state_dir",
    "load_state",
    "load_state_from_db",
    "migrate_from_json",
    "save_state",
]


def _safe_warning(message: str, *args: object) -> None:
    """Emit a warning without traceback noise when logging streams are closed."""
    previous_raise_exceptions = logging.raiseExceptions
    logging.raiseExceptions = False
    try:
        logger.warning(message, *args)
    finally:
        logging.raiseExceptions = previous_raise_exceptions


def get_state_dir(selector: Any) -> str:
    """Return the canonical per-user Vetinari state directory path.

    Reads ``VETINARI_STATE_DIR`` from the environment on every call so that
    test monkeypatches and runtime overrides take effect without module reload.
    Falls back to the project-relative ``.vetinari`` directory when the env
    var is not set.

    Args:
        selector: ThompsonSamplingSelector instance (unused — kept for API
            consistency with other persistence helpers).

    Returns:
        Absolute path string to the state directory.
    """
    # Re-read env on every call so monkeypatches and runtime overrides work
    return os.environ.get("VETINARI_STATE_DIR", "") or str(VETINARI_STATE_DIR)


def load_state(selector: ThompsonSamplingSelector) -> None:
    """Load persisted arm states — SQLite primary, JSON file fallback.

    Calls load_state_from_db first; if that returns 0 arms, falls back to
    migrate_from_json to import legacy JSON state and re-persist to SQLite.

    Args:
        selector: ThompsonSamplingSelector whose ``_arms`` dict is populated.
    """
    loaded = load_state_from_db(selector)
    if loaded == 0:
        migrate_from_json(selector)


def load_state_from_db(selector: ThompsonSamplingSelector) -> int:
    """Load arm states from the ``thompson_arms`` SQLite table.

    Args:
        selector: ThompsonSamplingSelector whose ``_arms`` dict is populated.

    Returns:
        Number of arms loaded, or 0 when the table is empty or unavailable.
    """
    from vetinari.learning.thompson_arms import ThompsonBetaArm

    try:
        from vetinari.database import get_connection

        conn = get_connection()
        rows = conn.execute(
            "SELECT arm_key, model_id, task_type, alpha, beta, total_pulls, last_updated FROM thompson_arms"
        ).fetchall()
        for row in rows:
            selector._arms[row[0]] = ThompsonBetaArm(
                model_id=row[1],
                task_type=row[2],
                alpha=float(row[3]),
                beta=float(row[4]),
                total_pulls=int(row[5]),
                last_updated=row[6] or datetime.now(timezone.utc).isoformat(),
            )
        if rows:
            logger.debug("[Thompson] Loaded %d arm states from SQLite", len(rows))
        return len(rows)
    except Exception as exc:
        logger.warning("[Thompson] Could not load arm state from SQLite; arms start at prior: %s", exc)
        return 0


def migrate_from_json(selector: ThompsonSamplingSelector) -> None:
    """One-time migration: import arms from legacy JSON file and save to SQLite.

    The JSON file is left in place after migration so it can be used as a
    backup.  Subsequent runs read exclusively from SQLite.  Migration is
    opt-in so a bundled project-local ``.vetinari/thompson_state.json`` cannot
    silently seed default model selection in a fresh checkout.

    Args:
        selector: ThompsonSamplingSelector whose ``_arms`` dict is populated
            from the JSON file, then re-persisted to SQLite via save_state.
    """
    import json

    from vetinari.learning.thompson_arms import ThompsonBetaArm

    try:
        allow_migration = os.environ.get("VETINARI_MIGRATE_LEGACY_THOMPSON_STATE", "").strip().lower()
        if allow_migration not in {"1", "true", "yes"}:
            logger.info(
                "[Thompson] Legacy JSON migration skipped; set VETINARI_MIGRATE_LEGACY_THOMPSON_STATE=1 "
                "to import thompson_state.json into SQLite"
            )
            return
        state_file = Path(get_state_dir(selector)) / "thompson_state.json"
        if not state_file.exists():
            return
        with state_file.open(encoding="utf-8") as f:
            data = json.load(f)
        for key, d in data.items():
            selector._arms[key] = ThompsonBetaArm(**d)
        logger.info("[Thompson] Migrated %d arms from JSON to SQLite", len(data))
        save_state(selector)
    except Exception as exc:
        _safe_warning("[Thompson] JSON migration failed — starting from uninformed priors: %s", exc)


def save_state(selector: ThompsonSamplingSelector) -> None:
    """Persist arm states to the ``thompson_arms`` SQLite table.

    Uses a DELETE + INSERT batch to keep the table in sync with in-memory arms.
    Falls back to a JSON file when SQLite is unavailable so state survives a
    missing database.

    During interpreter shutdown (atexit), logging streams may already be closed;
    logging is temporarily silenced to prevent traceback noise on process exit.

    Args:
        selector: ThompsonSamplingSelector whose ``_arms`` are persisted.
    """
    import logging as _logging

    # Detect interpreter shutdown — stream handlers have closed files
    _shutting_down = not _logging.root.handlers or any(
        getattr(h, "stream", None) is not None and getattr(h.stream, "closed", False) for h in _logging.root.handlers
    )
    if _shutting_down:
        _logging.disable(_logging.CRITICAL)

    try:
        from vetinari.database import get_connection

        conn = get_connection()
        conn.execute("DELETE FROM thompson_arms")
        conn.executemany(
            """INSERT INTO thompson_arms
               (arm_key, model_id, task_type, alpha, beta, total_pulls, last_updated)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            [
                (key, arm.model_id, arm.task_type, arm.alpha, arm.beta, arm.total_pulls, arm.last_updated)
                for key, arm in selector._arms.items()
            ],
        )
        conn.commit()
    except Exception as exc:
        if not _shutting_down:
            _safe_warning("[Thompson] Could not save arm state to SQLite — using JSON fallback: %s", exc)
        # JSON fallback so state is not lost when the database is unavailable
        try:
            import json
            from dataclasses import asdict

            state_dir = get_state_dir(selector)
            Path(state_dir).mkdir(parents=True, exist_ok=True)
            state_file = Path(state_dir) / "thompson_state.json"
            tmp_path = state_file.with_name(f".{state_file.name}.{os.getpid()}.tmp")
            with tmp_path.open("w", encoding="utf-8") as f:
                json.dump({k: asdict(v) for k, v in selector._arms.items()}, f, indent=2)
            os.replace(tmp_path, state_file)
        except Exception as json_exc:
            if not _shutting_down:
                _safe_warning("[Thompson] JSON fallback save also failed: %s", json_exc)
    finally:
        if _shutting_down:
            _logging.disable(_logging.NOTSET)
