"""Workbench context enrichment and unchanged-content elision."""

from __future__ import annotations

import hashlib
import logging
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from vetinari.security.path_confinement import PathConfinementError, confine_path
from vetinari.security.redaction import redact_repr
from vetinari.workbench.context_assets import ContextAssetPack, FreshnessState
from vetinari.workbench.feature_store import ContextRetrievalResult

logger = logging.getLogger(__name__)


class ContextEnrichmentError(ValueError):
    """Raised when a context enrichment request is malformed."""


class ContextEnrichmentStatus(str, Enum):
    """Typed outcome for context enrichment callers."""

    UNCHANGED = "unchanged"
    CHANGED = "changed"
    STALE = "stale"
    BLOCKED = "blocked"
    OPERATOR_REVIEW = "operator_review"
    RAW_READ_DEGRADED = "raw_read_degraded"


@dataclass(frozen=True, slots=True)
class ContextEnrichmentPolicy:
    """Runtime policy for unchanged elision and fail-closed reads."""

    enable_unchanged_elision: bool = True
    require_provenance: bool = True
    block_high_risk_without_diagnostics: bool = True
    raw_read_degraded_opt_in: bool = True
    require_diagnostics_for_changed_content: bool = True
    read_root: Path = field(default_factory=Path.cwd)

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"ContextEnrichmentPolicy(enable_unchanged_elision={self.enable_unchanged_elision!r}, require_provenance={self.require_provenance!r}, block_high_risk_without_diagnostics={self.block_high_risk_without_diagnostics!r})"


@dataclass(frozen=True, slots=True)
class ContextAssetMetadata:
    """Context asset metadata surfaced to operators and API clients."""

    context_asset_id: str
    freshness: str
    coverage_ratio: float
    usefulness_score: float
    provenance: dict[str, str]
    invalidation_reasons: tuple[str, ...] = ()

    @classmethod
    def from_pack(cls, pack: ContextAssetPack) -> ContextAssetMetadata:
        """Execute the from pack operation.

        Returns:
            ContextAssetMetadata value produced by from_pack().
        """
        coverage = 0.0
        if pack.source_coverage:
            coverage = sum(source.coverage_ratio for source in pack.source_coverage) / len(pack.source_coverage)
        return cls(
            context_asset_id=pack.context_asset_id,
            freshness=pack.freshness.value,
            coverage_ratio=round(coverage, 4),
            usefulness_score=pack.usefulness_score,
            provenance=dict(pack.provenance),
            invalidation_reasons=tuple(
                trigger.description for trigger in pack.invalidation_triggers if trigger.is_active
            ),
        )

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"ContextAssetMetadata(context_asset_id={self.context_asset_id!r}, freshness={self.freshness!r}, coverage_ratio={self.coverage_ratio!r})"


@dataclass(frozen=True, slots=True)
class FeatureContextMetadata:
    """Feature-store context metadata exposed without owning the feature store."""

    entity_id: str
    context_view_id: str
    project: str
    version: str
    source: str
    values: dict[str, Any]
    decisions: tuple[dict[str, Any], ...]
    rag_chunk_ids: tuple[str, ...]

    @classmethod
    def from_result(
        cls,
        result: ContextRetrievalResult,
        *,
        project: str = "default",
        version: str = "unknown",
        source: str = "feature_store",
    ) -> FeatureContextMetadata:
        return cls(
            entity_id=result.entity_id,
            context_view_id=result.context_view_id,
            project=project,
            version=version,
            source=source,
            values=dict(result.values),
            decisions=tuple(
                {
                    "feature_id": decision.feature_id,
                    "included": decision.included,
                    "reason": decision.reason,
                    "evidence_ref": decision.evidence_ref,
                }
                for decision in result.decisions
            ),
            rag_chunk_ids=tuple(result.rag_chunk_ids),
        )

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"FeatureContextMetadata(entity_id={self.entity_id!r}, context_view_id={self.context_view_id!r}, project={self.project!r})"


@dataclass(frozen=True, slots=True)
class UnchangedDigestResponse:
    """Compact response returned when content has not changed."""

    path: str
    digest: str
    provenance: dict[str, str]
    context_manifest: dict[str, Any]
    saved_body_bytes: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"UnchangedDigestResponse(path={self.path!r}, digest={self.digest!r}, provenance={self.provenance!r})"


@dataclass(frozen=True, slots=True)
class ContextEnrichmentRequest:
    """Request for enriched content or compact unchanged status."""

    path: str
    content: str | None = None
    prior_digest: str | None = None
    caller_digest_state: dict[str, str] | None = None
    mode: str = "read"
    high_risk_edit: bool = False
    allow_raw_read_degraded: bool = False
    diagnostics_available: bool = True
    provenance: dict[str, str] = field(default_factory=dict)
    context_asset_packs: tuple[ContextAssetPack, ...] = ()
    feature_context: ContextRetrievalResult | None = None
    feature_context_project: str = "default"
    feature_context_version: str = "unknown"
    dependency_hints: tuple[str, ...] = ()
    dependent_hints: tuple[str, ...] = ()
    symbols: tuple[str, ...] = ()
    references: tuple[str, ...] = ()

    @classmethod
    def from_mapping(cls, payload: dict[str, Any]) -> ContextEnrichmentRequest:
        """Execute the from mapping operation.

        Returns:
            ContextEnrichmentRequest value produced by from_mapping().

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        if not isinstance(payload, dict):
            raise ContextEnrichmentError("payload must be an object")
        return cls(
            path=str(payload.get("path", "")),
            content=payload.get("content") if payload.get("content") is None else str(payload.get("content")),
            prior_digest=payload.get("prior_digest")
            if payload.get("prior_digest") is None
            else str(payload.get("prior_digest")),
            caller_digest_state=payload.get("caller_digest_state"),
            mode=str(payload.get("mode", "read")),
            high_risk_edit=bool(payload.get("high_risk_edit")),
            allow_raw_read_degraded=bool(payload.get("allow_raw_read_degraded")),
            diagnostics_available=bool(payload.get("diagnostics_available", True)),
            provenance={str(key): str(value) for key, value in dict(payload.get("provenance", {})).items()},
            dependency_hints=tuple(str(item) for item in payload.get("dependency_hints", ())),
            dependent_hints=tuple(str(item) for item in payload.get("dependent_hints", ())),
            symbols=tuple(str(item) for item in payload.get("symbols", ())),
            references=tuple(str(item) for item in payload.get("references", ())),
        )

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return redact_repr(
            "ContextEnrichmentRequest",
            {"path": self.path, "content": self.content, "prior_digest": self.prior_digest},
        )


@dataclass(frozen=True, slots=True)
class ContextEnrichmentResult:
    """Enriched or fail-closed context response."""

    status: ContextEnrichmentStatus
    path: str
    digest: str
    provenance: dict[str, str]
    reasons: tuple[str, ...]
    fallback_policy: dict[str, Any]
    context_manifest: dict[str, Any]
    body: str | None = None
    unchanged: UnchangedDigestResponse | None = None
    context_assets: tuple[ContextAssetMetadata, ...] = ()
    feature_context: FeatureContextMetadata | None = None
    diagnostics: tuple[str, ...] = ()
    dependency_hints: tuple[str, ...] = ()
    dependent_hints: tuple[str, ...] = ()
    symbols: tuple[str, ...] = ()
    references: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """Execute the to dict operation.

        Returns:
            dict[str, Any] value produced by to_dict().
        """
        payload = _jsonable(asdict(self))
        payload["status"] = self.status.value
        return payload

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return redact_repr(
            "ContextEnrichmentResult",
            {"status": self.status, "path": self.path, "digest": self.digest},
        )


class ContextEnrichmentService:
    """Compose Workbench context and elide unchanged content by digest."""

    def __init__(self, policy: ContextEnrichmentPolicy | None = None) -> None:
        self.policy = policy or ContextEnrichmentPolicy()
        self._digest_ledger: dict[str, str] = {}

    @property
    def digest_ledger(self) -> dict[str, str]:
        """Return a copy of the instance-owned digest ledger."""
        return dict(self._digest_ledger)

    def enrich(self, request: ContextEnrichmentRequest) -> ContextEnrichmentResult:
        """Return enriched content, compact unchanged status, or fail-closed status.

        Returns:
            ContextEnrichmentResult value produced by enrich().
        """
        fallback_policy = {
            "allow_raw_read_degraded": request.allow_raw_read_degraded,
            "high_risk_edit": request.high_risk_edit,
            "block_high_risk_without_diagnostics": self.policy.block_high_risk_without_diagnostics,
        }
        try:
            content = self._resolve_content(request)
        except ContextEnrichmentError:
            logger.warning("Context enrichment raw read failed confinement.", exc_info=True)
            return self._result(
                ContextEnrichmentStatus.BLOCKED,
                request,
                "",
                dict(request.provenance),
                ["raw_read_outside_confinement"],
                fallback_policy,
                None,
                (),
                None,
            )
        digest = _sha256(content)
        prior_digest = self._prior_digest(request)
        provenance = dict(request.provenance)
        assets = tuple(ContextAssetMetadata.from_pack(pack) for pack in request.context_asset_packs)
        feature_context = (
            FeatureContextMetadata.from_result(
                request.feature_context,
                project=request.feature_context_project,
                version=request.feature_context_version,
            )
            if request.feature_context is not None
            else None
        )
        reasons = self._status_reasons(request, provenance, assets, feature_context)

        terminal_status = self._fail_closed_status(request, reasons)
        if terminal_status is not None:
            return self._result(
                terminal_status,
                request,
                digest,
                provenance,
                reasons,
                fallback_policy,
                content if terminal_status is ContextEnrichmentStatus.RAW_READ_DEGRADED else None,
                assets,
                feature_context,
            )

        if (
            self.policy.enable_unchanged_elision
            and prior_digest == digest
            and provenance
            and not any(reason.startswith("stale_") for reason in reasons)
        ):
            self._digest_ledger[request.path] = digest
            return _unchanged_result(
                request, digest, provenance, reasons, fallback_policy, content, assets, feature_context
            )

        self._digest_ledger[request.path] = digest
        diagnostics = self._diagnostics(request, prior_digest=prior_digest, digest=digest)
        dependency_hints = request.dependency_hints or (f"dependency:{Path(request.path).name}",)
        dependent_hints = request.dependent_hints or (f"dependent:{Path(request.path).stem}",)
        status = (
            ContextEnrichmentStatus.STALE
            if any(reason.startswith("stale_") for reason in reasons)
            else ContextEnrichmentStatus.CHANGED
        )
        return _changed_result(
            request,
            status,
            digest,
            provenance,
            reasons,
            fallback_policy,
            content,
            assets,
            feature_context,
            diagnostics,
            tuple(dependency_hints),
            tuple(dependent_hints),
        )

    def _resolve_content(self, request: ContextEnrichmentRequest) -> str:
        if not request.path.strip():
            raise ContextEnrichmentError("path must be non-empty")
        if request.content is not None:
            return request.content
        try:
            return confine_path(self.policy.read_root, request.path).read_text(encoding="utf-8")
        except (OSError, PathConfinementError) as exc:
            raise ContextEnrichmentError("unreadable_file") from exc

    def _prior_digest(self, request: ContextEnrichmentRequest) -> str | None:
        if request.caller_digest_state is not None and not isinstance(request.caller_digest_state, dict):
            return None
        if request.prior_digest:
            return request.prior_digest
        if request.caller_digest_state:
            prior = request.caller_digest_state.get(request.path)
            return prior if isinstance(prior, str) else None
        return self._digest_ledger.get(request.path)

    def _status_reasons(
        self,
        request: ContextEnrichmentRequest,
        provenance: dict[str, str],
        assets: tuple[ContextAssetMetadata, ...],
        feature_context: FeatureContextMetadata | None,
    ) -> list[str]:
        reasons: list[str] = []
        if self.policy.require_provenance and not provenance:
            reasons.append("unknown_provenance")
        if request.caller_digest_state is not None and not isinstance(request.caller_digest_state, dict):
            reasons.append("corrupt_digest_state")
        if not request.diagnostics_available:
            reasons.append("diagnostics_unavailable")
        reasons.extend(
            f"stale_context_asset:{asset.context_asset_id}"
            for asset in assets
            if asset.freshness in {FreshnessState.STALE.value, FreshnessState.UNKNOWN.value}
        )
        if feature_context is None:
            reasons.append("feature_context_unavailable")
        else:
            reasons.extend(
                f"stale_feature_context:{decision.get('feature_id')}:{decision.get('reason')}"
                for decision in feature_context.decisions
                if not decision.get("included", False)
            )
        return reasons

    def _fail_closed_status(
        self,
        request: ContextEnrichmentRequest,
        reasons: list[str],
    ) -> ContextEnrichmentStatus | None:
        if "corrupt_digest_state" in reasons:
            return ContextEnrichmentStatus.BLOCKED
        if "unknown_provenance" in reasons and not request.allow_raw_read_degraded:
            return ContextEnrichmentStatus.BLOCKED
        if "diagnostics_unavailable" in reasons:
            if request.high_risk_edit and self.policy.block_high_risk_without_diagnostics:
                return (
                    ContextEnrichmentStatus.OPERATOR_REVIEW
                    if request.allow_raw_read_degraded
                    else ContextEnrichmentStatus.BLOCKED
                )
            if request.allow_raw_read_degraded:
                return ContextEnrichmentStatus.RAW_READ_DEGRADED
            return ContextEnrichmentStatus.BLOCKED
        return None

    @staticmethod
    def _diagnostics(request: ContextEnrichmentRequest, *, prior_digest: str | None, digest: str) -> tuple[str, ...]:
        diagnostics = (
            ["content_digest_changed"] if prior_digest and prior_digest != digest else ["content_digest_captured"]
        )
        if request.high_risk_edit:
            diagnostics.append("high_risk_edit_preflight")
        return tuple(diagnostics)

    def _result(
        self,
        status: ContextEnrichmentStatus,
        request: ContextEnrichmentRequest,
        digest: str,
        provenance: dict[str, str],
        reasons: list[str],
        fallback_policy: dict[str, Any],
        body: str | None,
        assets: tuple[ContextAssetMetadata, ...],
        feature_context: FeatureContextMetadata | None,
    ) -> ContextEnrichmentResult:
        return ContextEnrichmentResult(
            status=status,
            path=request.path,
            digest=digest,
            provenance=provenance,
            reasons=tuple(reasons),
            fallback_policy=fallback_policy,
            context_manifest=_context_manifest(assets, feature_context),
            body=body,
            context_assets=assets,
            feature_context=feature_context,
        )


def _unchanged_result(
    request: ContextEnrichmentRequest,
    digest: str,
    provenance: dict[str, str],
    reasons: list[str],
    fallback_policy: dict[str, Any],
    content: str,
    assets: tuple[ContextAssetMetadata, ...],
    feature_context: FeatureContextMetadata | None,
) -> ContextEnrichmentResult:
    manifest = _context_manifest(assets, feature_context)
    unchanged = UnchangedDigestResponse(
        path=request.path,
        digest=digest,
        provenance=provenance,
        context_manifest=manifest,
        saved_body_bytes=len(content.encode("utf-8")),
    )
    return ContextEnrichmentResult(
        status=ContextEnrichmentStatus.UNCHANGED,
        path=request.path,
        digest=digest,
        provenance=provenance,
        reasons=tuple(reasons),
        fallback_policy=fallback_policy,
        context_manifest=manifest,
        body=None,
        unchanged=unchanged,
        context_assets=assets,
        feature_context=feature_context,
    )


def _changed_result(
    request: ContextEnrichmentRequest,
    status: ContextEnrichmentStatus,
    digest: str,
    provenance: dict[str, str],
    reasons: list[str],
    fallback_policy: dict[str, Any],
    content: str,
    assets: tuple[ContextAssetMetadata, ...],
    feature_context: FeatureContextMetadata | None,
    diagnostics: tuple[str, ...],
    dependency_hints: tuple[str, ...],
    dependent_hints: tuple[str, ...],
) -> ContextEnrichmentResult:
    return ContextEnrichmentResult(
        status=status,
        path=request.path,
        digest=digest,
        provenance=provenance,
        reasons=tuple(reasons),
        fallback_policy=fallback_policy,
        context_manifest=_context_manifest(assets, feature_context),
        body=content,
        context_assets=assets,
        feature_context=feature_context,
        diagnostics=diagnostics,
        dependency_hints=dependency_hints,
        dependent_hints=dependent_hints,
        symbols=tuple(request.symbols),
        references=tuple(request.references),
    )


def _sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _jsonable(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    return value


def _context_manifest(
    assets: tuple[ContextAssetMetadata, ...],
    feature_context: FeatureContextMetadata | None,
) -> dict[str, Any]:
    return {
        "context_asset_ids": [asset.context_asset_id for asset in assets],
        "context_asset_freshness": {asset.context_asset_id: asset.freshness for asset in assets},
        "feature_context_view_id": feature_context.context_view_id if feature_context else None,
        "feature_project": feature_context.project if feature_context else None,
        "feature_version": feature_context.version if feature_context else None,
    }


__all__ = [
    "ContextAssetMetadata",
    "ContextEnrichmentError",
    "ContextEnrichmentPolicy",
    "ContextEnrichmentRequest",
    "ContextEnrichmentResult",
    "ContextEnrichmentService",
    "ContextEnrichmentStatus",
    "FeatureContextMetadata",
    "UnchangedDigestResponse",
]
