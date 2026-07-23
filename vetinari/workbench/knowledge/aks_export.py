"""AKS-compatible portable knowledge bundles for Workbench projects.

The bundle types in this module are a typed export view. AKS is the export
target, not the authority; Workbench source cards, tool cards, context assets,
and semantic graph records remain the source of truth.
"""

from __future__ import annotations

import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from vetinari.workbench.knowledge.aks_bundle_records import (
    SCHEMA_VERSION,
    AKSBundle,
    AKSBundleAuthority,
    AKSBundleClaim,
    AKSBundleDecision,
    AKSBundleEntity,
    AKSBundleEvalResult,
    AKSBundleMemory,
    AKSBundleProvenance,
    AKSBundleRelationship,
    AKSBundleRunRecord,
    AKSBundleSource,
    AKSBundleWorkflowLesson,
    ClaimAttestation,
    ScopeBoundary,
    VerifiedFlag,
    _optional_scope,
    _provenance_from_payload,
)
from vetinari.workbench.knowledge.aks_bundle_support import (
    BundleAuthorityRefused,
    BundleExportError,
    _enum_or_value,
    _require_bool,
    _require_mapping,
    _require_str,
    _string_mapping,
)
from vetinari.workbench.source_cards import SourceCard, evaluate_freshness
from vetinari.workbench.tool_cards import ToolCard


@dataclass(frozen=True, slots=True)
class WorkbenchAksExporter:
    """Transform Workbench source-of-truth into AKS shape.

    AKS is the export target, not the internal authority. This class transforms
    Workbench source-of-truth into AKS shape; it never invents authority that
    Workbench does not already record.
    """

    @classmethod
    def assemble_bundle_from_workbench(
        cls,
        *,
        project_id: str,
        semantic_layer: Any,
        context_asset_packs: Iterable[Any],
        source_cards: Iterable[SourceCard],
        tool_cards: Iterable[ToolCard],
        run_records: Iterable[Mapping[str, Any] | AKSBundleRunRecord] | None = None,
        eval_results: Iterable[Mapping[str, Any] | AKSBundleEvalResult] | None = None,
        memories: Iterable[Mapping[str, Any] | AKSBundleMemory] | None = None,
        workflow_lessons: Iterable[Mapping[str, Any] | AKSBundleWorkflowLesson] | None = None,
        decisions: Iterable[Mapping[str, Any] | AKSBundleDecision] | None = None,
        authority: AKSBundleAuthority | None = None,
        claim_kinds: Mapping[str, Iterable[str]] | None = None,
        verified_flags: Mapping[str, VerifiedFlag | str] | None = None,
        caveats_acknowledged: bool = True,
        now_utc: datetime | None = None,
    ) -> AKSBundle:
        """Assemble one deterministic portable bundle from live Workbench inputs.

        Returns:
            AKSBundle value produced by assemble_bundle_from_workbench().
        """
        active_authority = authority or AKSBundleAuthority.strict_default()
        context_asset_pack_items = tuple(context_asset_packs)
        source_cards_by_id = {card.source_card_id: card for card in source_cards}
        run_record_items = tuple(_coerce_run_record(item) for item in (run_records or ()))
        entity_specs = tuple(_entity_spec_from_semantic(entity) for entity in tuple(semantic_layer.entities))
        relationship_specs = tuple(
            _relationship_from_semantic(relation) for relation in tuple(semantic_layer.relations)
        )
        source_items = _bundle_source_items(context_asset_pack_items, source_cards_by_id, now_utc=now_utc)
        claim_items = _bundle_claim_items(
            tool_cards,
            source_cards_by_id=source_cards_by_id,
            run_records=run_record_items,
            claim_kinds=claim_kinds,
            verified_flags=verified_flags or {},
            caveats_acknowledged=caveats_acknowledged,
            now_utc=now_utc,
        )
        memory_items = tuple(_coerce_memory(item) for item in (memories or ()))
        decision_items = tuple(_coerce_decision(item) for item in (decisions or ()))

        module = sys.modules[__name__]
        module._assert_authority_preserved(
            active_authority,
            entities=entity_specs,
            relationships=relationship_specs,
            sources=source_items,
            claims=claim_items,
            run_records=run_record_items,
            memories=memory_items,
            decisions=decision_items,
        )

        return AKSBundle(
            project_id=project_id,
            bundle_id=f"{project_id}:{getattr(semantic_layer, 'graph_id', 'semantic-layer')}",
            schema_version=SCHEMA_VERSION,
            exported_at_utc=_utc_now(now_utc),
            authority=active_authority,
            source_world_view={
                "semantic_layer": getattr(semantic_layer, "graph_id", ""),
                "context_asset_packs": str(len(context_asset_pack_items)),
                "source_cards": str(len(source_cards_by_id)),
            },
            entities=tuple(_entity_from_spec(spec) for spec in entity_specs),
            relationships=relationship_specs,
            sources=source_items,
            run_records=run_record_items,
            claims=claim_items,
            decisions=decision_items,
            eval_results=tuple(_coerce_eval_result(item) for item in (eval_results or ())),
            memories=memory_items,
            workflow_lessons=tuple(_coerce_workflow_lesson(item) for item in (workflow_lessons or ())),
        )

    @staticmethod
    def into_scratch_context(bundle: AKSBundle) -> dict[str, Any]:
        """Return an AKS scratch-context tree tagged as Workbench-derived."""
        payload = bundle.to_payload()
        claim_payloads = [claim for claim in payload["claims"] if claim["attestation"] == ClaimAttestation.CLAIM.value]
        observation_payloads = [
            claim for claim in payload["claims"] if claim["attestation"] == ClaimAttestation.OBSERVATION.value
        ]
        scratch_payload = dict(payload)
        scratch_payload["claims"] = claim_payloads
        scratch_payload["observations"] = observation_payloads
        return {"_workbench_export_only": True, "scratch_context": scratch_payload}

    @staticmethod
    def from_scratch_context(scratch: Mapping[str, Any]) -> AKSBundle:
        """Load a Workbench-derived scratch context back into a bundle."""
        if scratch.get("_workbench_export_only") is not True:
            raise BundleExportError("scratch context is not marked as Workbench export only")
        context = _require_mapping(scratch, "scratch_context")
        payload = dict(context)
        payload["claims"] = list(context.get("claims", ())) + list(context.get("observations", ()))
        payload.pop("observations", None)
        return AKSBundle.from_payload(payload)


def _bundle_source_items(
    context_asset_pack_items: tuple[Any, ...],
    source_cards_by_id: Mapping[str, SourceCard],
    *,
    now_utc: datetime | None,
) -> tuple[AKSBundleSource, ...]:
    return tuple(_source_from_context_pack(pack) for pack in context_asset_pack_items) + tuple(
        _source_from_source_card(card, now_utc=now_utc) for card in source_cards_by_id.values()
    )


def _bundle_claim_items(
    tool_cards: Iterable[ToolCard],
    *,
    source_cards_by_id: Mapping[str, SourceCard],
    run_records: tuple[AKSBundleRunRecord, ...],
    claim_kinds: Mapping[str, Iterable[str]] | None,
    verified_flags: Mapping[str, VerifiedFlag | str],
    caveats_acknowledged: bool,
    now_utc: datetime | None,
) -> tuple[AKSBundleClaim, ...]:
    return tuple(
        claim
        for tool in tool_cards
        for claim in _claims_from_tool_card(
            tool,
            source_cards_by_id=source_cards_by_id,
            run_records=run_records,
            claim_kinds=claim_kinds,
            verified_flags=verified_flags,
            caveats_acknowledged=caveats_acknowledged,
            now_utc=now_utc,
        )
    )


def _assert_authority_preserved(
    authority: AKSBundleAuthority,
    *,
    entities: Iterable[Any],
    relationships: Iterable[Any],
    sources: Iterable[Any],
    claims: Iterable[Any],
    run_records: Iterable[Any],
    memories: Iterable[Any],
    decisions: Iterable[Any],
) -> None:
    """Prove unsupported authority is not invented."""
    del relationships
    run_record_ids = {_field(record, "run_id") for record in run_records}
    if authority.must_preserve_source_traceability:
        for record in [*entities, *sources, *claims, *memories, *decisions]:
            if not _field(record, "provenance_refs"):
                raise BundleAuthorityRefused(
                    refused_field="source_traceability",
                    reason=f"{type(record).__name__} has no provenance_refs",
                )
    if authority.must_preserve_verified_flag:
        for claim in claims:
            if _field(claim, "verified_flag") == VerifiedFlag.VERIFIED:
                ref_types = {_field(ref, "ref_type") for ref in _field(claim, "provenance_refs", ())}
                if not ref_types & {"evidence_asset", "run_record"}:
                    raise BundleAuthorityRefused(
                        refused_field="verified_flag",
                        reason=f"claim {_field(claim, 'claim_id')} is verified without evidence provenance",
                    )
    if authority.must_preserve_scope:
        for entity in entities:
            if _field(entity, "scope") is None:
                raise BundleAuthorityRefused(
                    refused_field="scope",
                    reason=f"entity {_field(entity, 'entity_id')} has no scope evidence",
                )
        for memory in memories:
            if _field(memory, "scope") is None:
                raise BundleAuthorityRefused(
                    refused_field="scope",
                    reason=f"memory {_field(memory, 'memory_id')} has no scope evidence",
                )
    if authority.must_preserve_run_records:
        for claim in claims:
            if _field(claim, "attestation") == ClaimAttestation.CLAIM:
                ref_ids = {
                    _field(ref, "ref_id")
                    for ref in _field(claim, "provenance_refs", ())
                    if _field(ref, "ref_type") == "run_record"
                }
                if not ref_ids or not ref_ids <= run_record_ids:
                    raise BundleAuthorityRefused(
                        refused_field="run_records",
                        reason=f"claim {_field(claim, 'claim_id')} lacks backing run_record provenance",
                    )
    if authority.must_preserve_flow_steps:
        for decision in decisions:
            if not _field(decision, "flow_steps"):
                raise BundleAuthorityRefused(
                    refused_field="flow_steps",
                    reason=f"decision {_field(decision, 'decision_id')} has no flow_steps",
                )
    if authority.must_preserve_document_audit:
        for source in sources:
            if _field(source, "kind") == "document" and not _field(source, "document_audit"):
                raise BundleAuthorityRefused(
                    refused_field="document_audit",
                    reason=f"document source {_field(source, 'source_id')} has no document_audit",
                )


def _entity_spec_from_semantic(entity: Any) -> Mapping[str, Any]:
    properties = _string_mapping(getattr(entity, "properties", {}), "entity.properties")
    scope = _optional_scope(properties.get("scope"))
    return {
        "entity_id": entity.entity_id,
        "kind": _enum_or_value(entity.kind),
        "label": entity.label,
        "aliases": tuple(getattr(entity, "aliases", ())),
        "properties": properties,
        "verified_flag": VerifiedFlag(properties.get("verified_flag", VerifiedFlag.UNKNOWN.value)),
        "scope": scope,
        "provenance_refs": tuple(
            AKSBundleProvenance(
                ref_type="semantic_entity",
                ref_id=str(entity.entity_id),
                evidence=_field(ref, "evidence"),
            )
            for ref in tuple(getattr(entity, "provenance_refs", ()))
        ),
    }


def _entity_from_spec(spec: Mapping[str, Any]) -> AKSBundleEntity:
    return AKSBundleEntity(
        entity_id=str(spec["entity_id"]),
        kind=str(spec["kind"]),
        label=str(spec["label"]),
        aliases=tuple(str(alias) for alias in spec.get("aliases", ())),
        properties=_string_mapping(spec.get("properties", {}), "properties"),
        verified_flag=VerifiedFlag(_enum_or_value(spec.get("verified_flag", VerifiedFlag.UNKNOWN))),
        scope=spec.get("scope"),
        provenance_refs=tuple(spec.get("provenance_refs", ())),
    )


def _relationship_from_semantic(relation: Any) -> AKSBundleRelationship:
    relation_id = str(relation.relation_id)
    return AKSBundleRelationship(
        relationship_id=relation_id,
        kind=_enum_or_value(relation.kind),
        source_entity_id=str(relation.source_entity_id),
        target_entity_id=str(relation.target_entity_id),
        confidence=float(getattr(relation, "confidence", 1.0)),
        properties=_string_mapping(getattr(relation, "properties", {}), "relation.properties"),
        provenance_refs=tuple(
            AKSBundleProvenance(ref_type="semantic_relation", ref_id=relation_id, evidence=_field(ref, "evidence"))
            for ref in tuple(getattr(relation, "provenance_refs", ()))
        ),
    )


def _source_from_context_pack(pack: Any) -> AKSBundleSource:
    payload = pack.to_payload()
    metadata = _string_mapping(payload.get("metadata", {}), "metadata")
    kind = metadata.get("aks_source_kind", "context_asset_pack")
    document_audit = _extract_document_audit(metadata)
    return AKSBundleSource(
        source_id=str(payload["context_asset_id"]),
        kind=kind,
        name=str(payload["title"]),
        freshness=str(payload["freshness"]),
        prompt_safety_status=str(payload["prompt_safety_status"]),
        document_audit=document_audit,
        metadata={key: str(value) for key, value in payload.items() if key not in {"metadata", "source_coverage"}},
        provenance_refs=(
            AKSBundleProvenance(
                ref_type="context_asset_pack",
                ref_id=str(payload["context_asset_id"]),
                evidence=str(dict(getattr(pack, "provenance", ())).get("source", payload["context_asset_id"])),
            ),
        ),
    )


def _source_from_source_card(card: SourceCard, *, now_utc: datetime | None) -> AKSBundleSource:
    provenance = dict(card.provenance)
    verdict = evaluate_freshness(card, now_utc=now_utc)
    return AKSBundleSource(
        source_id=card.source_card_id,
        kind=provenance.get("aks_source_kind", card.kind.value),
        name=card.name,
        freshness="fresh" if verdict.passed else "stale",
        prompt_safety_status="safe",
        document_audit=_extract_document_audit(provenance),
        metadata={
            "freshness_reason": verdict.reason,
            "age_seconds": "" if verdict.age_seconds is None else str(verdict.age_seconds),
            "cite_required": str(card.cite_required),
        },
        provenance_refs=(
            AKSBundleProvenance(
                ref_type="source_card",
                ref_id=card.source_card_id,
                evidence=provenance.get("source", card.source_card_id),
            ),
        ),
    )


def _claims_from_tool_card(
    tool: ToolCard,
    *,
    source_cards_by_id: Mapping[str, SourceCard],
    run_records: tuple[AKSBundleRunRecord, ...],
    claim_kinds: Mapping[str, Iterable[str]] | None,
    verified_flags: Mapping[str, VerifiedFlag | str],
    caveats_acknowledged: bool,
    now_utc: datetime | None,
) -> tuple[AKSBundleClaim, ...]:
    sources = tuple(
        source_cards_by_id[source_id] for source_id in tool.source_card_ids if source_id in source_cards_by_id
    )
    kinds = (
        tuple(claim_kinds.get(tool.tool_card_id, ()))
        if claim_kinds
        else tuple(tool.claim_promotion_policy.permitted_claim_kinds or ("tool_output",))
    )
    claims: list[AKSBundleClaim] = []
    run_record_ref = run_records[0] if run_records else None
    for claim_kind in kinds:
        decision = tool.may_promote_to_claim(
            claim_kind=claim_kind,
            sources=sources,
            caveats_acknowledged=caveats_acknowledged,
            now_utc=now_utc,
        )
        if not decision.passed and any("caveats" in reason for reason in decision.rejection_reasons):
            continue
        attestation = ClaimAttestation.CLAIM if decision.passed else ClaimAttestation.OBSERVATION
        provenance_refs = [
            AKSBundleProvenance(
                ref_type="tool_card",
                ref_id=tool.tool_card_id,
                evidence=dict(tool.provenance).get("source", tool.tool_card_id),
            )
        ]
        provenance_refs.extend(
            AKSBundleProvenance(
                ref_type="source_card",
                ref_id=source.source_card_id,
                evidence=dict(source.provenance).get("source", source.source_card_id),
            )
            for source in sources
        )
        if attestation is ClaimAttestation.CLAIM and run_record_ref is not None:
            provenance_refs.append(
                AKSBundleProvenance(
                    ref_type="run_record",
                    ref_id=run_record_ref.run_id,
                    evidence=f"run_record:{run_record_ref.run_id}",
                )
            )
        claim_id = f"{tool.tool_card_id}:{claim_kind}"
        claims.append(
            AKSBundleClaim(
                claim_id=claim_id,
                claim_kind=claim_kind,
                statement=f"{tool.name} output supports {claim_kind}",
                attestation=attestation,
                verified_flag=VerifiedFlag(_enum_or_value(verified_flags.get(claim_id, VerifiedFlag.UNKNOWN))),
                metadata={"tool_kind": tool.kind.value},
                provenance_refs=tuple(provenance_refs),
            )
        )
    return tuple(claims)


def _coerce_run_record(value: Mapping[str, Any] | AKSBundleRunRecord) -> AKSBundleRunRecord:
    if isinstance(value, AKSBundleRunRecord):
        return value
    run_id = _require_str(value, "run_id")
    return AKSBundleRunRecord(
        run_id=run_id,
        summary=_require_str(value, "summary"),
        status=str(value.get("status", "unknown")),
        metadata=_string_mapping(value.get("metadata", {}), "metadata"),
        provenance_refs=_provenance_from_input(value, "run_record", run_id),
    )


def _coerce_eval_result(value: Mapping[str, Any] | AKSBundleEvalResult) -> AKSBundleEvalResult:
    if isinstance(value, AKSBundleEvalResult):
        return value
    eval_id = _require_str(value, "eval_id")
    passed = _require_bool(value.get("passed"), "passed")
    return AKSBundleEvalResult(
        eval_id=eval_id,
        summary=_require_str(value, "summary"),
        passed=passed,
        score=_score_from_eval_input(value, passed),
        evidence_refs=_evidence_refs_from_input(value, "eval_result", eval_id),
        metadata=_string_mapping(value.get("metadata", {}), "metadata"),
        provenance_refs=_provenance_from_input(value, "eval_result", eval_id),
    )


def _coerce_memory(value: Mapping[str, Any] | AKSBundleMemory) -> AKSBundleMemory:
    if isinstance(value, AKSBundleMemory):
        return value
    memory_id = _require_str(value, "memory_id")
    return AKSBundleMemory(
        memory_id=memory_id,
        summary=_require_str(value, "summary"),
        scope=_optional_scope(value.get("scope")),
        validation_state=str(value.get("validation_state", "verified")),
        evidence_refs=_evidence_refs_from_input(value, "memory_lineage", memory_id),
        metadata=_string_mapping(value.get("metadata", {}), "metadata"),
        provenance_refs=_provenance_from_input(value, "memory_lineage", memory_id),
    )


def _coerce_workflow_lesson(value: Mapping[str, Any] | AKSBundleWorkflowLesson) -> AKSBundleWorkflowLesson:
    if isinstance(value, AKSBundleWorkflowLesson):
        return value
    lesson_id = _require_str(value, "lesson_id")
    return AKSBundleWorkflowLesson(
        lesson_id=lesson_id,
        summary=_require_str(value, "summary"),
        metadata=_string_mapping(value.get("metadata", {}), "metadata"),
        provenance_refs=_provenance_from_input(value, "workflow_lesson", lesson_id),
    )


def _coerce_decision(value: Mapping[str, Any] | AKSBundleDecision) -> AKSBundleDecision:
    if isinstance(value, AKSBundleDecision):
        return value
    decision_id = _require_str(value, "decision_id")
    return AKSBundleDecision(
        decision_id=decision_id,
        summary=_require_str(value, "summary"),
        flow_steps=tuple(str(step) for step in value.get("flow_steps", ())),
        metadata=_string_mapping(value.get("metadata", {}), "metadata"),
        provenance_refs=_provenance_from_input(value, "decision", decision_id),
    )


def _provenance_from_input(
    value: Mapping[str, Any],
    default_ref_type: str,
    default_ref_id: str,
) -> tuple[AKSBundleProvenance, ...]:
    if "provenance_refs" in value:
        return _provenance_from_payload(value, "provenance_refs")
    return (
        AKSBundleProvenance(
            ref_type=default_ref_type,
            ref_id=default_ref_id,
            evidence=str(value.get("evidence", default_ref_id)),
        ),
    )


def _score_from_eval_input(value: Mapping[str, Any], passed: bool) -> float:
    if "score" not in value:
        return 1.0 if passed else 0.0
    try:
        score = float(value["score"])
    except (TypeError, ValueError) as exc:
        raise BundleExportError("eval_result.score must be numeric") from exc
    if not 0.0 <= score <= 1.0:
        raise BundleExportError("eval_result.score must be between 0 and 1")
    return score


def _evidence_refs_from_input(
    value: Mapping[str, Any],
    default_ref_type: str,
    default_ref_id: str,
) -> tuple[str, ...]:
    refs = value.get("evidence_refs")
    if refs is None:
        evidence = str(value.get("evidence", "")).strip()
        if not evidence:
            raise BundleExportError(f"{default_ref_type}.evidence_refs must be non-empty")
        return (evidence,)
    if isinstance(refs, (str, bytes)) or not isinstance(refs, Sequence):
        raise BundleExportError(f"{default_ref_type}.evidence_refs must be a sequence")
    cleaned = tuple(str(ref) for ref in refs if str(ref).strip())
    if not cleaned:
        raise BundleExportError(f"{default_ref_type}.evidence_refs must be non-empty")
    return cleaned


def _extract_document_audit(values: Mapping[str, str]) -> dict[str, str]:
    return {
        key.removeprefix("document_audit_"): value for key, value in values.items() if key.startswith("document_audit")
    }


def _field(record: Any, field_name: str, default: Any = None) -> Any:
    if isinstance(record, Mapping):
        return record.get(field_name, default)
    return getattr(record, field_name, default)


def _utc_now(now_utc: datetime | None) -> str:
    now = now_utc or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


__all__ = [
    "AKSBundle",
    "AKSBundleAuthority",
    "AKSBundleClaim",
    "AKSBundleDecision",
    "AKSBundleEntity",
    "AKSBundleEvalResult",
    "AKSBundleMemory",
    "AKSBundleProvenance",
    "AKSBundleRelationship",
    "AKSBundleRunRecord",
    "AKSBundleSource",
    "AKSBundleWorkflowLesson",
    "BundleAuthorityRefused",
    "BundleExportError",
    "ClaimAttestation",
    "ScopeBoundary",
    "VerifiedFlag",
    "WorkbenchAksExporter",
]
