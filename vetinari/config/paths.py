"""Filesystem path helpers for user configuration."""

from __future__ import annotations

import os
from pathlib import Path

from vetinari.constants import get_user_dir


def get_user_config_dir() -> Path:
    """Return the user configuration directory.

    Returns:
        Path configured by ``VETINARI_USER_DIR`` or the default local state path.
    """
    return Path(os.environ["VETINARI_USER_DIR"]).expanduser() if "VETINARI_USER_DIR" in os.environ else get_user_dir()


def get_models_dir() -> Path:
    """Return the local model directory.

    Returns:
        Path configured by ``VETINARI_MODELS_DIR`` or under the user config dir.
    """
    return Path(os.environ.get("VETINARI_MODELS_DIR", str(get_user_config_dir() / "models"))).expanduser()


__all__ = ["get_models_dir", "get_user_config_dir"]
