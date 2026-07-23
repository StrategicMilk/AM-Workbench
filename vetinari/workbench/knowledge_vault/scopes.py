"""Fail-closed export scope policy for Knowledge Vault candidates."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from vetinari.memory.governance import BoundaryClass, MemoryLifecycleState, RetentionClass

from .contracts import VaultConfig, VaultEntryCandidate

logger = logging.getLogger(__name__)


class VaultExportScope(str, Enum):
    """Runtime contract for VaultExportScope."""

    SHAREABLE = "shareable"
    PRIVATE = "private"
    SENSITIVE = "sensitive"


@dataclass(frozen=True, slots=True)
class VaultExportVerdict:
    """Runtime contract for VaultExportVerdict."""

    allowed: bool
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class VaultScopePolicy:
    """Runtime contract for VaultScopePolicy."""

    allowed_export_scopes: Mapping[str, bool] = field(
        default_factory=lambda: {"shareable": True, "private": True, "sensitive": False}
    )
    confidence_floor: float = 0.1

    @classmethod
    def from_config(cls, config: VaultConfig) -> VaultScopePolicy:
        return cls(config.allowed_export_scopes, config.confidence_floor)

    def evaluate(self, entry: VaultEntryCandidate | Any, requested_scope: VaultExportScope | str) -> VaultExportVerdict:
        """Execute the evaluate operation.

        Args:
            entry: Entry value consumed by evaluate().
            requested_scope: Request object sent through the operation.

        Returns:
            VaultExportVerdict value produced by evaluate().
        """
        try:
            scope = VaultExportScope(requested_scope)
            reasons: list[str] = []
            if not self.allowed_export_scopes.get(scope.value, False):
                reasons.append(f"scope-disabled:{scope.value}")
            boundary = getattr(entry, "boundary_class", None)
            if boundary is None:
                reasons.append("missing-boundary-class")
            else:
                boundary = BoundaryClass(boundary)
            retention = getattr(entry, "retention_class", None)
            if retention is not None and RetentionClass(retention) in {
                RetentionClass.EXPIRED,
                RetentionClass.FORGET_REQUESTED,
            }:
                reasons.append(f"retention-{RetentionClass(retention).value}")
            if scope is VaultExportScope.SHAREABLE and boundary in {BoundaryClass.PRIVATE, BoundaryClass.MIXED}:
                reasons.append("private-not-shareable")
            if scope is not VaultExportScope.SENSITIVE and _sensitive(entry, boundary):
                reasons.append("sensitive-scope-denied")
            confidence = getattr(entry, "confidence", None)
            if confidence is None:
                reasons.append("missing-confidence")
            elif float(confidence) < self.confidence_floor:
                reasons.append("confidence-below-floor")
            lifecycle = getattr(entry, "lifecycle_state", None)
            lifecycle_value = lifecycle.value if isinstance(lifecycle, MemoryLifecycleState) else str(lifecycle or "")
            if lifecycle_value in {"quarantined", "tombstoned", "superseded", "forgotten"}:
                reasons.append(f"lifecycle-{lifecycle_value}")
            if not tuple(getattr(entry, "provenance_refs", ()) or ()):
                reasons.append("missing-provenance")
            return VaultExportVerdict(not reasons, tuple(reasons or ("allowed",)))
        except Exception:
            logger.warning("Handled recoverable failure before fallback.", exc_info=True)
            return VaultExportVerdict(False, ("policy-evaluation-error",))


def _sensitive(entry: Any, boundary: BoundaryClass | None) -> bool:
    tags = {str(tag).casefold() for tag in getattr(entry, "sensitivity_tags", ())}
    return boundary is BoundaryClass.UNKNOWN or "sensitive" in tags or "secret" in tags


__all__ = ["VaultExportScope", "VaultExportVerdict", "VaultScopePolicy"]
