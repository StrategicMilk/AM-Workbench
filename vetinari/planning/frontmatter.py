"""Planning frontmatter parser compatibility wrapper."""

from __future__ import annotations

from typing import Any

from vetinari.utils.frontmatter import parse_frontmatter as _parse_frontmatter


def parse_frontmatter(content: str) -> dict[str, Any]:
    """Parse YAML frontmatter from text.

    Args:
        content: Document content.

    Returns:
        Mapping with ``metadata`` and ``body`` keys.
    """
    metadata, body = _parse_frontmatter(content.lstrip("\ufeff"), strict=False)
    return {"metadata": metadata, "body": body}


__all__ = ["parse_frontmatter"]
