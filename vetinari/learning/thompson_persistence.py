"""Thompson Sampling persistence helpers — load, save, migrate, and priors.

Extracted from ``model_selector.py`` to keep that file under the 550-line
ceiling.  Provides pure functions that operate on the arms dict so the caller
(ThompsonSamplingSelector) retains ownership of the lock.

Functions:
    ``load_state_from_db`` — load arm states from SQLite
    ``migrate_from_json``  — one-time migration from legacy JSON file
    ``save_state``         — persist arm states to SQLite (JSON fallback)
    ``get_informed_prior`` — get informed Beta prior from BenchmarkSeeder
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING

from vetinari.constants import VETINARI_STATE_DIR

logger = logging.getLogger(__name__)


if TYPE_CHECKING:
    from vetinari.learning.thompson_arms import ThompsonBetaArm


def _get_state_dir() -> str:
    """Return the .vetinari state directory path.

    Returns:
        Absolute path string, from VETINARI_STATE_DIR env var or project root.
    """
    return os.environ.get("VETINARI_STATE_DIR", "") or str(VETINARI_STATE_DIR)


def load_state_from_db(arms: dict[str, ThompsonBetaArm]) -> int:
    """Load arm states from the ``thompson_arms`` SQLite table.

    Populates ``arms`` in-place.  Caller is responsible for holding a lock
    before calling this function.

    Args:
        arms: The arms dict to populate.

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
            kwargs: dict = {
                "model_id": row[1],
                "task_type": row[2],
                "alpha": float(row[3]),
                "beta": float(row[4]),
                "total_pulls": int(row[5]),
            }
            if row[6]:
                kwargs["last_updated"] = row[6]
            arms[row[0]] = ThompsonBetaArm(**kwargs)
        if rows:
            logger.debug("[Thompson] Loaded %d arm states from SQLite", len(rows))
        return len(rows)
    except Exception as exc:
        logger.warning("[Thompson] Could not load state from SQLite — %s", exc)
        return 0


def migrate_from_json(arms: dict[str, ThompsonBetaArm]) -> None:
    """One-time migration: import arms from legacy JSON file and save to SQLite.

    The JSON file is left in place after migration.  Subsequent runs read
    exclusively from SQLite.  Migration is opt-in so a bundled project-local
    ``.vetinari/thompson_state.json`` cannot silently seed default model
    selection in a fresh checkout.

    Args:
        arms: The arms dict to populate from the legacy JSON file.
    """
    from vetinari.learning.thompson_arms import ThompsonBetaArm

    try:
        allow_migration = os.environ.get("VETINARI_MIGRATE_LEGACY_THOMPSON_STATE", "").strip().lower()
        if allow_migration not in {"1", "true", "yes"}:
            logger.info(
                "[Thompson] Legacy JSON migration skipped; set VETINARI_MIGRATE_LEGACY_THOMPSON_STATE=1 "
                "to import thompson_state.json into SQLite"
            )
            return
        state_file = Path(_get_state_dir()) / "thompson_state.json"
        if not state_file.exists():
            return
        with state_file.open(encoding="utf-8") as f:
            data = json.load(f)
        for key, d in data.items():
            arms[key] = ThompsonBetaArm(**d)
        logger.info("[Thompson] Migrated %d arms from JSON to SQLite", len(data))
        save_state(arms)
    except Exception as exc:
        logger.warning("[Thompson] JSON migration failed — %s", exc)


def save_state(arms: dict[str, ThompsonBetaArm]) -> None:
    """Persist arm states to the ``thompson_arms`` SQLite table.

    Uses a DELETE + INSERT batch to keep the table in sync with the in-memory
    arms dict.  Falls back to a JSON file on the local filesystem if SQLite is
    unavailable so that no observations are lost on process exit.

    During interpreter shutdown (atexit), logging streams may already be
    closed.  The function suppresses log writes during shutdown to prevent
    ``ValueError: I/O operation on closed file`` tracebacks.

    Args:
        arms: The arms dict to persist.
    """
    import logging as _logging

    # Detect shutdown: if root handlers are closed, suppress logging
    _shutting_down = not _logging.root.handlers or any(
        getattr(h, "stream", None) is not None and getattr(h.stream, "closed", False) for h in _logging.root.handlers
    )
    if _shutting_down:
        _logging.disable(_logging.CRITICAL)

    arm_items = list(arms.items())

    try:
        from vetinari.database import get_connection

        conn = get_connection()
        conn.execute("DELETE FROM thompson_arms")
        conn.executemany(
            """INSERT INTO thompson_arms (arm_key, model_id, task_type, alpha, beta, total_pulls, last_updated)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            [
                (key, arm.model_id, arm.task_type, arm.alpha, arm.beta, arm.total_pulls, arm.last_updated)
                for key, arm in arm_items
            ],
        )
        conn.commit()
    except Exception as exc:
        if not _shutting_down:
            logger.warning("[Thompson] Could not save state to SQLite — %s", exc)
        # Legacy JSON fallback so we don't lose state if DB is unavailable
        try:
            state_dir = _get_state_dir()
            Path(state_dir).mkdir(parents=True, exist_ok=True)
            state_file = Path(state_dir) / "thompson_state.json"
            with Path(state_file).open("w", encoding="utf-8") as f:
                json.dump({k: asdict(v) for k, v in arm_items}, f, indent=2)
        except Exception as json_exc:
            if not _shutting_down:
                logger.warning("[Thompson] JSON fallback also failed — %s", json_exc)
    finally:
        if _shutting_down:
            _logging.disable(_logging.NOTSET)


def prune_stale_arms(
    arms: dict[str, ThompsonBetaArm],
    known_model_ids: set[str] | None = None,
    days: int = 30,
) -> int:
    """Remove arms for models not seen in recent discovery or stale for too long.

    Prunes arms whose model is not in ``known_model_ids`` (if provided) AND
    whose ``last_updated`` timestamp is older than ``days`` days.  Arms for
    active models are always preserved regardless of age.

    Args:
        arms: The arms dict to prune in-place.  Caller must hold the lock.
        known_model_ids: Set of model IDs from the most recent discovery.
            When None, pruning is based on age alone.
        days: Number of days after which an arm is considered stale.

    Returns:
        Number of arms pruned.
    """
    from datetime import datetime, timedelta, timezone

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    to_prune = []

    for arm_key, arm in arms.items():
        # Always preserve arms for currently-known models
        model_id = arm.model_id
        if known_model_ids and model_id in known_model_ids:
            continue

        # Prune if last_updated is older than cutoff
        try:
            last_updated = datetime.fromisoformat(arm.last_updated)
            # Make timezone-aware if naive
            if last_updated.tzinfo is None:
                last_updated = last_updated.replace(tzinfo=timezone.utc)
        except (ValueError, AttributeError):
            # Unparseable timestamp — treat as very old
            last_updated = datetime.min.replace(tzinfo=timezone.utc)

        if last_updated < cutoff:
            to_prune.append(arm_key)

    for arm_key in to_prune:
        del arms[arm_key]

    if to_prune:
        logger.info(
            "Thompson arm pruning: removed %d stale arms (threshold: %d days)",
            len(to_prune),
            days,
        )

    return len(to_prune)


def get_informed_prior(model_id: str, task_type: str) -> tuple[float, float]:
    """Get an informed Beta prior from BenchmarkSeeder.

    Falls back to an uninformed Beta(1, 1) prior when BenchmarkSeeder is
    unavailable (minimal installs, test environments).

    Args:
        model_id: The model identifier.
        task_type: The task type string.

    Returns:
        Tuple (alpha, beta) for the initial Beta distribution.
    """
    try:
        from vetinari.learning.benchmark_seeder import get_benchmark_seeder

        return get_benchmark_seeder().get_prior(model_id, task_type)
    except Exception:
        logger.warning(
            "BenchmarkSeeder unavailable for %s:%s — using uninformed prior",
            model_id,
            task_type,
        )
        return (1.0, 1.0)
