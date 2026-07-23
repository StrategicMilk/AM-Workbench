"""Extracted implementation helpers for standards_loader.py."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import yaml

logger = logging.getLogger(__name__)


class StandardsYamlMixin:
    """Shared method implementations for the compatibility wrapper."""

    if TYPE_CHECKING:
        _dir: Any
        _lock: Any
        _yaml_cache: Any

    def _read_yaml(self, filename: str) -> dict[str, Any]:
        """Read and parse a YAML file with mtime caching.

        Fail closed: if the file is absent or unreadable, propagate the
        underlying filesystem error so callers cannot silently build prompts
        with an empty standards contract.

        Args:
            filename: Name of the YAML file in the standards directory.

        Returns:
            Parsed YAML content as a dictionary.
        """
        filepath = self._dir / filename
        try:
            mtime = filepath.stat().st_mtime
        except FileNotFoundError as exc:
            msg = f"Standards YAML file not found: {filepath}"
            raise FileNotFoundError(msg) from exc
        except OSError:
            logger.exception("Standards YAML file is unreadable: %s", filepath)
            raise

        with self._lock:
            cached = self._yaml_cache.get(filename)
            if cached and cached[0] == mtime:
                return cached[1]

        try:
            content = filepath.read_text(encoding="utf-8")
        except OSError:
            logger.exception("Could not read standards YAML file %s", filepath)
            raise

        parsed = yaml.safe_load(content) or {}
        with self._lock:
            self._yaml_cache[filename] = (mtime, parsed)
        return parsed
