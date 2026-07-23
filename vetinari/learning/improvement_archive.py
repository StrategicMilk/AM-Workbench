"""Improvement Archive — versioned configuration archive with DGM pattern.

Stores agent configurations with empirical performance data. New variants
branch from proven configs rather than being generated from scratch.
Uses Divergent Generation and Merging (DGM) pattern to keep top-N
performers as stepping stones for future improvements.

Decision: DGM-style improvement archive for evidence-based evolution.
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from vetinari.boundary_guards import require_nonempty
from vetinari.database import get_connection

logger = logging.getLogger(__name__)


# Maximum stepping stones per agent type
_MAX_STEPPING_STONES = 10
MINIMUM_SAMPLE_COUNT = 5


@dataclass
class ConfigVersion:
    """A versioned agent configuration with performance data."""

    config_id: str
    agent_type: str
    config_json: str  # Serialized configuration
    quality_score: float = 0.5
    cost_score: float | None = None  # None if cost evidence is not available
    sample_count: int = 0
    parent_id: str | None = None  # Config this branched from
    status: str = "candidate"  # candidate, active, archived
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def __repr__(self) -> str:
        return (
            f"ConfigVersion(config_id={self.config_id!r}, agent_type={self.agent_type!r}, "
            f"status={self.status!r}, quality_score={self.quality_score!r})"
        )


class ImprovementArchive:
    """Versioned archive of agent configurations for DGM-style evolution.

    New variants branch from the best-performing existing configs instead
    of starting from scratch. Top-N performers are retained as stepping
    stones for future evolution cycles.

    Side effects:
      - Creates config_archive table in unified SQLite DB
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._init_db()

    def _init_db(self) -> None:
        """Create the config_archive table if it doesn't exist."""
        conn = get_connection()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS config_archive (
                config_id TEXT PRIMARY KEY,
                agent_type TEXT NOT NULL,
                config_json TEXT NOT NULL,
                quality_score REAL DEFAULT 0.5,
                cost_score REAL DEFAULT 0.5,
                sample_count INTEGER DEFAULT 0,
                parent_id TEXT,
                status TEXT DEFAULT 'candidate',
                created_at TEXT
            )
        """)
        conn.commit()

    def store(
        self,
        agent_type: str,
        config: dict[str, Any],
        quality_score: float = 0.5,
        cost_score: float | None = None,
        parent_id: str | None = None,
    ) -> str:
        """Store a new configuration version.

        Args:
            agent_type: Agent type this config is for.
            config: The configuration dict.
            quality_score: Initial quality score estimate.
            cost_score: Initial cost efficiency score estimate. None if cost evidence is missing.
            parent_id: Config this branched from (None for originals).

        Returns:
            Generated config_id.
        """
        config_id = f"cfg_{uuid.uuid4().hex[:8]}"

        with self._lock:
            conn = get_connection()
            conn.execute(
                """INSERT INTO config_archive
                   (config_id, agent_type, config_json, quality_score, cost_score, parent_id, status, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, 'candidate', ?)""",
                (
                    config_id,
                    agent_type,
                    json.dumps(config),
                    quality_score,
                    cost_score,
                    parent_id,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            conn.commit()

        logger.info(
            "[ImprovementArchive] Stored config %s for %s (parent=%s)",
            config_id,
            agent_type,
            parent_id,
        )
        return config_id

    def get_best_configs(self, agent_type: str, limit: int = 5) -> list[ConfigVersion]:
        """Get top-performing configs for an agent type.

        Args:
            agent_type: Agent type to query.
            limit: Maximum configs to return.

        Returns:
            List of ConfigVersion sorted by quality_score descending.
        """
        conn = get_connection()
        rows = conn.execute(
            """SELECT config_id, agent_type, config_json, quality_score, cost_score,
                    sample_count, parent_id, status, created_at
               FROM config_archive
               WHERE agent_type = ? AND status != 'archived'
               ORDER BY quality_score DESC LIMIT ?""",
            (agent_type, limit),
        ).fetchall()

        return [
            ConfigVersion(
                config_id=r[0],
                agent_type=r[1],
                config_json=r[2],
                quality_score=r[3],
                cost_score=r[4],
                sample_count=r[5],
                parent_id=r[6],
                status=r[7],
                created_at=r[8],
            )
            for r in rows
        ]

    def update_score(self, config_id: str, quality_score: float) -> None:
        """Update the quality score for a config using exponential moving average.

        Args:
            config_id: Config to update.
            quality_score: New quality observation.
        """
        ema_alpha = 0.2  # EMA smoothing factor — higher = more responsive to recent observations
        with self._lock:
            conn = get_connection()
            conn.execute(
                """UPDATE config_archive
                   SET quality_score = quality_score * (1 - ?) + ? * ?,
                       sample_count = sample_count + 1
                   WHERE config_id = ?""",
                (ema_alpha, ema_alpha, quality_score, config_id),
            )
            conn.commit()

    def promote(self, config_id: str) -> None:
        """Promote a candidate config to active status.

        Args:
            config_id: Config to promote.

        Raises:
            ValueError: If the config is missing or lacks enough samples for
                evidence-backed promotion.
        """
        config_id = require_nonempty(config_id, field_name="config_id")
        with self._lock:
            conn = get_connection()
            row = conn.execute(
                "SELECT sample_count FROM config_archive WHERE config_id = ?",
                (config_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"config {config_id!r} does not exist")
            sample_count = int(row[0])
            if sample_count < MINIMUM_SAMPLE_COUNT:
                raise ValueError(
                    f"config {config_id!r} has {sample_count} sample(s); "
                    f"minimum {MINIMUM_SAMPLE_COUNT} required for promotion"
                )
            conn.execute(
                "UPDATE config_archive SET status = 'active' WHERE config_id = ?",
                (config_id,),
            )
            conn.commit()
        logger.info("[ImprovementArchive] Promoted config %s", config_id)

    def prune_stepping_stones(self, agent_type: str) -> int:
        """Archive configs beyond the top-N stepping stones.

        Configs ranked lower than _MAX_STEPPING_STONES by quality score
        are marked archived so they no longer appear in normal queries.

        Args:
            agent_type: Agent type to prune.

        Returns:
            Number of configs archived.
        """
        configs = self.get_best_configs(agent_type, limit=1000)
        if len(configs) <= _MAX_STEPPING_STONES:
            return 0

        to_archive = configs[_MAX_STEPPING_STONES:]
        with self._lock:
            conn = get_connection()
            for cfg in to_archive:
                conn.execute(
                    "UPDATE config_archive SET status = 'archived' WHERE config_id = ?",
                    (cfg.config_id,),
                )
            conn.commit()

        pruned = len(to_archive)
        logger.info("[ImprovementArchive] Archived %d configs for %s", pruned, agent_type)
        return pruned


# Module-level singleton — written by get_improvement_archive(), read by callers
_archive: ImprovementArchive | None = None
_archive_lock = threading.Lock()


def get_improvement_archive() -> ImprovementArchive:
    """Return the singleton ImprovementArchive instance (thread-safe).

    Returns:
        The shared ImprovementArchive instance.
    """
    global _archive
    if _archive is None:
        with _archive_lock:
            if _archive is None:
                _archive = ImprovementArchive()
    return _archive
