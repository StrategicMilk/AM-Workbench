"""Typed benchmark importer for Workbench dataset and eval records.

The importer normalizes external benchmarks, local failed traces, GitHub
issues, support tickets, manual examples, and release-evidence cases through
a pluggable provider catalog. Imports are side-effect free until
``BenchmarkImporter.import_case`` is called. That method writes durable state
through ``DatasetRevisionStore`` and ``WorkbenchSpine`` only; it creates the
synthetic run required by the existing spine eval dependency checker.

Decision-Ref: ADR-0126.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Final, Protocol, runtime_checkable

import yaml

from vetinari.constants import PROJECT_ROOT
from vetinari.workbench.assets import WorkbenchAsset
from vetinari.workbench.evals import EvalKind, EvalResult
from vetinari.workbench.metadata_spine import (
    _PROJECT_ID_RE as _STORAGE_PROJECT_ID_RE,
)
from vetinari.workbench.spine import WorkbenchProjectIdRejected
from vetinari.workbench.spine import validate_project_id as _validate_workbench_project_id
from vetinari.workbench.trace_to_eval import TraceEvalFactory, TraceEvalFactoryError

if TYPE_CHECKING:
    from vetinari.workbench.benchmark_importer_runtime import BenchmarkImporter

_PROJECT_ID_RE = _STORAGE_PROJECT_ID_RE
_DEFAULT_CATALOG_PATH: Final[Path] = PROJECT_ROOT / "config" / "workbench" / "benchmark_importers.yaml"
_PROVIDER_ID_RE: Final[re.Pattern[str]] = re.compile(r"[a-z][a-z0-9_]{1,63}")


class BenchmarkSourceKind(str, Enum):
    """Source kind for a benchmark case draft."""

    EXTERNAL_BENCHMARK = "external_benchmark"
    LOCAL_FAILED_TRACE = "local_failed_trace"
    GITHUB_ISSUE = "github_issue"
    SUPPORT_TICKET = "support_ticket"
    MANUAL_EXAMPLE = "manual_example"
    RELEASE_EVIDENCE = "release_evidence"


class LicenseClassification(str, Enum):
    """License classification accepted by the benchmark importer."""

    PERMISSIVE_OPEN_SOURCE = "permissive_open_source"
    RESTRICTED_RESEARCH_USE = "restricted_research_use"
    PROPRIETARY_REDISTRIBUTION_BLOCKED = "proprietary_redistribution_blocked"
    UNKNOWN_BLOCKED = "unknown_blocked"


class PrivacyClassification(str, Enum):
    """Privacy classification accepted by the benchmark importer."""

    PUBLIC = "public"
    INTERNAL_ONLY = "internal_only"
    PII_BLOCKED = "pii_blocked"


class BenchmarkImportRefused(Exception):
    """Raised when benchmark import input fails closed."""

    def __init__(self, reason: str, case_source_uri: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.case_source_uri = case_source_uri

    def __str__(self) -> str:
        return (
            f"BenchmarkImportRefused(reason={self.reason!r}, case_source_uri={self.case_source_uri!r}): {self.args[0]}"
        )


class BenchmarkProjectIdRejected(ValueError):
    """Raised when a project id is not safe to use as storage scope."""


@dataclass(frozen=True, slots=True)
class BenchmarkCaseDraft:
    """One normalized import draft produced by a BenchmarkProvider."""

    source_uri: str
    source_kind: BenchmarkSourceKind
    license_classification: LicenseClassification
    privacy_classification: PrivacyClassification
    revision_pin: str
    expected_output_schema: str
    allowed_eval_method: EvalKind
    case_payload: dict[str, str]

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "source_kind", BenchmarkSourceKind(getattr(self.source_kind, "value", self.source_kind))
        )
        object.__setattr__(
            self,
            "license_classification",
            LicenseClassification(getattr(self.license_classification, "value", self.license_classification)),
        )
        object.__setattr__(
            self,
            "privacy_classification",
            PrivacyClassification(getattr(self.privacy_classification, "value", self.privacy_classification)),
        )
        object.__setattr__(
            self,
            "allowed_eval_method",
            EvalKind(getattr(self.allowed_eval_method, "value", self.allowed_eval_method)),
        )
        for name in ("source_uri", "revision_pin", "expected_output_schema"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"BenchmarkCaseDraft.{name} must be non-empty")
        if not isinstance(self.source_kind, BenchmarkSourceKind):
            raise ValueError("BenchmarkCaseDraft.source_kind must be a BenchmarkSourceKind")
        if not isinstance(self.license_classification, LicenseClassification):
            raise ValueError("BenchmarkCaseDraft.license_classification must be a LicenseClassification")
        if not isinstance(self.privacy_classification, PrivacyClassification):
            raise ValueError("BenchmarkCaseDraft.privacy_classification must be a PrivacyClassification")
        if not isinstance(self.allowed_eval_method, EvalKind):
            raise ValueError("BenchmarkCaseDraft.allowed_eval_method must be an EvalKind")
        if not isinstance(self.case_payload, dict) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in self.case_payload.items()
        ):
            raise ValueError("BenchmarkCaseDraft.case_payload must be dict[str, str]")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"BenchmarkCaseDraft(source_uri={self.source_uri!r}, source_kind={self.source_kind!r}, license_classification={self.license_classification!r})"


@dataclass(frozen=True, slots=True)
class BenchmarkProviderConfig:
    """Validated provider policy loaded from the YAML catalog."""

    provider_id: str
    kind: BenchmarkSourceKind
    class_path: str
    allowed_license_classifications: tuple[LicenseClassification, ...]
    allowed_privacy_classifications: tuple[PrivacyClassification, ...]
    default_eval_method: EvalKind
    description: str
    provider_class: type[BenchmarkProvider]

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"BenchmarkProviderConfig(provider_id={self.provider_id!r}, kind={self.kind!r}, class_path={self.class_path!r})"


@dataclass(frozen=True, slots=True)
class BenchmarkImporterCatalog:
    """Validated benchmark importer catalog."""

    schema_version: int
    providers: dict[str, BenchmarkProviderConfig]
    license_classifications: tuple[LicenseClassification, ...]
    privacy_classifications: tuple[PrivacyClassification, ...]
    eval_methods: tuple[EvalKind, ...]
    allowed_output_schemas: tuple[str, ...]

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"BenchmarkImporterCatalog(schema_version={self.schema_version!r}, providers={self.providers!r}, license_classifications={self.license_classifications!r})"


@runtime_checkable
class BenchmarkProvider(Protocol):
    """Provider contract for normalizing one source-specific payload."""

    source_kind: BenchmarkSourceKind

    def build_case(self, payload: dict[str, str], config: BenchmarkProviderConfig) -> BenchmarkCaseDraft:
        """Return a normalized benchmark case draft.

        Args:
            payload: Payload data validated or transformed by the operation.
            config: Config value consumed by build_case().
        """


class _PayloadProvider:
    source_kind: BenchmarkSourceKind

    def build_case(self, payload: dict[str, str], config: BenchmarkProviderConfig) -> BenchmarkCaseDraft:
        return _draft_from_payload(payload, self.source_kind, config)


class ExternalHttpProvider(_PayloadProvider):
    """Runtime contract for ExternalHttpProvider."""

    source_kind = BenchmarkSourceKind.EXTERNAL_BENCHMARK


class GitHubIssueProvider(_PayloadProvider):
    """Runtime contract for GitHubIssueProvider."""

    source_kind = BenchmarkSourceKind.GITHUB_ISSUE


class SupportTicketProvider(_PayloadProvider):
    """Runtime contract for SupportTicketProvider."""

    source_kind = BenchmarkSourceKind.SUPPORT_TICKET


class ManualExampleProvider(_PayloadProvider):
    """Runtime contract for ManualExampleProvider."""

    source_kind = BenchmarkSourceKind.MANUAL_EXAMPLE


class ReleaseEvidenceProvider(_PayloadProvider):
    """Runtime contract for ReleaseEvidenceProvider."""

    source_kind = BenchmarkSourceKind.RELEASE_EVIDENCE


class LocalTraceProvider(_PayloadProvider):
    """Provider that can normalize an existing local trace payload."""

    source_kind = BenchmarkSourceKind.LOCAL_FAILED_TRACE

    def __init__(self, factory: TraceEvalFactory | None = None) -> None:
        self._factory = factory

    def build_case(self, payload: dict[str, str], config: BenchmarkProviderConfig) -> BenchmarkCaseDraft:
        """Execute the build case operation.

        Args:
            payload: Payload data validated or transformed by the operation.
            config: Config value consumed by build_case().

        Returns:
            Newly constructed case value.

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        if self._factory is None or "trace_id" not in payload:
            return _draft_from_payload(payload, self.source_kind, config)
        try:
            scaffold = self._factory.trace_to_eval_case(payload["trace_id"])
        except TraceEvalFactoryError as exc:
            raise BenchmarkImportRefused(
                "trace-conversion-failed",
                payload.get("source_uri", payload.get("trace_id", "local-trace")),
                "local trace conversion failed; select a trace with replayable prompt and tool data",
            ) from exc
        normalized = dict(payload)
        normalized.setdefault("source_uri", f"trace:{scaffold.trace_id}")
        normalized.setdefault("revision_pin", scaffold.trace_id)
        normalized.setdefault("expected_output_schema", "text")
        normalized.setdefault("prompt", scaffold.prompt_text)
        return _draft_from_payload(normalized, self.source_kind, config)


_ALLOWED_PROVIDER_CLASSES: dict[str, type[BenchmarkProvider]] = {
    f"{ExternalHttpProvider.__module__}.{ExternalHttpProvider.__name__}": ExternalHttpProvider,
    f"{GitHubIssueProvider.__module__}.{GitHubIssueProvider.__name__}": GitHubIssueProvider,
    f"{SupportTicketProvider.__module__}.{SupportTicketProvider.__name__}": SupportTicketProvider,
    f"{ManualExampleProvider.__module__}.{ManualExampleProvider.__name__}": ManualExampleProvider,
    f"{ReleaseEvidenceProvider.__module__}.{ReleaseEvidenceProvider.__name__}": ReleaseEvidenceProvider,
    f"{LocalTraceProvider.__module__}.{LocalTraceProvider.__name__}": LocalTraceProvider,
}


@dataclass(frozen=True, slots=True)
class BenchmarkImportRecord:
    """Return value for a successful import."""

    eval_result: EvalResult
    asset: WorkbenchAsset
    revision_id: str
    provider_id: str

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"BenchmarkImportRecord(eval_result={self.eval_result!r}, asset={self.asset!r}, revision_id={self.revision_id!r})"


def validate_project_id(value: str | None) -> str:
    """Return ``value`` if it is canonical, otherwise raise.

    Returns:
        Validation outcome for project id.

    Raises:
        Exception: Propagates validation or runtime failures from the underlying operation.
    """
    try:
        return _validate_workbench_project_id(value)
    except WorkbenchProjectIdRejected as exc:
        raise BenchmarkProjectIdRejected(str(exc)) from exc


def load_benchmark_importer_catalog(path: Path | str = _DEFAULT_CATALOG_PATH) -> BenchmarkImporterCatalog:
    """Load and validate the benchmark importer YAML catalog.

    Returns:
        Resolved benchmark importer catalog value.

    Raises:
        Exception: Propagates validation or runtime failures from the underlying operation.
    """
    catalog_path = Path(path)
    if not catalog_path.exists():
        raise BenchmarkImportRefused(
            "catalog-not-found",
            str(catalog_path),
            "benchmark importer catalog was not found; create config/workbench/benchmark_importers.yaml",
        )
    try:
        raw = yaml.safe_load(catalog_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise BenchmarkImportRefused(
            "catalog-unreadable",
            str(catalog_path),
            "benchmark importer catalog could not be read; fix permissions or path",
        ) from exc
    except yaml.YAMLError as exc:
        raise BenchmarkImportRefused(
            "catalog-malformed",
            str(catalog_path),
            "benchmark importer catalog YAML is malformed; fix YAML syntax",
        ) from exc
    try:
        return _catalog_from_mapping(raw)
    except (KeyError, TypeError, ValueError, ImportError, AttributeError) as exc:
        raise BenchmarkImportRefused(
            "catalog-malformed",
            str(catalog_path),
            "benchmark importer catalog schema is invalid; align provider entries with importer enums",
        ) from exc


def _catalog_from_mapping(raw: object) -> BenchmarkImporterCatalog:
    if not isinstance(raw, dict):
        raise TypeError("catalog must be a mapping")
    schema_version = int(raw["schema_version"])
    if schema_version != 1:
        raise ValueError("unsupported catalog schema_version")
    license_values = tuple(LicenseClassification(value) for value in raw["license_classifications"])
    privacy_values = tuple(PrivacyClassification(value) for value in raw["privacy_classifications"])
    eval_methods = tuple(EvalKind(value) for value in raw["eval_methods"])
    allowed_output_schemas = tuple(str(value) for value in raw["allowed_output_schemas"])
    if not allowed_output_schemas or any(not value.strip() for value in allowed_output_schemas):
        raise ValueError("catalog requires non-empty allowed_output_schemas")
    providers: dict[str, BenchmarkProviderConfig] = {}
    for provider_id, body in raw["providers"].items():
        if not isinstance(provider_id, str) or not isinstance(body, dict):
            raise TypeError("provider entries must be mappings")
        if _PROVIDER_ID_RE.fullmatch(provider_id) is None:
            raise ValueError(f"provider id {provider_id!r} must match [a-z][a-z0-9_]{{1,63}}")
        class_path = str(body["class_path"])
        provider_class = _resolve_provider_class(class_path)
        provider_kind = BenchmarkSourceKind(body["kind"])
        providers[provider_id] = BenchmarkProviderConfig(
            provider_id=provider_id,
            kind=provider_kind,
            class_path=class_path,
            allowed_license_classifications=tuple(
                LicenseClassification(value) for value in body["allowed_license_classifications"]
            ),
            allowed_privacy_classifications=tuple(
                PrivacyClassification(value) for value in body["allowed_privacy_classifications"]
            ),
            default_eval_method=EvalKind(body["default_eval_method"]),
            description=str(body["description"]),
            provider_class=provider_class,
        )
    if not providers:
        raise ValueError("catalog requires at least one provider")
    return BenchmarkImporterCatalog(
        schema_version,
        providers,
        license_values,
        privacy_values,
        eval_methods,
        allowed_output_schemas,
    )


def _resolve_provider_class(class_path: str) -> type[BenchmarkProvider]:
    try:
        return _ALLOWED_PROVIDER_CLASSES[class_path]
    except KeyError as exc:
        raise ImportError(f"provider class_path {class_path!r} is not in the closed provider registry") from exc


def _draft_from_payload(
    payload: dict[str, str],
    source_kind: BenchmarkSourceKind,
    config: BenchmarkProviderConfig,
) -> BenchmarkCaseDraft:
    source_uri = payload.get("source_uri", "")
    license_value = payload.get("license_classification", "")
    privacy_value = payload.get("privacy_classification", "")
    eval_value = payload.get("allowed_eval_method") or config.default_eval_method.value
    return BenchmarkCaseDraft(
        source_uri=source_uri,
        source_kind=source_kind,
        license_classification=LicenseClassification(license_value),
        privacy_classification=PrivacyClassification(privacy_value),
        revision_pin=payload.get("revision_pin", ""),
        expected_output_schema=payload.get("expected_output_schema", ""),
        allowed_eval_method=EvalKind(eval_value),
        case_payload={key: value for key, value in payload.items() if isinstance(key, str) and isinstance(value, str)},
    )


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def __getattr__(name: str) -> object:
    if name == "BenchmarkImporter":
        from vetinari.workbench.benchmark_importer_runtime import BenchmarkImporter

        return BenchmarkImporter
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "BenchmarkCaseDraft",
    "BenchmarkImportRecord",
    "BenchmarkImportRefused",
    "BenchmarkImporter",
    "BenchmarkImporterCatalog",
    "BenchmarkProjectIdRejected",
    "BenchmarkProvider",
    "BenchmarkProviderConfig",
    "BenchmarkSourceKind",
    "ExternalHttpProvider",
    "GitHubIssueProvider",
    "LicenseClassification",
    "LocalTraceProvider",
    "ManualExampleProvider",
    "PrivacyClassification",
    "ReleaseEvidenceProvider",
    "SupportTicketProvider",
    "load_benchmark_importer_catalog",
    "validate_project_id",
]
