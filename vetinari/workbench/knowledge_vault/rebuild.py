"""Rebuild planner facade for Knowledge Vault manifests."""

from __future__ import annotations

from .contracts import VaultManifest, VaultRebuildPlan
from .exporter import KnowledgeVaultExporter


class VaultRebuildPlanner:
    """Small wrapper used by API callers that only need planning."""

    def __init__(self, exporter: KnowledgeVaultExporter) -> None:
        self.exporter = exporter

    def plan(self, manifest: VaultManifest) -> VaultRebuildPlan:
        return self.exporter.rebuild_vault(manifest)


__all__ = ["VaultRebuildPlanner"]
