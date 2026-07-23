"""Tool result disk persistence — offloads large tool outputs to disk.

When a tool result exceeds LARGE_RESULT_THRESHOLD chars, stores the full
content to disk and returns a 2KB preview in-message with a retrieval key.
This prevents large tool results from consuming context window budget.

Pipeline role: Called by context window management when tool results arrive.
"""

from __future__ import annotations

import hashlib
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path

from vetinari.constants import VETINARI_STATE_DIR
from vetinari.learning.atomic_writers import _write_text_atomic
from vetinari.utils.bounded_collections import bounded_rglob

logger = logging.getLogger(__name__)


# Results larger than this threshold are offloaded to disk
LARGE_RESULT_THRESHOLD = 50_000  # chars

# Number of chars kept inline as a preview when content is offloaded
PREVIEW_SIZE = 2_048  # chars

# Characters forbidden in tool_name to prevent path traversal in filenames
_TOOL_NAME_FORBIDDEN = frozenset(("/", "\\", ".."))
_KEY_FORBIDDEN = frozenset("*?[]/\\")
DEFAULT_MAX_CACHE_FILES = 1024
_CACHE_PRUNE_SCAN_LIMIT = DEFAULT_MAX_CACHE_FILES * 2


class ToolResultStore:
    """Stores large tool results to disk and returns compact previews.

    When a tool result exceeds LARGE_RESULT_THRESHOLD characters, the full
    content is written to disk under ``cache_dir`` and only a truncated
    preview is returned to the caller.  The caller can retrieve the full
    content later via :meth:`load`.

    Thread-safe: disk writes are serialised through an instance lock so
    concurrent ``store()`` calls on the same instance are safe.

    Example::

        store = ToolResultStore()
        preview, key = store.store(large_output, tool_name="grep")
        # preview is <=2KB; full content retrievable via store.load(key)
    """

    def __init__(self, cache_dir: Path | None = None, *, max_cache_files: int = DEFAULT_MAX_CACHE_FILES) -> None:
        """Configure store directory.

        Args:
            cache_dir: Directory for persisted results. Defaults to
                .vetinari/tool_results/ relative to cwd.
            max_cache_files: Maximum persisted result files to retain.
        """
        if max_cache_files < 1:
            raise ValueError("max_cache_files must be >= 1")
        if cache_dir is None:
            cache_dir = VETINARI_STATE_DIR / "tool_results"
        self._cache_dir = cache_dir
        self._max_cache_files = max_cache_files
        self._lock = threading.Lock()

    def store(self, content: str, tool_name: str = "") -> tuple[str, str]:
        """Store large content to disk, returning (preview, key).

        If content is within LARGE_RESULT_THRESHOLD characters, the full
        content is returned unchanged with an empty retrieval key — nothing
        is written to disk.

        Args:
            content: The full tool result content.
            tool_name: Name of the tool that produced this result. Used as
                part of the persisted filename for human readability.

        Returns:
            Tuple of (preview_or_full_content, retrieval_key).
            retrieval_key is empty string when content was not persisted.

        Raises:
            ValueError: If tool_name contains path-traversal characters.
        """
        if len(content) <= LARGE_RESULT_THRESHOLD:
            return content, ""

        # Reject tool_name values that contain path-traversal characters so
        # they cannot escape the cache directory when embedded in a filename.
        if tool_name and any(bad in tool_name for bad in _TOOL_NAME_FORBIDDEN):
            raise ValueError(f"tool_name {tool_name!r} contains forbidden characters (/, \\, ..)")

        key = hashlib.md5(content.encode(), usedforsecurity=False).hexdigest()[:16]
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        if tool_name:
            filename = f"{timestamp}_{tool_name}_{key}.txt"
        else:
            filename = f"{timestamp}_{key}.txt"

        with self._lock:
            self._cache_dir.mkdir(parents=True, exist_ok=True)
            dest = self._cache_dir / filename
            _write_text_atomic(dest, content)
            self._prune_cache_locked()

        preview = content[:PREVIEW_SIZE]
        if len(content) > PREVIEW_SIZE:
            remaining = len(content) - PREVIEW_SIZE
            suffix = f"\n\n[... {remaining} chars truncated — retrieve via key={key}]"
            preview += suffix

        logger.info(
            "Tool result persisted to disk: %s chars -> %s (key=%s)",
            len(content),
            dest.name,
            key,
        )
        return preview, key

    def load(self, key: str) -> str | None:
        """Load full content for a previously stored result.

        Scans the cache directory for a file whose name contains the key.

        Args:
            key: Retrieval key returned by :meth:`store`.

        Returns:
            Full content string, or None if no file matching the key is found.

        Raises:
            ValueError: If ``key`` is not an exact persisted cache key.
            OSError: If the matching cache file cannot be read.
        """
        if not key:
            return None
        if any(bad in key for bad in _KEY_FORBIDDEN) or len(key) != 16:
            raise ValueError("retrieval key must be the exact 16-character cache key")
        if not self._cache_dir.exists():
            return None
        for path in bounded_rglob(
            self._cache_dir,
            f"*{key}*.txt",
            max_depth=1,
            max_files=self._max_cache_files,
        ):
            return path.read_text(encoding="utf-8")
        return None

    def _prune_cache_locked(self) -> None:
        """Remove oldest cached result files when the store exceeds its retention cap."""
        cached_files = list(bounded_rglob(self._cache_dir, "*.txt", max_depth=1, max_files=_CACHE_PRUNE_SCAN_LIMIT))
        if len(cached_files) <= self._max_cache_files:
            return

        def sort_key(path: Path) -> tuple[float, str]:
            try:
                return (path.stat().st_mtime, path.name)
            except OSError as exc:
                logger.warning("Could not stat cached tool result %s for pruning order: %s", path, exc)
                return (0.0, path.name)

        for path in sorted(cached_files, key=sort_key)[: len(cached_files) - self._max_cache_files]:
            try:
                path.unlink()
            except FileNotFoundError:
                logger.debug("Cached tool result already removed before prune: %s", path)
                continue
            except OSError:
                logger.warning("Could not prune cached tool result %s", path, exc_info=True)
