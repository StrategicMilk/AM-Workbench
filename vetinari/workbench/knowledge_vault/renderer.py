"""Deterministic markdown renderer for Knowledge Vault entries."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .contracts import VaultEntry, VaultEntryKind


class VaultRenderError(ValueError):
    """Raised when a vault entry cannot be rendered safely."""


@dataclass(frozen=True, slots=True)
class RenderedVaultFile:
    """Runtime contract for RenderedVaultFile."""

    path: Path
    body_bytes: bytes
    content_hash: str


def render_entry(entry: VaultEntry, *, allowed_wiki_slugs: set[str] | None = None) -> RenderedVaultFile:
    """Render an entry to stable frontmatter plus markdown body bytes.

    Returns:
        RenderedVaultFile value produced by render_entry().

    Raises:
        Exception: Propagates validation or runtime failures from the underlying operation.
    """
    if not isinstance(entry.kind, VaultEntryKind):
        raise VaultRenderError("entry.kind must be VaultEntryKind")
    if not 0.0 <= entry.confidence <= 1.0:
        raise VaultRenderError("entry.confidence must be between 0.0 and 1.0")
    if allowed_wiki_slugs is not None:
        unknown = sorted(set(entry.wiki_links) - allowed_wiki_slugs)
        if unknown:
            raise VaultRenderError(f"unknown wiki links: {unknown}")
    governed_fields = {
        "authority",
        "boundary_class",
        "confidence",
        "entry_id",
        "kind",
        "lifecycle_state",
        "provenance_refs",
        "slug",
        "source_links",
        "title",
    }
    forged_fields = sorted(str(key) for key in dict(entry.frontmatter) if str(key) in governed_fields)
    if forged_fields:
        raise VaultRenderError(f"user frontmatter cannot set governed fields: {forged_fields}")
    user_frontmatter = {
        str(key): value for key, value in dict(entry.frontmatter).items() if str(key) not in governed_fields
    }
    frontmatter: dict[str, Any] = {
        **user_frontmatter,
        "authority": entry.authority.value,
        "boundary_class": entry.boundary_class.value,
        "confidence": entry.confidence,
        "entry_id": entry.entry_id,
        "kind": entry.kind.value,
        "lifecycle_state": entry.lifecycle_state.value
        if hasattr(entry.lifecycle_state, "value")
        else str(entry.lifecycle_state),
        "provenance_refs": [ref.ref_id for ref in entry.provenance_refs],
        "slug": entry.slug,
        "source_links": list(entry.source_links),
        "title": entry.title,
    }
    frontmatter_text = "\n".join(
        f"{key}: {json.dumps(frontmatter[key], sort_keys=True)}" for key in sorted(frontmatter)
    )
    links = " ".join(f"[[{slug}]]" for slug in entry.wiki_links)
    body = f"---\n{frontmatter_text}\n---\n\n# {entry.title}\n\n{links}\n"
    body_bytes = body.encode("utf-8")
    digest = hashlib.sha256(body_bytes).hexdigest()
    return RenderedVaultFile(path=Path(f"{entry.slug}.md"), body_bytes=body_bytes, content_hash=digest)


__all__ = ["RenderedVaultFile", "VaultRenderError", "render_entry"]
