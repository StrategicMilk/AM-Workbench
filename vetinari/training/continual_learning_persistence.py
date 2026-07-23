"""Shared persistence helpers for continual-learning safeguards.

This module owns the file-system details used by replay buffers and LoRA
adapter registries. Keeping the persistence helpers separate lets the public
continual-learning facade stay small while preserving the same on-disk formats.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable
from pathlib import Path

from vetinari.constants import get_user_dir

logger = logging.getLogger(__name__)


ADAPTER_REGISTRY_FILENAME = "registry.json"


def _require_immutable_model_revision(model_revision: str | None) -> str:
    """Return a usable immutable remote revision or raise.

    Args:
        model_revision: Hugging Face revision supplied for a remote model load.

    Returns:
        The stripped immutable model revision.

    Raises:
        ValueError: If the revision is missing or points to the floating
            ``main`` branch.
    """
    revision = (model_revision or "").strip()
    if not revision or revision == "main":
        msg = (
            "Remote Hugging Face model loads require an explicit immutable revision "
            "(tag or commit hash), not the floating 'main' branch."
        )
        raise ValueError(msg)
    return revision


def _default_replay_buffer_path() -> Path:
    """Resolve the replay-buffer path from the configured Vetinari user dir.

    Returns:
        Path to the default replay-buffer JSONL file.
    """
    return Path(get_user_dir()) / "replay_buffer.jsonl"


def _default_adapters_dir() -> Path:
    """Resolve the LoRA adapter registry root from the configured user dir.

    Returns:
        Path to the default adapter registry directory.
    """
    return Path(get_user_dir()) / "adapters"


def _atomic_write_text(path: Path, text: str) -> None:
    """Write text through a same-directory temp file and atomic replace.

    Args:
        path: Destination file to replace atomically.
        text: UTF-8 text content to persist.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.tmp.{os.getpid()}.{threading.get_ident()}")
    try:
        tmp_path.write_text(text, encoding="utf-8")
        tmp_path.replace(path)
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                logger.warning("Could not remove temporary file %s", tmp_path)


def _move_corrupt_file(path: Path, *, clock: Callable[[], float] | None = None) -> Path | None:
    """Move a corrupt persistence file aside for visible operator diagnosis.

    Args:
        path: Path to the damaged persistence file.
        clock: Optional time source used to suffix the quarantine path.

    Returns:
        Path to the moved corrupt file, or ``None`` when the source is absent
        or cannot be moved.
    """
    if not path.exists():
        return None
    active_clock = clock or time.time
    corrupt_path = path.with_name(f"{path.name}.corrupt.{int(active_clock())}")
    try:
        path.replace(corrupt_path)
        return corrupt_path
    except OSError as exc:
        logger.warning("Could not move corrupt file %s aside: %s", path, exc)
        return None


__all__ = [
    "ADAPTER_REGISTRY_FILENAME",
    "_atomic_write_text",
    "_default_adapters_dir",
    "_default_replay_buffer_path",
    "_move_corrupt_file",
    "_require_immutable_model_revision",
]
