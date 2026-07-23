"""Vetinari package - Comprehensive AI Orchestration System."""

from __future__ import annotations

__version__ = "0.6.0"

import logging as _logging
import os as _os
import pathlib as _pathlib
import shlex as _shlex
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from types import ModuleType

    from vetinari import public_api as public_api

    adapters: ModuleType
    cli: ModuleType
    code_sandbox: ModuleType
    kaizen: ModuleType
    memory: ModuleType
    model_discovery: ModuleType
    orchestration: ModuleType
    public_api: ModuleType
    token_optimizer: ModuleType
    tool_interface: ModuleType
    training: ModuleType
    utils: ModuleType


def _logger() -> _logging.Logger:
    return _logging.getLogger(__name__)


def _parse_env_value(value: str) -> str:
    """Parse a minimal dotenv value, including documented quoted values."""
    stripped = value.strip()
    if not stripped:
        return ""
    if stripped[0] in {"'", '"'}:
        logger = _logger()
        try:
            parsed = _shlex.split(stripped, comments=False, posix=True)
        except ValueError:
            logger.warning("Could not parse quoted environment value; using raw value")
            return stripped
        if len(parsed) == 1:
            return parsed[0]
    return stripped


def _load_env_file() -> None:
    """Load .env file into os.environ (lightweight, no dependencies).

    Searches upward from this file's directory for a ``.env`` file and sets
    any ``KEY=VALUE`` pairs that are not already in the environment.
    """
    start = _pathlib.Path(__file__).resolve().parent.parent
    for candidate in (start, start.parent, _pathlib.Path.cwd()):
        env_path = candidate / ".env"
        if env_path.is_file():
            try:
                for raw_line in env_path.read_text(encoding="utf-8").splitlines():
                    line = raw_line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, _, value = line.partition("=")
                    key = key.strip()
                    value = _parse_env_value(value)
                    # Don't overwrite values already in the environment
                    if key and key not in _os.environ:
                        _os.environ[key] = value
            except (OSError, UnicodeDecodeError):
                # .env file exists but is unreadable (permission error, encoding issue,
                # etc.) — skip it silently so that startup is never blocked by env loading.
                # Logging is not used here because this runs before logging is configured.
                _logger().warning("Unable to load environment file %s", env_path, exc_info=True)
            return  # Stop after first .env found


def bootstrap_environment() -> None:
    """Explicitly load the nearest Vetinari ``.env`` file into the process."""
    _load_env_file()


__all__ = [
    "adapters",
    "bootstrap_environment",
    "cli",
    "code_sandbox",
    "kaizen",
    "memory",
    "model_discovery",
    "models",
    "orchestration",
    "public_api",
    "token_optimizer",
    "tool_interface",
    "training",
    "utils",
]


# IMPLICIT: __getattr__ triggers lazy loading of the subpackages listed in
# __all__ on first attribute access.  Specifically, accessing any of:
#   vetinari.cli, vetinari.code_sandbox, vetinari.kaizen, vetinari.memory,
#   vetinari.model_discovery, vetinari.token_optimizer, vetinari.tool_interface,
#   vetinari.training, vetinari.utils
# causes importlib.import_module() to run, which performs filesystem I/O
# (finds and reads the subpackage's __init__.py).  All other attribute
# accesses raise AttributeError immediately without I/O.
#
# This mechanism exists solely to satisfy unittest.mock.patch on Python 3.10,
# where mock's internal _importer uses getattr traversal rather than
# importlib.import_module directly.  Without this, mock.patch paths like
# "vetinari.memory.unified.func" fail to resolve the intermediate "memory"
# package attribute.
def __getattr__(name: str):
    """Lazily import subpackages on attribute access.

    Required for ``unittest.mock.patch("vetinari.memory.unified.func")``
    on Python 3.10 where mock's ``_importer`` uses ``getattr`` traversal
    instead of ``importlib.import_module``.
    """
    import importlib

    if name in __all__:
        return importlib.import_module(f"vetinari.{name}")
    raise AttributeError(f"module 'vetinari' has no attribute {name!r}")
