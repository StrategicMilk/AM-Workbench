"""Composition-only Knowledge Vault exporter."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from .contracts import (
    VaultConfig,
    VaultEntry,
    VaultEntryCandidate,
    VaultManifest,
    VaultRebuildPlan,
    VaultSchemaValidator,
)
from .renderer import VaultRenderError, render_entry
from .scopes import VaultExportScope, VaultExportVerdict, VaultScopePolicy

logger = logging.getLogger(__name__)


class VaultPathTraversalError(VaultRenderError):
    """Raised when a rendered path escapes the configured vault root."""


class KnowledgeVaultExporter:
    """Export supplied candidates without mutating authoritative memory state."""

    def __init__(
        self,
        scope_policy: VaultScopePolicy | None = None,
        config: VaultConfig | None = None,
        schema_validator: VaultSchemaValidator | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self.config = config or VaultConfig()
        self.scope_policy = scope_policy or VaultScopePolicy.from_config(self.config)
        self.schema_validator = schema_validator or VaultSchemaValidator()
        self.clock = clock
        self.last_manifest = VaultManifest(entries=())

    def export_entries(
        self, records: Sequence[VaultEntryCandidate], requested_scope: VaultExportScope
    ) -> VaultManifest:
        """Execute the export entries operation.

        Args:
            records: Typed record consumed by the operation.
            requested_scope: Request object sent through the operation.

        Returns:
            VaultManifest value produced by export_entries().
        """
        accepted: list[VaultEntry] = []
        rejected = []
        created: list[str] = []
        updated: list[str] = []
        unchanged: list[str] = []
        root = self.config.vault_root
        root.mkdir(parents=True, exist_ok=True)
        verdicts = {id(candidate): self._evaluate(candidate, requested_scope) for candidate in records}
        allowed_slugs = {_normalize_slug(candidate.slug) for candidate in records if verdicts[id(candidate)].allowed}
        planned_paths = {
            _normalize_slug(candidate.slug): _safe_path(root, _normalize_slug(candidate.slug))
            for candidate in records
            if verdicts[id(candidate)].allowed
        }
        existing_bytes = _read_existing_vault_bytes(tuple(planned_paths.values()))

        for candidate in records:
            verdict = verdicts[id(candidate)]
            if not verdict.allowed:
                rejected.append(candidate.to_rejected(verdict.reasons))
                continue
            slug = _normalize_slug(candidate.slug)
            path = _safe_path(root, slug)
            draft = candidate.to_entry(content_hash="0" * 64)
            rendered = render_entry(replace(draft, slug=slug), allowed_wiki_slugs=allowed_slugs)
            entry = replace(draft, slug=slug, content_hash=rendered.content_hash)
            self.schema_validator.validate(entry.to_dict())
            old_bytes = existing_bytes.get(path)
            existed = path in existing_bytes
            if old_bytes == rendered.body_bytes:
                unchanged.append(slug)
            else:
                _atomic_write(path, rendered.body_bytes)
                (updated if existed else created).append(slug)
            accepted.append(entry)

        manifest = VaultManifest(
            entries=tuple(sorted(accepted, key=lambda item: item.slug)),
            rejected=tuple(sorted(rejected, key=lambda item: item.slug)),
            created=tuple(sorted(created)),
            updated=tuple(sorted(updated)),
            unchanged=tuple(sorted(unchanged)),
            manifest_hash=_manifest_hash(accepted),
        )
        self.last_manifest = manifest
        return manifest

    def rebuild_vault(self, manifest: VaultManifest) -> VaultRebuildPlan:
        """Execute the rebuild vault operation.

        Returns:
            VaultRebuildPlan value produced by rebuild_vault().
        """
        root = self.config.vault_root
        unchanged = []
        created = []
        updated = []
        expected = {entry.slug: entry for entry in manifest.entries}
        expected_paths = tuple(_safe_path(root, slug) for slug in sorted(expected))
        existing_bytes = _read_existing_vault_bytes(expected_paths)
        for slug, entry in sorted(expected.items()):
            path = _safe_path(root, slug)
            rendered = render_entry(entry, allowed_wiki_slugs=set(expected))
            old_bytes = existing_bytes.get(path)
            if old_bytes is None:
                created.append(slug)
            elif hashlib.sha256(old_bytes).hexdigest() == rendered.content_hash:
                unchanged.append(slug)
            else:
                updated.append(slug)
        existing = {path.stem for path in root.glob("*.md")} if root.exists() else set()
        removed = sorted(existing - set(expected) - {Path(self.config.index_file).stem})
        return VaultRebuildPlan(tuple(created), tuple(updated), tuple(removed), tuple(unchanged))

    def _evaluate(self, candidate: VaultEntryCandidate, requested_scope: VaultExportScope) -> VaultExportVerdict:
        try:
            return self.scope_policy.evaluate(candidate, requested_scope)
        except Exception:
            logger.warning("Handled recoverable failure before fallback.", exc_info=True)
            return VaultExportVerdict(False, ("policy-evaluation-error",))


def _normalize_slug(slug: str) -> str:
    raw = slug.strip()
    if raw.startswith(("..", "/", "\\")) or ".." in raw or "/" in raw or "\\" in raw:
        raise VaultPathTraversalError("vault slug resolves outside vault root")
    normalized = re.sub(r"[^a-z0-9._-]+", "-", slug.lower()).strip("-")
    if not normalized or normalized.startswith(("..", "/", "\\")):
        raise VaultPathTraversalError("vault slug resolves outside vault root")
    return normalized


def _safe_path(root: Path, slug: str) -> Path:
    path = (root / f"{_normalize_slug(slug)}.md").resolve()
    root_resolved = root.resolve()
    if root_resolved not in path.parents and path != root_resolved:
        raise VaultPathTraversalError("vault path escapes vault root")
    return path


def _atomic_write(path: Path, body: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("wb") as handle:
        handle.write(body)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def _read_existing_vault_bytes(paths: Sequence[Path]) -> dict[Path, bytes]:
    existing: dict[Path, bytes] = {}
    for path in paths:
        if path.exists():
            existing[path] = path.read_bytes()
    return existing


def _manifest_hash(entries: Sequence[VaultEntry]) -> str:
    payload = json.dumps([entry.to_dict() for entry in sorted(entries, key=lambda item: item.slug)], sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


__all__ = ["KnowledgeVaultExporter", "VaultPathTraversalError"]
