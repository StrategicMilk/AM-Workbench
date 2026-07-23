"""Deterministic Knowledge Vault index builder."""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path

from vetinari.ux import display_label

from .contracts import VaultEntryKind, VaultIndex, VaultManifest


def build_index(manifest: VaultManifest, *, index_file: str = "INDEX.md") -> VaultIndex:
    """Execute the build index operation.

    Returns:
        Newly constructed index value.
    """
    groups: dict[VaultEntryKind, list[str]] = defaultdict(list)
    for entry in manifest.entries:
        groups[entry.kind].append(entry.slug)
    lines = ["# Knowledge Vault", ""]
    for kind in VaultEntryKind:
        lines.extend([f"## {display_label(kind)}", ""])
        lines.extend(f"- [[{slug}]]" for slug in sorted(groups.get(kind, [])))
        if not groups.get(kind):
            lines.append("- None")
        lines.append("")
    lines.extend(["## Rejected", ""])
    lines.extend(
        f"- ~~{rejected.slug}~~ - {', '.join(rejected.reasons)}"
        for rejected in sorted(manifest.rejected, key=lambda item: item.slug)
    )
    if not manifest.rejected:
        lines.append("- None")
    lines.append("")
    return VaultIndex(
        entries=manifest.entries, rejected=manifest.rejected, generated_path=Path(index_file), body="\n".join(lines)
    )


__all__ = ["build_index"]
