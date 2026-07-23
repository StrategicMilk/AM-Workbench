"""Export Workbench evidence into local tamper-evident AI bundles."""

from __future__ import annotations

import json
from dataclasses import dataclass, fields, is_dataclass
from enum import Enum
from itertools import starmap
from pathlib import Path
from typing import Any

from vetinari.workbench.dataset_revisions import DatasetRevisionStore, get_dataset_revision_store
from vetinari.workbench.evidence_assets import EvidenceAssetLibrary
from vetinari.workbench.packaging.manifest import (
    AIBundleComponent,
    AIBundleComponentKind,
    AIBundleKind,
    AIBundleManifest,
    BundleIntegrityError,
    raw_sha256_digest,
)
from vetinari.workbench.packaging.oci import write_oci_layout
from vetinari.workbench.repro_capsules import ReproCapsuleService


class PackagingBundleExportError(Exception):
    """Raised when a trusted AI bundle cannot be exported."""


@dataclass(frozen=True, slots=True)
class BundleExportRequest:
    """Inputs for one local AI bundle export."""

    project_id: str
    bundle_id: str
    run_id: str
    dataset_revision_id: str
    asset_card_ids: tuple[str, ...]
    destination: Path

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return (
            f"BundleExportRequest(project_id={self.project_id!r}, bundle_id={self.bundle_id!r}, run_id={self.run_id!r})"
        )


@dataclass(frozen=True, slots=True)
class BundleExportResult:
    """Result of a successful local AI bundle export."""

    bundle_dir: Path
    manifest: AIBundleManifest


class AIBundleExporter:
    """Collect real Workbench evidence and write a local OCI-like bundle."""

    def __init__(
        self,
        *,
        repro_capsules: ReproCapsuleService | None = None,
        evidence_assets: EvidenceAssetLibrary | None = None,
        dataset_revisions: DatasetRevisionStore | None = None,
    ) -> None:
        self._repro_capsules = repro_capsules if repro_capsules is not None else ReproCapsuleService()
        self._evidence_assets = evidence_assets if evidence_assets is not None else EvidenceAssetLibrary()
        self._dataset_revisions = dataset_revisions if dataset_revisions is not None else get_dataset_revision_store()

    def export_bundle(self, request: BundleExportRequest) -> BundleExportResult:
        """Export a complete bundle or fail before writing a trusted layout.

        Returns:
            BundleExportResult value produced by export_bundle().

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        try:
            capsule = self._repro_capsules.build_capsule(project_id=request.project_id, run_id=request.run_id)
            cards = self._read_asset_cards(request)
            revision = self._read_dataset_revision(request)
            blobs = self._build_component_blobs(capsule, cards, revision)
            components = tuple(starmap(_component_from_blob, blobs))
            manifest = AIBundleManifest(
                schema_version=1,
                bundle_id=request.bundle_id,
                project_id=request.project_id,
                kind=AIBundleKind.OCI_BUNDLE,
                components=components,
                dependency_refs=(
                    f"repro-capsule:{capsule.manifest.capsule_id}",
                    f"dataset-revision:{revision.revision_id}",
                    *(f"asset-card:{card.asset_card_id}@{card.revision}" for card in cards),
                ),
                provenance_refs=(
                    "vetinari.workbench.repro_capsules.ReproCapsuleService.build_capsule",
                    "vetinari.workbench.evidence_assets.EvidenceAssetLibrary.get_card",
                    "vetinari.workbench.dataset_revisions.DatasetRevisionStore.list_revisions",
                ),
                runtime_facts={
                    "run_id": request.run_id,
                    "dataset_revision_id": revision.revision_id,
                    "external_packaging_authority": "none",
                    "layout_writer": "pure-python-local-oci-descriptor",
                },
                selective_unpack={"root": "components"},
            )
            component_blobs = {name: blob for _kind, name, blob, _source, _path in blobs}
            persisted_manifest = write_oci_layout(
                manifest=manifest,
                blobs=component_blobs,
                destination=request.destination,
            )
            return BundleExportResult(bundle_dir=request.destination.resolve(), manifest=persisted_manifest)
        except PackagingBundleExportError:
            raise
        except (BundleIntegrityError, OSError, ValueError, TypeError) as exc:
            raise PackagingBundleExportError(str(exc)) from exc

    def _read_asset_cards(self, request: BundleExportRequest) -> tuple[Any, ...]:
        if not request.asset_card_ids:
            raise PackagingBundleExportError("asset_card_ids are required for bundle export")
        cards: list[Any] = []
        for card_id in request.asset_card_ids:
            card = self._evidence_assets.get_card(project_id=request.project_id, asset_card_id=card_id)
            if card is None:
                raise PackagingBundleExportError(f"evidence asset card {card_id!r} not found")
            cards.append(card)
        return tuple(cards)

    def _read_dataset_revision(self, request: BundleExportRequest) -> Any:
        store_project_id = _dataset_store_project_id(self._dataset_revisions)
        if store_project_id is not None and store_project_id != request.project_id:
            raise PackagingBundleExportError(
                f"dataset revision store project {store_project_id!r} does not match request project {request.project_id!r}"
            )
        for revision in self._dataset_revisions.list_revisions():
            if revision.revision_id == request.dataset_revision_id:
                return revision
        raise PackagingBundleExportError(f"dataset revision {request.dataset_revision_id!r} not found")

    @staticmethod
    def _build_component_blobs(
        capsule: Any,
        cards: tuple[Any, ...],
        revision: Any,
    ) -> list[tuple[AIBundleComponentKind, str, bytes, str, str]]:
        rows: list[tuple[AIBundleComponentKind, str, bytes, str, str]] = []
        seen_kinds: set[AIBundleComponentKind] = set()
        for card in cards:
            kind = _component_kind_for_card(card)
            blob = _json_bytes({"evidence_asset_card": _to_jsonable(card)})
            rows.append((
                kind,
                f"{kind.value}-{card.asset_card_id}",
                blob,
                "evidence_asset_card",
                f"components/{kind.value}/{card.asset_card_id}.json",
            ))
            seen_kinds.add(kind)
        dataset_blob = _json_bytes({"dataset_revision": _to_jsonable(revision)})
        rows.append((
            AIBundleComponentKind.DATASET_SNAPSHOT,
            f"dataset-snapshot-{revision.revision_id}",
            dataset_blob,
            "dataset_revision",
            f"components/dataset_snapshot/{revision.revision_id}.json",
        ))
        seen_kinds.add(AIBundleComponentKind.DATASET_SNAPSHOT)
        capsule_blob = _json_bytes({"repro_capsule": capsule.to_redacted_dict()})
        rows.append((
            AIBundleComponentKind.RUNTIME_FACT,
            f"runtime-fact-{capsule.manifest.run_id}",
            capsule_blob,
            "repro_capsule",
            f"components/runtime_fact/{capsule.manifest.run_id}.json",
        ))
        seen_kinds.add(AIBundleComponentKind.RUNTIME_FACT)
        manifest_record_blob = _json_bytes({
            "manifest_record": {
                "capsule_id": capsule.manifest.capsule_id,
                "capsule_manifest_hash_sha256": capsule.manifest_hash_sha256,
                "dataset_revision_id": revision.revision_id,
                "asset_card_count": len(cards),
            }
        })
        rows.append((
            AIBundleComponentKind.MANIFEST_RECORD,
            f"manifest-record-{capsule.manifest.capsule_id}",
            manifest_record_blob,
            "bundle_manifest_record",
            f"components/manifest_record/{capsule.manifest.capsule_id}.json",
        ))
        seen_kinds.add(AIBundleComponentKind.MANIFEST_RECORD)
        missing = sorted(AIBundleComponentKind.required_values() - {kind.value for kind in seen_kinds})
        if missing:
            raise PackagingBundleExportError(f"missing required AI bundle component evidence: {', '.join(missing)}")
        return rows


def _component_kind_for_card(card: Any) -> AIBundleComponentKind:
    provenance = dict(getattr(card, "provenance", ()))
    explicit = provenance.get("ai_bundle_component_kind") or provenance.get("bundle_component_kind")
    if explicit:
        return AIBundleComponentKind(explicit)
    kind_value = getattr(getattr(card, "kind", None), "value", None)
    mapping = {
        "model_route": AIBundleComponentKind.MODEL,
        "dataset": AIBundleComponentKind.DATASET_SNAPSHOT,
        "eval_suite": AIBundleComponentKind.EVAL_SUITE,
        "prompt": AIBundleComponentKind.PROMPT,
        "policy": AIBundleComponentKind.POLICY,
        "runtime_profile": AIBundleComponentKind.RUNTIME_FACT,
    }
    if kind_value in mapping:
        return mapping[kind_value]
    raise PackagingBundleExportError(f"asset card {getattr(card, 'asset_card_id', '<unknown>')!r} lacks AI bundle kind")


def _component_from_blob(
    kind: AIBundleComponentKind,
    name: str,
    blob: bytes,
    source: str,
    unpack_path: str,
) -> AIBundleComponent:
    digest = raw_sha256_digest(blob)
    return AIBundleComponent(
        name=name,
        kind=kind,
        media_type="application/json",
        digest=digest,
        size_bytes=len(blob),
        blob_path=f"blobs/sha256/{digest.removeprefix('sha256:')}",
        source=source,
        unpack_path=unpack_path,
        metadata={"sha256": digest.removeprefix("sha256:")},
    )


def _json_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")


def _to_jsonable(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return {field.name: _to_jsonable(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, tuple):
        return [_to_jsonable(item) for item in value]
    if isinstance(value, list):
        return [_to_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _to_jsonable(item) for key, item in value.items()}
    if isinstance(value, Path):
        return str(value)
    return value


def _dataset_store_project_id(store: Any) -> str | None:
    direct = getattr(store, "_project_id", None)
    if isinstance(direct, str) and direct:
        return direct
    nested = getattr(getattr(store, "_store", None), "_project_id", None)
    if isinstance(nested, str) and nested:
        return nested
    return None


__all__ = [
    "AIBundleExporter",
    "BundleExportRequest",
    "BundleExportResult",
    "PackagingBundleExportError",
]
