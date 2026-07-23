"""Evidence-disciplined AM Workbench domain-kit catalog.

Domain kits are curated workflow bundles over existing Workbench surfaces:
capability packs, source/tool card kinds, benchmark providers, prompts,
examples, eval fixtures, rate limits, refusal boundaries, and sample notebook
references. The loader is fail-closed and performs no import-time I/O.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

from vetinari.constants import PROJECT_ROOT
from vetinari.workbench.benchmark_importer import BenchmarkImportRefused, load_benchmark_importer_catalog
from vetinari.workbench.capability_packs import CapabilityPackError, load_capability_pack_catalog
from vetinari.workbench.source_cards import SourceCardLibraryError, SourceKind, load_source_policies
from vetinari.workbench.tool_cards import ToolKind

logger = logging.getLogger(__name__)


_CATALOG_SCHEMA_VERSION = 1
_CATALOG_DIR = PROJECT_ROOT / "config" / "workbench" / "domain_kits"
_REQUIRED_LIST_FIELDS = (
    "supported_workflows",
    "supported_claim_kinds",
    "capability_pack_ids",
    "source_kinds",
    "tool_kinds",
    "examples",
    "eval_fixtures",
    "refusal_boundaries",
    "sample_notebook_refs",
    "benchmark_provider_ids",
    "required_caveat_acknowledgements",
)
_REQUIRED_STRING_FIELDS = ("kit_id", "title", "domain")

_DOMAIN_KIT_CATALOG_LOCK = threading.Lock()
_DOMAIN_KIT_CATALOG_CACHE: tuple[DomainKit, ...] | None = None


class DomainKitError(RuntimeError):
    """Fail-closed error raised when a domain-kit catalog cannot be trusted."""


@dataclass(frozen=True, slots=True)
class DomainKit:
    """Immutable catalog row for one curated Workbench workflow bundle."""

    kit_id: str
    title: str
    domain: str
    supported_workflows: tuple[str, ...]
    supported_claim_kinds: tuple[str, ...]
    unsupported_claims: tuple[str, ...]
    capability_pack_ids: tuple[str, ...]
    source_kinds: tuple[str, ...]
    tool_kinds: tuple[str, ...]
    prompt_templates: Mapping[str, str]
    examples: tuple[str, ...]
    eval_fixtures: tuple[str, ...]
    rate_limit_policy: Mapping[str, int | str]
    refusal_boundaries: tuple[str, ...]
    sample_notebook_refs: tuple[str, ...]
    benchmark_provider_ids: tuple[str, ...]
    required_caveat_acknowledgements: tuple[str, ...]
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable representation.

        Returns:
            dict[str, Any] value produced by to_dict().
        """
        row = asdict(self)
        row["prompt_templates"] = dict(self.prompt_templates)
        row["rate_limit_policy"] = dict(self.rate_limit_policy)
        row["metadata"] = dict(self.metadata)
        return row

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"DomainKit(kit_id={self.kit_id!r}, title={self.title!r}, domain={self.domain!r})"


@dataclass(frozen=True, slots=True)
class DomainKitSupportVerdict:
    """Support or refusal decision for a requested workflow and claim kind."""

    kit_id: str
    supported: bool
    status: str
    requested_workflow: str
    requested_claim_kind: str
    reasons: tuple[str, ...]
    required_caveat_acknowledgements: tuple[str, ...] = ()
    missing_caveat_acknowledgements: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable representation."""
        return {
            "kit_id": self.kit_id,
            "supported": self.supported,
            "status": self.status,
            "requested_workflow": self.requested_workflow,
            "requested_claim_kind": self.requested_claim_kind,
            "reasons": list(self.reasons),
            "required_caveat_acknowledgements": list(self.required_caveat_acknowledgements),
            "missing_caveat_acknowledgements": list(self.missing_caveat_acknowledgements),
        }

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"DomainKitSupportVerdict(kit_id={self.kit_id!r}, supported={self.supported!r}, status={self.status!r})"


@dataclass(frozen=True, slots=True)
class _UpstreamCatalogs:
    capability_pack_ids: frozenset[str]
    source_kind_ids: frozenset[str]
    tool_kind_ids: frozenset[str]
    benchmark_provider_ids: frozenset[str]

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"_UpstreamCatalogs(capability_pack_ids={self.capability_pack_ids!r}, source_kind_ids={self.source_kind_ids!r}, tool_kind_ids={self.tool_kind_ids!r})"


def _as_non_empty_string(value: Any, field_name: str, *, kit_id: str | None = None) -> str:
    if not isinstance(value, str) or not value.strip():
        suffix = f" for {kit_id}" if kit_id else ""
        raise DomainKitError(f"missing {field_name}{suffix}")
    return value.strip()


def _as_string_tuple(value: Any, field_name: str, *, kit_id: str) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise DomainKitError(f"{field_name} must be a non-empty list for {kit_id}")
    rows = tuple(str(item).strip() for item in value if str(item).strip())
    if not rows:
        raise DomainKitError(f"missing {field_name} for {kit_id}")
    return rows


def _as_string_mapping(value: Any, field_name: str, *, kit_id: str) -> Mapping[str, str]:
    if not isinstance(value, Mapping) or not value:
        raise DomainKitError(f"{field_name} must be a non-empty mapping for {kit_id}")
    rows = {
        str(key).strip(): str(item).strip() for key, item in value.items() if str(key).strip() and str(item).strip()
    }
    if not rows:
        raise DomainKitError(f"missing {field_name} for {kit_id}")
    return rows


def _as_rate_limit_policy(value: Any, *, kit_id: str) -> Mapping[str, int | str]:
    if not isinstance(value, Mapping) or not value:
        raise DomainKitError(f"rate_limit_policy must be a non-empty mapping for {kit_id}")
    requests_per_minute = value.get("requests_per_minute")
    if not isinstance(requests_per_minute, int) or requests_per_minute <= 0:
        raise DomainKitError(f"rate_limit_policy.requests_per_minute must be positive for {kit_id}")
    burst = value.get("burst")
    if burst is not None and (not isinstance(burst, int) or burst <= 0):
        raise DomainKitError(f"rate_limit_policy.burst must be positive for {kit_id}")
    return {str(key): item for key, item in value.items() if isinstance(item, (int, str))}


def _load_upstream_catalogs() -> _UpstreamCatalogs:
    try:
        capability_pack_ids = frozenset(pack.pack_id for pack in load_capability_pack_catalog())
        load_source_policies()
        source_kind_ids = frozenset(kind.value for kind in SourceKind)
        tool_kind_ids = frozenset(kind.value for kind in ToolKind)
        benchmark_provider_ids = frozenset(load_benchmark_importer_catalog().providers)
    except (
        BenchmarkImportRefused,
        CapabilityPackError,
        SourceCardLibraryError,
        OSError,
        RuntimeError,
        ValueError,
    ) as exc:
        raise DomainKitError(f"upstream Workbench catalog unavailable: {exc}") from exc
    return _UpstreamCatalogs(capability_pack_ids, source_kind_ids, tool_kind_ids, benchmark_provider_ids)


def _kit_from_mapping(raw: Mapping[str, Any], upstream: _UpstreamCatalogs) -> DomainKit:
    kit_id = _as_non_empty_string(raw.get("kit_id"), "kit_id")
    for field_name in _REQUIRED_STRING_FIELDS:
        _as_non_empty_string(raw.get(field_name), field_name, kit_id=kit_id)
    list_values = {
        field_name: _as_string_tuple(raw.get(field_name), field_name, kit_id=kit_id)
        for field_name in _REQUIRED_LIST_FIELDS
    }
    unsupported_claims = tuple(str(item).strip() for item in raw.get("unsupported_claims", ()) if str(item).strip())
    missing_packs = sorted(set(list_values["capability_pack_ids"]) - upstream.capability_pack_ids)
    if missing_packs:
        raise DomainKitError(f"unknown capability pack ids for {kit_id}: {', '.join(missing_packs)}")
    missing_sources = sorted(set(list_values["source_kinds"]) - upstream.source_kind_ids)
    if missing_sources:
        raise DomainKitError(f"unknown source kinds for {kit_id}: {', '.join(missing_sources)}")
    missing_tools = sorted(set(list_values["tool_kinds"]) - upstream.tool_kind_ids)
    if missing_tools:
        raise DomainKitError(f"unknown tool kinds for {kit_id}: {', '.join(missing_tools)}")
    missing_providers = sorted(set(list_values["benchmark_provider_ids"]) - upstream.benchmark_provider_ids)
    if missing_providers:
        raise DomainKitError(f"unknown benchmark providers for {kit_id}: {', '.join(missing_providers)}")
    return DomainKit(
        kit_id=kit_id,
        title=_as_non_empty_string(raw.get("title"), "title", kit_id=kit_id),
        domain=_as_non_empty_string(raw.get("domain"), "domain", kit_id=kit_id),
        supported_workflows=list_values["supported_workflows"],
        supported_claim_kinds=list_values["supported_claim_kinds"],
        unsupported_claims=unsupported_claims,
        capability_pack_ids=list_values["capability_pack_ids"],
        source_kinds=list_values["source_kinds"],
        tool_kinds=list_values["tool_kinds"],
        prompt_templates=_as_string_mapping(raw.get("prompt_templates"), "prompt_templates", kit_id=kit_id),
        examples=list_values["examples"],
        eval_fixtures=list_values["eval_fixtures"],
        rate_limit_policy=_as_rate_limit_policy(raw.get("rate_limit_policy"), kit_id=kit_id),
        refusal_boundaries=list_values["refusal_boundaries"],
        sample_notebook_refs=list_values["sample_notebook_refs"],
        benchmark_provider_ids=list_values["benchmark_provider_ids"],
        required_caveat_acknowledgements=list_values["required_caveat_acknowledgements"],
        metadata=dict(raw.get("metadata", {}) if isinstance(raw.get("metadata", {}), Mapping) else {}),
    )


def _load_catalog_uncached(catalog_dir: Path) -> tuple[DomainKit, ...]:
    paths = sorted(catalog_dir.glob("*.yaml"))
    if not paths:
        raise DomainKitError(f"no domain-kit YAML files found in {catalog_dir}")
    upstream = _load_upstream_catalogs()
    rows: list[DomainKit] = []
    seen: set[str] = set()
    for path in paths:
        try:
            doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            raise DomainKitError(f"cannot read domain-kit catalog {path}: {exc}") from exc
        if not isinstance(doc, Mapping):
            raise DomainKitError(f"domain-kit catalog {path} must be a mapping")
        if doc.get("schema_version") != _CATALOG_SCHEMA_VERSION:
            raise DomainKitError(
                f"domain-kit catalog {path} schema mismatch: expected {_CATALOG_SCHEMA_VERSION}, "
                f"got {doc.get('schema_version')!r}"
            )
        raw_kits = doc.get("kits")
        if not isinstance(raw_kits, Sequence) or isinstance(raw_kits, (str, bytes)) or not raw_kits:
            raise DomainKitError(f"domain-kit catalog {path} must contain non-empty kits")
        for raw in raw_kits:
            if not isinstance(raw, Mapping):
                raise DomainKitError(f"domain-kit row in {path} must be a mapping")
            kit = _kit_from_mapping(raw, upstream)
            if kit.kit_id in seen:
                raise DomainKitError(f"duplicate domain kit id {kit.kit_id}")
            seen.add(kit.kit_id)
            rows.append(kit)
    return tuple(rows)


def load_domain_kit_catalog(catalog_dir: Path | str | None = None) -> tuple[DomainKit, ...]:
    """Load domain-kit catalog rows, failing closed on untrusted references.

    Returns:
        Resolved domain kit catalog value.
    """
    global _DOMAIN_KIT_CATALOG_CACHE
    if catalog_dir is not None:
        return _load_catalog_uncached(Path(catalog_dir))
    with _DOMAIN_KIT_CATALOG_LOCK:
        if _DOMAIN_KIT_CATALOG_CACHE is None:
            loaded = _load_catalog_uncached(_CATALOG_DIR)
            _DOMAIN_KIT_CATALOG_CACHE = loaded
        return _DOMAIN_KIT_CATALOG_CACHE


def reset_domain_kit_catalog_for_test() -> None:
    """Clear the module-level catalog cache for deterministic tests."""
    global _DOMAIN_KIT_CATALOG_CACHE
    with _DOMAIN_KIT_CATALOG_LOCK:
        _DOMAIN_KIT_CATALOG_CACHE = None


class DomainKitService:
    """Evaluate domain-kit workflow and claim support."""

    def __init__(self, *, catalog_dir: Path | str | None = None) -> None:
        self._catalog_dir = Path(catalog_dir) if catalog_dir is not None else None

    def list_kits(self) -> list[dict[str, Any]]:
        """Return all domain kits as JSON-serializable rows."""
        return [kit.to_dict() for kit in load_domain_kit_catalog(self._catalog_dir)]

    def get_kit(self, kit_id: str) -> DomainKit:
        """Return one domain kit by id or fail closed.

        Returns:
            Resolved kit value.

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        for kit in load_domain_kit_catalog(self._catalog_dir):
            if kit.kit_id == kit_id:
                return kit
        raise DomainKitError(f"domain kit {kit_id!r} not found")

    def evaluate_request_support(
        self,
        kit_id: str,
        requested_workflow: str,
        requested_claim_kind: str,
        *,
        caveat_acknowledgements: Sequence[str] = (),
    ) -> DomainKitSupportVerdict:
        """Return whether the requested workflow and claim are explicitly supported.

        Args:
            kit_id: Kit id value consumed by evaluate_request_support().
            requested_workflow: Request object sent through the operation.
            requested_claim_kind: Request object sent through the operation.
            caveat_acknowledgements: Caveat acknowledgements value consumed by evaluate_request_support().

        Returns:
            DomainKitSupportVerdict value produced by evaluate_request_support().

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        try:
            kit = self.get_kit(kit_id)
        except DomainKitError as exc:
            logger.warning("Handled recoverable failure before fallback.", exc_info=True)
            if "not found" not in str(exc):
                raise
            return DomainKitSupportVerdict(
                kit_id=kit_id,
                supported=False,
                status="denied",
                requested_workflow=requested_workflow,
                requested_claim_kind=requested_claim_kind,
                reasons=(str(exc),),
            )
        reasons: list[str] = []
        workflow = requested_workflow.strip()
        claim_kind = requested_claim_kind.strip()
        if workflow not in kit.supported_workflows:
            reasons.append(f"workflow {workflow!r} is not explicitly supported")
        if claim_kind not in kit.supported_claim_kinds:
            reasons.append(f"claim kind {claim_kind!r} is not explicitly supported")
        if claim_kind in kit.unsupported_claims:
            reasons.append(f"claim kind {claim_kind!r} is explicitly unsupported")
        acknowledged = {item.strip() for item in caveat_acknowledgements if item.strip()}
        missing_caveats = tuple(item for item in kit.required_caveat_acknowledgements if item not in acknowledged)
        if missing_caveats:
            reasons.append("required caveat acknowledgements missing")
        if reasons:
            return DomainKitSupportVerdict(
                kit_id=kit.kit_id,
                supported=False,
                status="denied",
                requested_workflow=workflow,
                requested_claim_kind=claim_kind,
                reasons=tuple(reasons),
                required_caveat_acknowledgements=kit.required_caveat_acknowledgements,
                missing_caveat_acknowledgements=missing_caveats,
            )
        return DomainKitSupportVerdict(
            kit_id=kit.kit_id,
            supported=True,
            status="supported",
            requested_workflow=workflow,
            requested_claim_kind=claim_kind,
            reasons=("explicitly supported by domain kit",),
            required_caveat_acknowledgements=kit.required_caveat_acknowledgements,
        )


__all__ = [
    "DomainKit",
    "DomainKitError",
    "DomainKitService",
    "DomainKitSupportVerdict",
    "load_domain_kit_catalog",
    "reset_domain_kit_catalog_for_test",
]
