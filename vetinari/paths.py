"""Centralized path resolution for the Vetinari project.

All runtime path construction should flow through this module rather than
using ad-hoc ``Path(__file__).parent`` or string concatenation calls.  The
canonical directory constants live in :mod:`vetinari.constants`; this
module adds higher-level resolution helpers that combine those constants
with project IDs, model names, or other dynamic segments.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Any

from vetinari.constants import (
    CHECKPOINT_DIR,
    LOGS_DIR,
    MODEL_CACHE_DIR,
    OUTPUTS_DIR,
    PROJECTS_DIR,
    VETINARI_STATE_DIR,
    get_user_dir,
)

logger = logging.getLogger(__name__)


__all__ = [
    "ensure_dir",
    "privacy_receipt_for_path",
    "resolve_checkpoint_path",
    "resolve_log_path",
    "resolve_model_cache_path",
    "resolve_output_path",
    "resolve_project_path",
    "resolve_state_path",
    "resolve_user_path",
]


# ── Core helpers ────────────────────────────────────────────────────────────


def _safe_join(root: Path, segments: tuple[str, ...]) -> Path:
    """Join *segments* onto *root*, raising if the result escapes *root*.

    Args:
        root: The trusted base directory that the result must remain inside.
        segments: Path components to append to *root*.

    Returns:
        Resolved absolute path that is guaranteed to be inside *root*.

    Raises:
        ValueError: If the joined path escapes *root* (path traversal attempt).
    """
    result = root.joinpath(*segments).resolve()
    if not result.is_relative_to(root.resolve()):
        raise ValueError(f"Path segments {segments!r} escape the root {root} — path traversal rejected")
    return result


def ensure_dir(path: Path) -> Path:
    """Create *path* (and parents) if it does not exist, then return it.

    Args:
        path: Directory path to ensure exists.

    Returns:
        The same *path*, guaranteed to exist on disk.
    """
    path.mkdir(parents=True, exist_ok=True)
    return path


def privacy_receipt_for_path(
    path: Path | str,
    *,
    privacy_class: str = "operational",
    subject_id: str | None = None,
    source: str = "vetinari.paths",
) -> dict[str, Any]:
    """Build a privacy receipt for a resolved path reference.

    Args:
        path: Filesystem path to fingerprint after user and symlink
            resolution.
        privacy_class: Privacy classification to attach to the receipt.
        subject_id: Optional subject identifier for subject-bound records.
        source: Receipt source namespace used in the erasure token.

    Returns:
        Privacy receipt for the resolved path, including an erasure token based
        on a SHA-256 path fingerprint instead of the raw path.
    """
    from vetinari.privacy.envelope import privacy_receipt

    resolved = Path(path).expanduser().resolve()
    path_fingerprint = hashlib.sha256(resolved.as_posix().encode("utf-8", errors="replace")).hexdigest()
    return privacy_receipt(
        privacy_class=privacy_class,
        subject_id=subject_id,
        source=source,
        erasure_token=f"{source}:path:{path_fingerprint}",
        redaction_applied=privacy_class in {"subject_data", "secret"},
    )


# ── Project paths ───────────────────────────────────────────────────────────


def resolve_project_path(project_id: str, *segments: str) -> Path:
    """Build an absolute path under the projects directory.

    Args:
        project_id: Unique project identifier (used as subdirectory name).
        *segments: Additional path segments appended after the project dir.

    Returns:
        Resolved :class:`Path` — directory is **not** auto-created.

    Raises:
        ValueError: If *project_id* is empty.
    """
    if not project_id:
        msg = "project_id must not be empty"
        raise ValueError(msg)
    project_dir = PROJECTS_DIR / project_id
    if not project_dir.resolve().is_relative_to(PROJECTS_DIR.resolve()):
        raise ValueError(f"project_id contains path traversal: {project_id}")
    return project_dir / Path(*segments) if segments else project_dir


def resolve_output_path(project_id: str, filename: str) -> Path:
    """Build an absolute path for a project output artefact.

    Args:
        project_id: Unique project identifier.
        filename: Output file basename (e.g. ``"result.json"``).

    Returns:
        Path under ``<OUTPUTS_DIR>/<project_id>/<filename>``.
    """
    return OUTPUTS_DIR / project_id / filename


# ── State / checkpoint paths ────────────────────────────────────────────────


def resolve_state_path(*segments: str) -> Path:
    """Build an absolute path under the ``.vetinari`` state directory.

    Args:
        *segments: Path segments appended to the state root.

    Returns:
        Resolved :class:`Path`.

    Raises:
        ValueError: If *segments* attempt path traversal outside the state root.
    """
    if not segments:
        return VETINARI_STATE_DIR
    return _safe_join(VETINARI_STATE_DIR, segments)


def resolve_checkpoint_path(run_id: str) -> Path:
    """Return the checkpoint directory for a training run.

    Args:
        run_id: Unique training run identifier.

    Returns:
        Path under ``<CHECKPOINT_DIR>/<run_id>/``.
    """
    return CHECKPOINT_DIR / run_id


# ── Logging / cache paths ──────────────────────────────────────────────────


def resolve_log_path(filename: str = "vetinari.log") -> Path:
    """Return the full path to a log file.

    Args:
        filename: Log file basename.

    Returns:
        Path under ``<LOGS_DIR>/<filename>``.
    """
    return LOGS_DIR / filename


def resolve_model_cache_path(model_name: str) -> Path:
    """Return the cache directory for a downloaded model.

    Args:
        model_name: Model identifier (used as subdirectory name).

    Returns:
        Path under ``<MODEL_CACHE_DIR>/<model_name>/``.
    """
    return MODEL_CACHE_DIR / model_name


# ── User-level paths ───────────────────────────────────────────────────────


def resolve_user_path(*segments: str) -> Path:
    """Build an absolute path under the user-level Vetinari directory.

    The user directory (``~/.vetinari`` by default) stores per-user config,
    downloaded models, and credentials that are not project-specific.

    Re-reads ``VETINARI_USER_DIR`` from the environment on every call so that
    test overrides via ``monkeypatch.setenv`` take effect without restarting.

    Args:
        *segments: Path segments appended to the user directory root.

    Returns:
        Resolved :class:`Path`.

    Raises:
        ValueError: If *segments* attempt path traversal outside the user root.
    """
    user_dir = get_user_dir()
    if not segments:
        return user_dir
    return _safe_join(user_dir, segments)
