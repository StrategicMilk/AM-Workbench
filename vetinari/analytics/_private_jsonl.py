"""Private, cross-process-safe JSONL filesystem primitives."""

from __future__ import annotations

import logging
import os
import stat
import threading
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO, TextIO

from vetinari.analytics._windows_security import (
    _secure_windows_path,
    _verify_windows_handle_owner_identity,
    _verify_windows_owner_identity,
)

logger = logging.getLogger(__name__)

_LOCK_TIMEOUT_SECONDS = 30.0
_LOCK_POLL_SECONDS = 0.05
_SERVICE_UID_ENV = "VETINARI_COST_STORAGE_SERVICE_UID"
_LEDGER_THREAD_LOCK = threading.RLock()


@contextmanager
def _cost_ledger_transaction(path: Path) -> Iterator[None]:
    """Serialize a ledger transaction across threads and operating-system processes."""
    with _LEDGER_THREAD_LOCK:
        _ensure_private_parent(path)
        lock_path = _cost_ledger_lock_path(path)
        with _open_private_lock(lock_path) as lock_handle:
            _acquire_cross_process_lock(lock_handle, lock_path)
            try:
                yield
            finally:
                _release_cross_process_lock(lock_handle)


def _cost_ledger_lock_path(path: Path) -> Path:
    """Return the stable sidecar used to synchronize every ledger generation."""
    resolved = path.resolve(strict=False)
    return resolved.with_name(f".{resolved.name}.lock")


@contextmanager
def _open_private_lock(path: Path) -> Iterator[BinaryIO]:
    """Open a non-link private lock file and ensure its lock byte exists."""
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        _verify_open_file_handle(path, descriptor)
        _secure_private_file(path)
        _verify_open_file_handle(path, descriptor)
        if os.fstat(descriptor).st_size == 0:
            os.write(descriptor, b"\0")
        os.lseek(descriptor, 0, os.SEEK_SET)
        with os.fdopen(descriptor, "r+b", buffering=0) as handle:
            descriptor = -1
            yield handle
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _acquire_cross_process_lock(handle: BinaryIO, path: Path) -> None:
    """Acquire one exclusive byte-range lock with a bounded fail-closed timeout."""
    deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
    while True:
        handle.seek(0)
        try:
            if os.name == "nt":
                msvcrt = __import__("msvcrt")
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl = __import__("fcntl")
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except OSError as exc:
            if time.monotonic() >= deadline:
                raise TimeoutError(f"cost ledger lock acquisition timed out: {path}") from exc
            time.sleep(_LOCK_POLL_SECONDS)


def _release_cross_process_lock(handle: BinaryIO) -> None:
    """Release the platform-specific byte-range lock held by ``handle``."""
    handle.seek(0)
    if os.name == "nt":
        msvcrt = __import__("msvcrt")
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return
    fcntl = __import__("fcntl")
    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _rotate_jsonl_if_needed(path: Path, incoming_bytes: int, *, max_bytes: int, backup_count: int) -> None:
    """Rotate a JSONL ledger before an append would exceed its byte cap."""
    current = _private_regular_file_stat(path)
    if current is None or current.st_size + incoming_bytes <= max_bytes:
        return
    if backup_count == 0:
        path.unlink()
        return
    oldest = path.with_name(f"{path.name}.{backup_count}")
    if _path_exists_strict(oldest):
        oldest.unlink()
    for index in range(backup_count - 1, 0, -1):
        source = path.with_name(f"{path.name}.{index}")
        if _path_exists_strict(source):
            destination = path.with_name(f"{path.name}.{index + 1}")
            if _path_exists_strict(destination):
                raise FileExistsError(f"cost ledger rotation destination unexpectedly exists: {destination}")
            source.replace(destination)
            _secure_private_file(destination)
    destination = path.with_name(f"{path.name}.1")
    if _path_exists_strict(destination):
        raise FileExistsError(f"cost ledger rotation destination unexpectedly exists: {destination}")
    path.replace(destination)
    _secure_private_file(destination)


def _bounded_jsonl_paths(path: Path, *, backup_count: int) -> list[Path]:
    """Return rotated JSONL ledgers from oldest backup to active file."""
    paths = [path.with_name(f"{path.name}.{index}") for index in range(backup_count, 0, -1)]
    paths.append(path)
    return [candidate for candidate in paths if _path_exists_strict(candidate)]


def _path_exists_strict(path: Path) -> bool:
    """Return whether a path exists without hiding unreadable state."""
    return _private_regular_file_stat(path) is not None


def _rewrite_compacted_ledgers(path: Path, chunks: Sequence[Sequence[str]], *, backup_count: int) -> None:
    """Durably replace compacted ledger files and remove stale rotations."""
    candidates = [path.with_name(f"{path.name}.{index}") for index in range(backup_count, 0, -1)]
    candidates.append(path)
    existing = [candidate for candidate in candidates if _path_exists_strict(candidate)]
    if not chunks and not existing:
        return
    _ensure_private_parent(path)
    destinations = [path.with_name(f"{path.name}.{index}") for index in range(len(chunks) - 1, 0, -1)]
    if chunks:
        destinations.append(path)
    staged: list[tuple[Path, Path]] = []
    try:
        for index, (destination, chunk) in enumerate(zip(destinations, chunks, strict=True)):
            temporary = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.{index}.compact.tmp")
            _write_private_text(temporary, "".join(chunk))
            staged.append((temporary, destination))
        for temporary, destination in staged:
            os.replace(temporary, destination)
            _secure_private_file(destination)
        desired = set(destinations)
        for candidate in candidates:
            if candidate not in desired and _path_exists_strict(candidate):
                candidate.unlink()
        _sync_directory(path.parent)
    finally:
        for temporary, _destination in staged:
            try:
                if _path_exists_strict(temporary):
                    temporary.unlink()
            except OSError:
                logger.exception("Failed to remove staged cost-ledger compaction file: path=%r", temporary)


def _ensure_private_parent(path: Path) -> None:
    """Create and secure the immediate ledger directory without accepting links."""
    parent = path.parent
    _validate_path_components(parent)
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    metadata = os.lstat(parent)
    _reject_link_or_reparse(parent, metadata)
    if not stat.S_ISDIR(metadata.st_mode):
        raise OSError(f"cost ledger parent must be a directory: {parent}")
    if parent not in {Path("."), Path("")}:
        _secure_private_path(parent, directory=True)


def _private_regular_file_stat(path: Path) -> os.stat_result | None:
    """Return private regular-file metadata without following links or reparse points."""
    try:
        metadata = os.lstat(path)
    except FileNotFoundError:
        metadata = None
    if metadata is None:
        return None
    _reject_link_or_reparse(path, metadata)
    if not stat.S_ISREG(metadata.st_mode):
        raise OSError(f"cost ledger path must be a regular file: {path}")
    _secure_private_file(path)
    return os.lstat(path)


def _reject_link_or_reparse(path: Path, metadata: os.stat_result) -> None:
    """Reject symbolic links and Windows reparse points before storage mutation."""
    file_attributes = getattr(metadata, "st_file_attributes", 0)
    reparse_attribute = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if stat.S_ISLNK(metadata.st_mode) or file_attributes & reparse_attribute:
        raise OSError(f"cost ledger path must not be a link or reparse point: {path}")


def _secure_private_file(path: Path) -> None:
    """Apply and verify platform-native private access on a non-link ledger file."""
    _secure_private_path(path, directory=False)


def _secure_private_path(path: Path, *, directory: bool) -> None:
    """Apply and verify owner-only storage policy on Windows and POSIX."""
    _validate_path_components(path)
    metadata = os.lstat(path)
    _reject_link_or_reparse(path, metadata)
    expected_kind = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected_kind(metadata.st_mode):
        expected = "directory" if directory else "regular file"
        raise OSError(f"cost ledger path must be a {expected}: {path}")
    _verify_metadata_owner(path, metadata)
    if not directory:
        _verify_single_link(path, metadata)
    if os.name == "nt":
        _secure_windows_path(path, directory=directory)
        return
    if os.name == "posix":
        expected_mode = 0o700 if directory else 0o600
        os.chmod(path, expected_mode, follow_symlinks=False)
        if stat.S_IMODE(os.lstat(path).st_mode) != expected_mode:
            raise PermissionError(f"cost ledger permissions are not private: {path}")
        return
    raise PermissionError(f"cost ledger private-storage policy is unsupported on platform {os.name!r}: {path}")


def _validate_path_components(path: Path) -> None:
    """Walk every existing path component and reject links or Windows reparse points."""
    absolute = Path(os.path.abspath(path))
    current = Path(absolute.anchor)
    for component in absolute.parts[1:]:
        current /= component
        try:
            metadata = os.lstat(current)
        except FileNotFoundError:
            metadata = None
        if metadata is None:
            return
        _reject_link_or_reparse(current, metadata)


def _verify_metadata_owner(path: Path, metadata: os.stat_result) -> None:
    """Require current-process or explicitly configured service ownership."""
    if os.name == "nt":
        _verify_windows_owner_identity(path)
        return
    if os.name != "posix":
        raise PermissionError(f"cost ledger ownership policy is unsupported on platform {os.name!r}: {path}")
    if metadata.st_uid not in _expected_posix_owner_uids():
        raise PermissionError(
            f"cost ledger owner UID is not the current process or configured service identity: {path}"
        )


def _expected_posix_owner_uids() -> set[int]:
    """Return the effective process UID plus an optional configured service UID."""
    get_effective_uid = getattr(os, "geteuid", None)
    if get_effective_uid is None:
        raise PermissionError("POSIX effective-owner identity is unavailable")
    expected = {int(get_effective_uid())}
    configured = os.environ.get(_SERVICE_UID_ENV)
    if configured is None:
        return expected
    try:
        configured_uid = int(configured)
    except ValueError as exc:
        raise ValueError(f"{_SERVICE_UID_ENV} must be a non-negative integer UID") from exc
    if configured_uid < 0:
        raise ValueError(f"{_SERVICE_UID_ENV} must be a non-negative integer UID")
    expected.add(configured_uid)
    return expected


def _verify_single_link(path: Path, metadata: os.stat_result) -> None:
    """Reject regular files reachable through more than one hard-link name."""
    if metadata.st_nlink != 1:
        raise PermissionError(f"cost ledger file has unexpected hard links ({metadata.st_nlink}): {path}")


def _verify_open_file_handle(path: Path, descriptor: int) -> None:
    """Verify owner, link count, kind, and identity on an already-open descriptor."""
    handle_metadata = os.fstat(descriptor)
    if not stat.S_ISREG(handle_metadata.st_mode):
        raise OSError(f"cost ledger open handle must reference a regular file: {path}")
    _verify_single_link(path, handle_metadata)
    if os.name == "nt":
        _verify_windows_handle_owner_identity(descriptor)
    elif os.name == "posix":
        if handle_metadata.st_uid not in _expected_posix_owner_uids():
            raise PermissionError(
                f"cost ledger open handle owner UID is not the current process or configured service identity: {path}"
            )
    else:
        raise PermissionError(f"cost ledger handle ownership is unsupported on platform {os.name!r}: {path}")
    path_metadata = os.lstat(path)
    _reject_link_or_reparse(path, path_metadata)
    if not os.path.samestat(handle_metadata, path_metadata):
        raise PermissionError(f"cost ledger open handle no longer matches its path: {path}")


def _private_open_flags(*, append: bool) -> int:
    """Return no-follow creation flags for a private ledger file."""
    flags = os.O_WRONLY | os.O_CREAT | (os.O_APPEND if append else os.O_EXCL)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_BINARY", 0)
    return flags


@contextmanager
def _open_private_append(path: Path) -> Iterator[TextIO]:
    """Open an append-only ledger handle with private creation semantics."""
    descriptor = os.open(path, _private_open_flags(append=True), 0o600)
    try:
        _verify_open_file_handle(path, descriptor)
        _secure_private_file(path)
        _verify_open_file_handle(path, descriptor)
        with os.fdopen(descriptor, "a", encoding="utf-8", newline="") as handle:
            descriptor = -1
            yield handle
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _write_private_text(path: Path, content: str) -> None:
    """Create one new private file, flush it, and fsync its contents."""
    descriptor = os.open(path, _private_open_flags(append=False), 0o600)
    try:
        _verify_open_file_handle(path, descriptor)
        _secure_private_file(path)
        _verify_open_file_handle(path, descriptor)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            descriptor = -1
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)


@contextmanager
def _open_private_read(path: Path) -> Iterator[TextIO]:
    """Open one private ledger for reading and verify the opened handle identity."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    descriptor = os.open(path, flags)
    try:
        _verify_open_file_handle(path, descriptor)
        _secure_private_file(path)
        _verify_open_file_handle(path, descriptor)
        with os.fdopen(descriptor, "r", encoding="utf-8", newline="") as handle:
            descriptor = -1
            yield handle
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _sync_directory(path: Path) -> None:
    """Fsync a changed directory where the platform supports directory handles."""
    if os.name != "posix":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
