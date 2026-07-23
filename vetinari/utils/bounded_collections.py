"""Shared bounded collection primitives for capacity-sensitive code paths."""

from __future__ import annotations

import fnmatch
import logging
import os
import threading
from collections import OrderedDict, deque
from collections.abc import Iterable, Iterator, MutableMapping
from pathlib import Path
from typing import Generic, TypeVar

_LOGGER = logging.getLogger(__name__)

T = TypeVar("T")
K = TypeVar("K")
V = TypeVar("V")


class BoundedList(Generic[T]):
    """Deque-backed list-like container that evicts oldest entries at maxlen."""

    def __init__(self, maxlen: int, iterable: Iterable[T] = ()) -> None:
        if maxlen < 1:
            raise ValueError("maxlen must be >= 1")
        self._dq: deque[T] = deque(iterable, maxlen=maxlen)

    @property
    def maxlen(self) -> int:
        """Return the configured maximum number of retained items."""
        return self._dq.maxlen or 0

    def append(self, item: T) -> None:
        """Append one item, evicting the oldest item if the list is full."""
        self._dq.append(item)

    def extend(self, items: Iterable[T]) -> None:
        """Append multiple items, retaining only the newest ``maxlen`` entries."""
        self._dq.extend(items)

    def __len__(self) -> int:
        return len(self._dq)

    def __iter__(self) -> Iterator[T]:
        return iter(self._dq)

    def __getitem__(self, index: int | slice) -> T | list[T]:
        if isinstance(index, slice):
            return list(self._dq)[index]
        return self._dq[index]

    def __repr__(self) -> str:
        return f"{type(self).__name__}(maxlen={self.maxlen}, items={list(self._dq)!r})"


class BoundedDict(MutableMapping[K, V]):
    """Thread-safe insertion-ordered dict that evicts oldest entries on overflow."""

    def __init__(
        self,
        maxsize: int,
        *,
        lock_factory: type[threading.RLock] | object = threading.RLock,
    ) -> None:
        if maxsize < 1:
            raise ValueError("maxsize must be >= 1")
        self.maxsize = maxsize
        self._lock = lock_factory()
        self._items: OrderedDict[K, V] = OrderedDict()

    def __setitem__(self, key: K, value: V) -> None:
        with self._lock:
            if key in self._items:
                self._items.move_to_end(key)
            self._items[key] = value
            while len(self._items) > self.maxsize:
                self._items.popitem(last=False)

    def __getitem__(self, key: K) -> V:
        with self._lock:
            return self._items[key]

    def __delitem__(self, key: K) -> None:
        with self._lock:
            del self._items[key]

    def __iter__(self) -> Iterator[K]:
        with self._lock:
            return iter(tuple(self._items.keys()))

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)

    def __contains__(self, key: object) -> bool:
        with self._lock:
            return key in self._items

    def get(self, key: K, default: V | None = None) -> V | None:
        """Return the value for ``key`` or ``default`` without mutating order.

        Args:
            key: Lookup key.
            default: Value returned when ``key`` is absent.

        Returns:
            Stored value or ``default``.
        """
        with self._lock:
            return self._items.get(key, default)

    def pop(self, key: K, default: V | object = _LOGGER) -> V | object:
        """Remove ``key`` and return its value, or ``default`` when provided.

        Args:
            key: Lookup key to remove.
            default: Optional value returned when ``key`` is absent.

        Returns:
            Removed value, or ``default`` when supplied and the key is absent.
        """
        with self._lock:
            if default is _LOGGER:
                return self._items.pop(key)
            return self._items.pop(key, default)

    def items(self):  # type: ignore[override]
        """Return a stable snapshot of key/value pairs.

        Returns:
            Tuple of key/value pairs.
        """
        with self._lock:
            return tuple(self._items.items())

    def keys(self):  # type: ignore[override]
        """Return a stable snapshot of keys.

        Returns:
            Tuple of keys.
        """
        with self._lock:
            return tuple(self._items.keys())

    def values(self):  # type: ignore[override]
        """Return a stable snapshot of values.

        Returns:
            Tuple of values.
        """
        with self._lock:
            return tuple(self._items.values())

    def __repr__(self) -> str:
        with self._lock:
            return f"{type(self).__name__}(maxsize={self.maxsize}, items={dict(self._items)!r})"


def bounded_rglob(
    root: Path,
    pattern: str,
    *,
    max_depth: int = 4,
    max_files: int = 10_000,
) -> Iterator[Path]:
    """Yield matching files below root without following symlinks or exceeding caps.

    Args:
        root: Directory to scan.
        pattern: Filename pattern accepted by :func:`fnmatch.fnmatch`.
        max_depth: Maximum directory depth below ``root`` to descend.
        max_files: Maximum matching paths to yield before stopping.

    Raises:
        ValueError: If ``max_depth`` or ``max_files`` is less than 1.
    """
    if max_depth < 1:
        raise ValueError("max_depth must be >= 1")
    if max_files < 1:
        raise ValueError("max_files must be >= 1")

    root = Path(root)
    yielded = 0

    def walk(directory: Path, depth: int) -> Iterator[Path]:
        nonlocal yielded
        if yielded >= max_files:
            return
        try:
            entries = list(os.scandir(directory))
        except OSError as exc:
            _LOGGER.warning("bounded_rglob could not scan %s: %s", directory, exc)
            entries = []
        for entry in entries:
            if yielded >= max_files:
                _LOGGER.warning(
                    "bounded_rglob stopped after max_files=%s under %s",
                    max_files,
                    root,
                )
                return
            try:
                if entry.is_symlink():
                    continue
                entry_path = Path(entry.path)
                entry_depth = depth + 1
                if fnmatch.fnmatch(entry.name, pattern):
                    yielded += 1
                    yield entry_path
                if entry.is_dir(follow_symlinks=False) and entry_depth < max_depth:
                    yield from walk(entry_path, entry_depth)
            except OSError as exc:
                _LOGGER.warning("bounded_rglob could not inspect %s: %s", entry.path, exc)

    if root.exists():
        yield from walk(root, 0)


__all__ = ["BoundedDict", "BoundedList", "bounded_rglob"]
