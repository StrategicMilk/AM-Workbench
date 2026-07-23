"""Runtime configuration path resolution."""

from __future__ import annotations

import os
from pathlib import Path

_PACKAGE_ROOT = Path(__file__).resolve().parent
_PACKAGE_CONFIG_DIR = _PACKAGE_ROOT / "config"
_PACKAGE_RUNTIME_CONFIG_DIR = _PACKAGE_CONFIG_DIR / "runtime"
_PROJECT_CONFIG_DIR = _PACKAGE_ROOT.parent / "config"
_CONFIG_DIR_ENV = "VETINARI_CONFIG_DIR"


def _configured_project_config_dir() -> Path:
    """Return the active source-tree config directory without assuming install layout."""
    override = os.environ.get(_CONFIG_DIR_ENV)
    if override:
        return Path(override).expanduser().resolve()
    return _PROJECT_CONFIG_DIR


def resolve_config_path(*parts: str) -> Path:
    """Return a source-tree config path or the packaged runtime fallback.

    Returns:
        The first existing config path for ``parts``, or the source-tree path
        where the config should be created.
    """
    project_path = _configured_project_config_dir().joinpath(*parts)
    if os.environ.get(_CONFIG_DIR_ENV):
        return project_path
    if project_path.exists():
        return project_path

    packaged_runtime_path = _PACKAGE_RUNTIME_CONFIG_DIR.joinpath(*parts)
    if packaged_runtime_path.exists():
        return packaged_runtime_path

    packaged_path = _PACKAGE_CONFIG_DIR.joinpath(*parts)
    if packaged_path.exists():
        return packaged_path

    return project_path
