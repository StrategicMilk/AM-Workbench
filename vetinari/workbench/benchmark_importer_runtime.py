"""Runtime persistence for Workbench benchmark imports."""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from contextlib import suppress
from pathlib import Path

from vetinari.types import AgentType
from vetinari.workbench.assets import AssetKind, WorkbenchAsset
from vetinari.workbench.benchmark_importer import (
    _DEFAULT_CATALOG_PATH,
    BenchmarkCaseDraft,
    BenchmarkImportRecord,
    BenchmarkImportRefused,
    BenchmarkProviderConfig,
    LicenseClassification,
    PrivacyClassification,
    _utc_now_iso,
    load_benchmark_importer_catalog,
    validate_project_id,
)
from vetinari.workbench.data_assets import DataAsset, DataAssetKind
from vetinari.workbench.dataset_revision_records import DatasetRevision
from vetinari.workbench.dataset_revisions import (
    DatasetRevisionError,
    DatasetRevisionStore,
    get_dataset_revision_store,
)
from vetinari.workbench.evals import EvalResult, EvalScore
from vetinari.workbench.metadata_spine import WorkbenchSpine, WorkbenchSpineCorrupt, get_workbench_spine
from vetinari.workbench.runs import RunKind, RunStatus, WorkbenchRun

logger = logging.getLogger(__name__)


class BenchmarkImporter:
    """Fail-closed importer that writes through Workbench storage surfaces."""

    def __init__(
        self,
        *,
        catalog_path: Path | str = _DEFAULT_CATALOG_PATH,
        spine: WorkbenchSpine | None = None,
        revision_store: DatasetRevisionStore | None = None,
    ) -> None:
        self.catalog = load_benchmark_importer_catalog(catalog_path)
        self._spine = spine
        self._revision_store = revision_store

    @property
    def providers(self) -> dict[str, BenchmarkProviderConfig]:
        """Return loaded provider configs keyed by provider id."""
        return dict(self.catalog.providers)

    def import_case(
        self,
        provider_id: str,
        draft: BenchmarkCaseDraft,
        *,
        project_id: str = "default",
    ) -> BenchmarkImportRecord:
        """Validate and persist one benchmark import draft.

        Args:
            provider_id: Provider name or adapter selected for the operation.
            draft: Draft value consumed by import_case().
            project_id: Project identifier that scopes the operation.

        Returns:
            BenchmarkImportRecord value produced by import_case().

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        canonical_project_id = validate_project_id(project_id)
        config = self.catalog.providers.get(provider_id)
        if config is None:
            raise BenchmarkImportRefused(
                "unknown-provider-id",
                draft.source_uri,
                f"provider_id {provider_id!r} is not in the catalog; choose a catalog provider",
            )
        self._validate_draft(config, draft)
        spine = self._spine if self._spine is not None else get_workbench_spine()
        revision_store = self._revision_store if self._revision_store is not None else get_dataset_revision_store()
        return self._write_case(
            spine=spine,
            revision_store=revision_store,
            provider_id=provider_id,
            config=config,
            draft=draft,
            project_id=canonical_project_id,
        )

    def verify_import_record(self, record: BenchmarkImportRecord) -> None:
        """Verify a benchmark import record against the dataset revision digest.

        Args:
            record: Import record returned by ``import_case``.

        Raises:
            BenchmarkImportRefused: If the read-back digest is missing or mismatched.
        """
        revision_store = self._revision_store if self._revision_store is not None else get_dataset_revision_store()
        revisions = {revision.revision_id: revision for revision in revision_store.list_revisions()}
        revision = revisions.get(record.revision_id)
        if revision is None:
            raise BenchmarkImportRefused(
                "revision-not-found",
                record.asset.provenance.get("source_uri", record.asset.asset_id),
                "benchmark import revision is missing; cannot verify payload integrity",
            )
        expected_sha = record.asset.provenance.get("payload_sha256", "")
        if not expected_sha:
            raise BenchmarkImportRefused(
                "payload-sha256-missing",
                record.asset.provenance.get("source_uri", record.asset.asset_id),
                "benchmark import asset is missing payload_sha256 provenance",
            )
        matching_assets = [asset for asset in revision.assets if asset.content_sha256 == expected_sha]
        if not matching_assets:
            raise BenchmarkImportRefused(
                "payload-sha256-mismatch",
                record.asset.provenance.get("source_uri", record.asset.asset_id),
                "benchmark import payload_sha256 does not match any asset in the dataset revision",
            )
        revision_id = record.asset.provenance.get("dataset_revision_id", "")
        if revision_id != record.revision_id:
            raise BenchmarkImportRefused(
                "dataset-revision-mismatch",
                record.asset.provenance.get("source_uri", record.asset.asset_id),
                "benchmark import asset provenance points at a different dataset revision",
            )

    def _validate_draft(self, config: BenchmarkProviderConfig, draft: BenchmarkCaseDraft) -> None:
        source_kind_value = getattr(draft.source_kind, "value", draft.source_kind)
        license_value = getattr(draft.license_classification, "value", draft.license_classification)
        privacy_value = getattr(draft.privacy_classification, "value", draft.privacy_classification)
        eval_method_value = getattr(draft.allowed_eval_method, "value", draft.allowed_eval_method)
        if source_kind_value != config.kind.value:
            raise BenchmarkImportRefused(
                "provider-source-kind-mismatch",
                draft.source_uri,
                f"provider {config.provider_id!r} accepts {config.kind.value}, got {source_kind_value}",
            )
        if not draft.source_uri.strip():
            raise BenchmarkImportRefused("source-uri-missing", draft.source_uri, "source_uri is required")
        if license_value == LicenseClassification.UNKNOWN_BLOCKED.value:
            raise BenchmarkImportRefused(
                "license-classification-blocked",
                draft.source_uri,
                "license classification is unknown_blocked; classify the source before import",
            )
        if license_value not in {classification.value for classification in config.allowed_license_classifications}:
            raise BenchmarkImportRefused(
                "license-classification-blocked",
                draft.source_uri,
                "license classification is not allowed for this provider; choose a compatible provider",
            )
        if privacy_value == PrivacyClassification.PII_BLOCKED.value:
            raise BenchmarkImportRefused(
                "privacy-pii-blocked",
                draft.source_uri,
                "privacy classification is pii_blocked; redact or exclude the source before import",
            )
        if privacy_value not in {classification.value for classification in config.allowed_privacy_classifications}:
            raise BenchmarkImportRefused(
                "privacy-classification-blocked",
                draft.source_uri,
                "privacy classification is not allowed for this provider; choose a compatible provider",
            )
        if not draft.revision_pin.strip():
            raise BenchmarkImportRefused("revision-pin-missing", draft.source_uri, "revision_pin is required")
        if not draft.expected_output_schema.strip():
            raise BenchmarkImportRefused(
                "expected-output-schema-missing",
                draft.source_uri,
                "expected_output_schema is required",
            )
        if draft.expected_output_schema not in set(self.catalog.allowed_output_schemas):
            raise BenchmarkImportRefused(
                "expected-output-schema-unknown",
                draft.source_uri,
                "expected_output_schema is not declared in the importer catalog",
            )
        if eval_method_value not in {method.value for method in self.catalog.eval_methods}:
            raise BenchmarkImportRefused(
                "eval-method-unknown",
                draft.source_uri,
                "allowed_eval_method is not declared in the importer catalog",
            )
        if eval_method_value != config.default_eval_method.value:
            raise BenchmarkImportRefused(
                "eval-method-disallowed",
                draft.source_uri,
                "allowed_eval_method is not permitted for this provider; use the provider default method",
            )

    def _write_case(
        self,
        *,
        spine: WorkbenchSpine,
        revision_store: DatasetRevisionStore,
        provider_id: str,
        config: BenchmarkProviderConfig,
        draft: BenchmarkCaseDraft,
        project_id: str,
    ) -> BenchmarkImportRecord:
        now = _utc_now_iso()
        payload_json = json.dumps(draft.case_payload, sort_keys=True, separators=(",", ":"))
        payload_sha = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        data_asset = _benchmark_data_asset(provider_id, payload_sha, payload_json, now)
        revision = self._commit_revision(revision_store, data_asset, provider_id, draft)
        asset = _benchmark_asset(provider_id, config, draft, project_id, revision.revision_id, payload_sha, now)
        run = _benchmark_run(asset, project_id, now)
        eval_result = _benchmark_eval_result(draft, provider_id, asset, run.run_id, now)
        record = BenchmarkImportRecord(eval_result, asset, revision.revision_id, provider_id)
        try:
            self._append_benchmark_asset(spine, asset, draft)
            self._append_benchmark_run(spine, run, draft)
            self._append_benchmark_eval(spine, eval_result, draft)
            self.verify_import_record(record)
            logger.info("Imported benchmark case provider=%s source=%s", provider_id, draft.source_uri)
            return record
        except Exception:
            self._rollback_partial_import(spine, revision_store, record)
            raise

    @staticmethod
    def _commit_revision(
        revision_store: DatasetRevisionStore,
        data_asset: DataAsset,
        provider_id: str,
        draft: BenchmarkCaseDraft,
    ) -> DatasetRevision:
        try:
            return revision_store.commit(
                parent_revision_id=None,
                branch=f"benchmark-{data_asset.content_sha256[:12]}",
                assets=(data_asset,),
                message=f"benchmark import {provider_id} {draft.source_uri}",
                source_receipt_ids=(),
            )
        except (DatasetRevisionError, ValueError, OSError, RuntimeError) as exc:
            raise BenchmarkImportRefused(
                "revision-store-rejected",
                draft.source_uri,
                "dataset revision store rejected the import; no benchmark asset or eval row was written",
            ) from exc

    @staticmethod
    def _append_benchmark_asset(
        spine: WorkbenchSpine,
        asset: WorkbenchAsset,
        draft: BenchmarkCaseDraft,
    ) -> None:
        try:
            spine.append_asset(asset)
        except WorkbenchSpineCorrupt:
            raise
        except Exception as exc:
            raise BenchmarkImportRefused(
                "spine-asset-rejected",
                draft.source_uri,
                "spine rejected the benchmark asset after the dataset revision was committed; retry by source URI",
            ) from exc

    @staticmethod
    def _append_benchmark_run(
        spine: WorkbenchSpine,
        run: WorkbenchRun,
        draft: BenchmarkCaseDraft,
    ) -> None:
        try:
            spine.append_run(run)
        except WorkbenchSpineCorrupt:
            raise
        except Exception as exc:
            raise BenchmarkImportRefused(
                "spine-run-rejected",
                draft.source_uri,
                "spine rejected the synthetic eval run; the asset exists but no eval row was written",
            ) from exc

    @staticmethod
    def _append_benchmark_eval(
        spine: WorkbenchSpine,
        eval_result: EvalResult,
        draft: BenchmarkCaseDraft,
    ) -> None:
        try:
            spine.append_eval(eval_result)
        except WorkbenchSpineCorrupt:
            raise
        except Exception as exc:
            raise BenchmarkImportRefused(
                "spine-eval-rejected",
                draft.source_uri,
                "spine rejected the eval row; benchmark import state was rolled back",
            ) from exc

    @staticmethod
    def _rollback_partial_import(
        spine: WorkbenchSpine,
        revision_store: DatasetRevisionStore,
        record: BenchmarkImportRecord,
    ) -> None:
        with suppress(Exception):
            spine.delete_record("eval", record.eval_result.eval_id, reason="benchmark import rollback")
        with suppress(Exception):
            spine.delete_record("run", record.eval_result.run_id, reason="benchmark import rollback")
        with suppress(Exception):
            spine.delete_record("asset", record.asset.asset_id, reason="benchmark import rollback")
        with suppress(Exception):
            spine.delete_record("asset", record.revision_id, reason="benchmark import revision rollback")
        if hasattr(revision_store, "discard_revision_for_failed_import"):
            revision_store.discard_revision_for_failed_import(
                record.revision_id,
                expected_branch_prefix="benchmark-",
            )


def _benchmark_data_asset(provider_id: str, payload_sha: str, payload_json: str, now: str) -> DataAsset:
    return DataAsset(
        asset_path=f"benchmark/{provider_id}/{payload_sha}.json",
        kind=DataAssetKind.INLINE,
        content_sha256=payload_sha,
        size_bytes=len(payload_json.encode("utf-8")),
        mime_type="application/json",
        captured_at_utc=now,
    )


def _benchmark_asset(
    provider_id: str,
    config: BenchmarkProviderConfig,
    draft: BenchmarkCaseDraft,
    project_id: str,
    revision_id: str,
    payload_sha: str,
    now: str,
) -> WorkbenchAsset:
    asset_id = f"benchmark-case::{uuid.uuid5(uuid.NAMESPACE_URL, provider_id + draft.source_uri + draft.revision_pin)}"
    return WorkbenchAsset(
        asset_id=asset_id,
        kind=AssetKind.DATASET,
        name=f"benchmark/{provider_id}",
        revision=revision_id,
        created_at_utc=now,
        provenance={
            "source": draft.source_uri,
            "source_uri": draft.source_uri,
            "source_kind": draft.source_kind.value,
            "provider_id": provider_id,
            "project_id": project_id,
            "license_classification": draft.license_classification.value,
            "privacy_classification": draft.privacy_classification.value,
            "revision_pin": draft.revision_pin,
            "dataset_revision_id": revision_id,
            "expected_output_schema": draft.expected_output_schema,
            "allowed_eval_method": draft.allowed_eval_method.value,
            "provider_kind": config.kind.value,
            "payload_sha256": payload_sha,
        },
    )


def _benchmark_run(asset: WorkbenchAsset, project_id: str, now: str) -> WorkbenchRun:
    return WorkbenchRun(
        run_id=f"benchmark-import-run::{uuid.uuid5(uuid.NAMESPACE_URL, asset.asset_id + asset.revision)}",
        kind=RunKind.EVAL_RUN,
        status=RunStatus.SUCCEEDED,
        started_at_utc=now,
        finished_at_utc=now,
        actor_agent_type=AgentType.WORKBENCH,
        asset_revisions=((asset.asset_id, asset.revision),),
        lease_id="",
        shard_kind=None,
        metrics=(),
        outcome=None,
        project_id=project_id,
    )


def _benchmark_eval_result(
    draft: BenchmarkCaseDraft,
    provider_id: str,
    asset: WorkbenchAsset,
    run_id: str,
    now: str,
) -> EvalResult:
    return EvalResult(
        eval_id=f"benchmark-eval::{uuid.uuid5(uuid.NAMESPACE_URL, run_id + asset.asset_id)}",
        kind=draft.allowed_eval_method,
        run_id=run_id,
        asset_id=asset.asset_id,
        asset_revision=asset.revision,
        scores=(EvalScore(metric_name="unevaluated", value=0.0, threshold=1.0, passed=False),),
        captured_at_utc=now,
        notes=f"Imported benchmark case from {provider_id}; evaluation pending.",
    )
