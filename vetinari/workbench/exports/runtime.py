"""Deterministic compliance/evidence export packages for Workbench cards."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any

from vetinari.security.redaction import redact_route_payload
from vetinari.workbench.cards import WorkbenchCard
from vetinari.workbench.collaboration import CollaborationAuditView, CollaborationBoard, WorkbenchProject


class ExportGenerationError(RuntimeError):
    """Raised when an export would be empty, untrusted, or malformed."""


@dataclass(frozen=True, slots=True)
class ComplianceEvidenceExport:
    """A redacted export package that can be shared for compliance review."""

    schema_version: int
    export_id: str
    project_id: str
    generated_at_utc: str
    cards: tuple[dict[str, Any], ...]
    collaboration: CollaborationAuditView | Mapping[str, Any] | None
    templates: tuple[tuple[str, str], ...]
    blocked_reasons: tuple[str, ...]
    manifest_hash_sha256: str
    shareable: bool

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ExportGenerationError("export schema_version must be 1")
        for field_name in ("export_id", "project_id", "generated_at_utc", "manifest_hash_sha256"):
            _require_non_empty(getattr(self, field_name), field_name)
        if not self.cards:
            raise ExportGenerationError("export requires at least one card")
        if len(self.manifest_hash_sha256) != 64:
            raise ExportGenerationError("manifest_hash_sha256 must be a SHA-256 hex digest")

    def to_dict(self) -> dict[str, Any]:
        """Return a redacted JSON-serializable export payload."""
        return {
            "schema_version": self.schema_version,
            "export_id": self.export_id,
            "project_id": self.project_id,
            "generated_at_utc": self.generated_at_utc,
            "cards": list(self.cards),
            "collaboration": _plain_collaboration(self.collaboration),
            "templates": [list(row) for row in self.templates],
            "blocked_reasons": list(self.blocked_reasons),
            "manifest_hash_sha256": self.manifest_hash_sha256,
            "shareable": self.shareable,
        }

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"ComplianceEvidenceExport(schema_version={self.schema_version!r}, export_id={self.export_id!r}, project_id={self.project_id!r})"


class WorkbenchCardExportService:
    """Build deterministic export packages from generated Workbench cards."""

    def build_export(
        self,
        *,
        export_id: str,
        project: WorkbenchProject,
        cards: Sequence[WorkbenchCard],
        generated_at_utc: str,
    ) -> ComplianceEvidenceExport:
        """Build a compliance/evidence export package from explicit inputs.

        Returns:
            Newly constructed export value.

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        _require_non_empty(export_id, "export_id")
        _require_non_empty(generated_at_utc, "generated_at_utc")
        if not cards:
            raise ExportGenerationError("cannot export without cards")
        collaboration = CollaborationBoard().audit_view(project)
        card_payloads = tuple(card.to_dict() for card in sorted(cards, key=lambda row: row.card_id))
        blocked_reasons = _blocked_reasons(cards, collaboration)
        templates = _templates(cards)
        payload = {
            "schema_version": 1,
            "export_id": export_id,
            "project_id": project.project_id,
            "generated_at_utc": generated_at_utc,
            "cards": card_payloads,
            "collaboration": asdict(collaboration),
            "templates": templates,
            "blocked_reasons": blocked_reasons,
        }
        redacted = redact_route_payload(payload)
        if not isinstance(redacted, dict):
            raise ExportGenerationError("redaction returned unexpected export shape")
        return ComplianceEvidenceExport(
            schema_version=1,
            export_id=export_id,
            project_id=project.project_id,
            generated_at_utc=generated_at_utc,
            cards=tuple(dict(card) for card in redacted.get("cards", ())),
            collaboration=redacted.get("collaboration") if isinstance(redacted.get("collaboration"), Mapping) else None,
            templates=tuple(tuple(row) for row in redacted.get("templates", ())),
            blocked_reasons=tuple(str(reason) for reason in redacted.get("blocked_reasons", ())),
            manifest_hash_sha256=seal_export_payload(redacted),
            shareable=not blocked_reasons,
        )


def seal_export_payload(payload: dict[str, Any]) -> str:
    """Return a deterministic SHA-256 hash for a redacted export payload.

    Returns:
        str value produced by seal_export_payload().
    """
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _templates(cards: Sequence[WorkbenchCard]) -> tuple[tuple[str, str], ...]:
    kinds = {card.kind.value for card in cards}
    rows = [
        (kind, "include purpose, provenance, evidence, governance, risk, and collaboration fields") for kind in kinds
    ]
    rows.append(("compliance_evidence_export", "include cards, approval history, queues, blockers, and manifest hash"))
    return tuple(sorted(rows))


def _blocked_reasons(cards: Sequence[WorkbenchCard], collaboration: CollaborationAuditView) -> tuple[str, ...]:
    reasons = [f"card {card.card_id} degraded: {card.risk_posture}" for card in cards if card.degraded]
    if collaboration.blocked_review_count:
        reasons.append(f"{collaboration.blocked_review_count} blocked review assignment(s)")
    if collaboration.pending_review_count:
        reasons.append(f"{collaboration.pending_review_count} pending review assignment(s)")
    return tuple(sorted(reasons))


def _plain_collaboration(collaboration: CollaborationAuditView | Mapping[str, Any] | None) -> dict[str, Any] | None:
    if collaboration is None:
        return None
    if isinstance(collaboration, Mapping):
        return dict(collaboration)
    return asdict(collaboration)


def _require_non_empty(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ExportGenerationError(f"{field_name} must be non-empty")
