"""Idle-time housekeeping routines for the training scheduler."""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from vetinari.boundary_guards import account_evidence_drop
from vetinari.constants import _PROJECT_ROOT
from vetinari.utils.module_probe import module_is_available as _module_is_available

logger = logging.getLogger(__name__)
# Subdirectory names under outputs/ that the idle sweep MUST never delete.
# These contain release artifacts, FSA closure evidence, and audit receipts that
# are not scratch data and must survive across maintenance cycles.
# Who writes: idle_scheduler adds subdirs here when new protected output categories
# are registered. Who reads: sweep_outputs_scratch checks each candidate path.
_SWEEP_PROTECTED_SUBDIRS: frozenset[str] = frozenset({
    "releases",  # Built release artifacts — deletion would break distribution
    "full-suite-audit",  # Full-spectrum audit run evidence trees
    "closure-receipts",  # FSA closure receipt JSON files
    "audit",  # General audit evidence subtrees
    "receipts",  # Per-task FSA receipt store
})


def _is_protected_path(path: Path, target_root: Path) -> bool:
    """Return True if path is inside a protected subdirectory of target_root.

    Args:
        path: The candidate file or directory path.
        target_root: The root outputs directory being swept.

    Returns:
        True if the path is under any protected top-level subdirectory.
    """
    if not path.is_relative_to(target_root):
        return False
    relative = path.relative_to(target_root)
    # The first part of the relative path is the immediate subdirectory of target_root.
    parts = relative.parts
    if not parts:
        return False
    return parts[0] in _SWEEP_PROTECTED_SUBDIRS


def consolidate_memory(logger: logging.Logger) -> None:
    """Run memory, archive, and plan-retention maintenance during idle time."""
    try:
        if not _module_is_available("vetinari.memory.unified"):
            raise ModuleNotFoundError("vetinari.memory.unified")
        from vetinari.memory.unified import get_unified_store

        store = get_unified_store()
        promoted = store.consolidate()
        if promoted > 0:
            logger.info(
                "TrainingScheduler: memory consolidation promoted %d entries to long-term storage",
                promoted,
            )

        _prune_weak_memories(store, logger)
        try:
            pattern_count = store.promote_episodes_to_semantic()
            if pattern_count > 0:
                logger.info(
                    "TrainingScheduler: promoted %d episode groups to semantic patterns",
                    pattern_count,
                )
        except Exception:
            logger.warning(
                "Episode-to-semantic promotion failed - patterns will not be extracted this cycle",
                exc_info=True,
            )
        _flag_contradictions(store, logger)
    except ModuleNotFoundError:
        logger.debug("Memory consolidation skipped - unified memory store not available")
    except Exception:
        logger.warning("Memory consolidation failed during idle cycle - will retry next cycle", exc_info=True)

    _prune_improvement_archive(logger)
    _prune_plan_records(logger)


def sweep_outputs_scratch(outputs_root: Path | None, ttl_days: int) -> int:
    """Remove stale files and empty directories from outputs scratch storage.

    Args:
        outputs_root: Root output directory to sweep, or the default outputs root.
        ttl_days: File modification age threshold in days.

    Returns:
        Number of stale files removed.
    """
    target_root = outputs_root or (_PROJECT_ROOT / "outputs")
    if not target_root.is_dir():
        return 0

    cutoff = time.time() - (ttl_days * 86_400)
    removed = 0
    stale_dirs: set[Path] = set()
    for path in sorted(target_root.rglob("*"), reverse=True):
        if path.is_symlink():
            continue
        # Skip protected subdirectories — releases, audit evidence, FSA receipts.
        # Deleting these would destroy release artifacts and closure proofs (FSA-0087).
        if _is_protected_path(path, target_root):
            continue
        try:
            stale = path.stat().st_mtime < cutoff
            if path.is_file() and stale:
                path.unlink()
                stale_dirs.add(path.parent)
                removed += 1
            elif path.is_dir() and path != target_root and (stale or path in stale_dirs) and not any(path.iterdir()):
                path.rmdir()
        except OSError:
            logging.getLogger(__name__).warning("Could not sweep stale outputs scratch path %s", path, exc_info=True)
    return removed


def _prune_weak_memories(store: Any, logger: logging.Logger) -> None:
    """Remove memories whose Ebbinghaus retention strength has decayed below threshold."""
    try:
        if not _module_is_available("vetinari.memory.memory_storage"):
            raise ModuleNotFoundError("vetinari.memory.memory_storage")
        from vetinari.memory.memory_storage import PRUNE_THRESHOLD, ebbinghaus_strength

        with store._lock:
            rows = store._conn.execute(
                "SELECT id, importance, timestamp, recall_count FROM memories WHERE forgotten = 0"
            ).fetchall()

        weak_ids: list[str] = []
        skip_count = 0
        for row in rows:
            timestamp = row["timestamp"]
            try:
                created_ts_ms = int(timestamp)
            except (TypeError, ValueError) as exc:
                account_evidence_drop(
                    {"memory_id": row["id"], "timestamp": timestamp, "error_type": type(exc).__name__},
                    "idle_maintenance",
                    logger=logger,
                )
                logger.warning(
                    "idle_maintenance._prune_weak_memories: skipped unparseable timestamp %r",
                    timestamp,
                )
                skip_count += 1
                continue
            strength = ebbinghaus_strength(
                importance=float(row["importance"] or 0.5),
                created_ts_ms=created_ts_ms,
                recall_count=int(row["recall_count"] or 0),
            )
            if strength < PRUNE_THRESHOLD:
                weak_ids.append(row["id"])

        for entry_id in weak_ids:
            store.forget(entry_id, reason="Ebbinghaus strength below prune threshold")

        if weak_ids:
            logger.info("TrainingScheduler: pruned %d weak memories (Ebbinghaus decay)", len(weak_ids))
        if skip_count:
            logger.warning(
                "idle_maintenance._prune_weak_memories: skipped %d memories with unparseable timestamps",
                skip_count,
            )
    except ModuleNotFoundError:
        logger.debug("Ebbinghaus pruning skipped - memory_storage not available")
    except Exception:
        logger.warning(
            "Ebbinghaus decay pruning failed - weak memories will persist until next cycle",
            exc_info=True,
        )


def _flag_contradictions(store: Any, logger: logging.Logger) -> None:
    """Log memories linked by CONTRADICTS relationships for human review."""
    try:
        with store._lock:
            rows = store._conn.execute(
                "SELECT id, supersedes_id, content FROM memories "
                "WHERE relationship_type = 'contradicts' AND forgotten = 0"
            ).fetchall()

        for row in rows:
            logger.info(
                "TrainingScheduler: contradiction detected - memory %s contradicts %s: %.100s",
                row["id"],
                row["supersedes_id"],
                row["content"],
            )
    except Exception:
        logger.warning("Contradiction flagging skipped - column may not exist yet", exc_info=True)


def _prune_improvement_archive(logger: logging.Logger) -> None:
    try:
        if not _module_is_available("vetinari.learning.improvement_archive"):
            raise ModuleNotFoundError("vetinari.learning.improvement_archive")
        from vetinari.learning.improvement_archive import ImprovementArchive
        from vetinari.types import AgentType

        archive = ImprovementArchive()
        for agent_type in AgentType:
            pruned = archive.prune_stepping_stones(agent_type.value)
            if pruned > 0:
                logger.info(
                    "TrainingScheduler: pruned %d stepping-stone configs for agent=%s",
                    pruned,
                    agent_type.value,
                )
    except ModuleNotFoundError:
        logger.debug("ImprovementArchive not available - stepping-stone pruning skipped")
    except Exception:
        logger.warning(
            "Could not prune improvement archive stepping-stones - archive may grow unbounded",
            exc_info=True,
        )


def _prune_plan_records(logger: logging.Logger) -> None:
    try:
        if not _module_is_available("vetinari.memory.plan_tracking"):
            raise ModuleNotFoundError("vetinari.memory.plan_tracking")
        from vetinari.memory.plan_tracking import MemoryStore

        deleted = MemoryStore().prune_old_plans()
        if deleted > 0:
            logger.info("TrainingScheduler: pruned %d old plan records from memory store", deleted)
    except ModuleNotFoundError:
        logger.debug("MemoryStore not available - plan pruning skipped")
    except Exception:
        logger.warning(
            "Could not prune old plan records from memory store - storage may grow unbounded",
            exc_info=True,
        )


__all__ = ["consolidate_memory", "sweep_outputs_scratch"]
