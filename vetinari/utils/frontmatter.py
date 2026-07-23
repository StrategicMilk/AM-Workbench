"""YAML frontmatter parsing utilities.

Provides a single canonical implementation for extracting YAML frontmatter
delimited by ``---`` from markdown files. Replaces two independent
implementations in ``agents/prompt_loader.py`` and ``skills/catalog_loader.py``.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import yaml

logger = logging.getLogger(__name__)


# Frontmatter regex: captures content between opening and closing --- delimiters
_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)


class FrontmatterError(ValueError):
    """Raised when strict frontmatter parsing encounters invalid metadata."""


def parse_frontmatter(content: str, *, strict: bool = False) -> tuple[dict[str, Any], str]:
    """Parse YAML frontmatter from markdown content and return metadata and body.

    Matches an opening ``---`` delimiter at the start of the content, extracts
    the YAML block, and returns the parsed metadata alongside the remaining
    body text after the closing ``---``.

    Args:
        content: Full markdown file content, potentially starting with ``---``.
        strict: When True, invalid YAML or non-dict metadata raises
            FrontmatterError instead of being treated as absent metadata.

    Returns:
        A tuple of ``(metadata, body)`` where ``metadata`` is the parsed
        frontmatter dict (empty if absent or invalid) and ``body`` is the
        remaining text after the closing delimiter.

    Raises:
        FrontmatterError: If ``strict`` is True and metadata is malformed.
    """
    match = _FRONTMATTER_RE.match(content)
    if not match:
        return {}, content

    try:
        metadata = yaml.safe_load(match.group(1)) or {}
        if not isinstance(metadata, dict):
            msg = f"Frontmatter did not parse to a dict; got {type(metadata)}"
            if strict:
                raise FrontmatterError(msg)
            logger.warning(msg)
            metadata = {}
    except yaml.YAMLError as exc:
        if strict:
            raise FrontmatterError(f"Failed to parse frontmatter YAML: {exc}") from exc
        logger.warning("Failed to parse frontmatter YAML: %s", exc)
        metadata = {}

    body = content[match.end() :]
    return metadata, body
