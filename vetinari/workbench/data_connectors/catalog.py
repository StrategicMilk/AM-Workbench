"""Fail-closed data connector catalog bound to source-card provenance.

Imports are side-effect free. The catalog is immutable module-level state:
callers receive tuples or copies and there is no runtime cache to mutate.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from types import MappingProxyType

from vetinari.workbench.source_cards import SourceCard, SourceKind, evaluate_freshness


class DataConnectorKind(str, Enum):
    """Connector classes that can feed data into AM Workbench."""

    FILE = "file"
    FOLDER = "folder"
    API = "api"
    WEB = "web"
    REPOSITORY = "repository"
    MODEL_HUB = "model_hub"
    ISSUE_TRACKER = "issue_tracker"


class ConnectorPolicyError(ValueError):
    """Raised when a connector policy or source binding is not trustworthy."""


@dataclass(frozen=True, slots=True)
class DataConnector:
    """Immutable connector policy row."""

    connector_id: str
    kind: DataConnectorKind
    source_kind: SourceKind
    display_name: str
    required_provenance_keys: tuple[str, ...]
    permitted_claim_kinds: tuple[str, ...]
    claim_limits: tuple[str, ...]
    requires_freshness: bool = True

    def __post_init__(self) -> None:
        _require_non_empty(self.connector_id, "connector_id")
        _require_non_empty(self.display_name, "display_name")
        if not isinstance(self.kind, DataConnectorKind):
            raise ConnectorPolicyError("kind must be a DataConnectorKind")
        if not isinstance(self.source_kind, SourceKind):
            raise ConnectorPolicyError("source_kind must be a SourceKind")
        _require_string_tuple(self.required_provenance_keys, "required_provenance_keys")
        _require_string_tuple(self.permitted_claim_kinds, "permitted_claim_kinds")
        _require_string_tuple(self.claim_limits, "claim_limits")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return (
            f"DataConnector(connector_id={self.connector_id!r}, kind={self.kind!r}, source_kind={self.source_kind!r})"
        )


@dataclass(frozen=True, slots=True)
class ConnectorBinding:
    """Trusted binding between a connector and one source card."""

    connector_id: str
    source_card_id: str
    source_kind: SourceKind
    permitted_claim_kinds: tuple[str, ...]
    claim_limits: tuple[str, ...]
    provenance_keys: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_non_empty(self.connector_id, "connector_id")
        _require_non_empty(self.source_card_id, "source_card_id")
        if not isinstance(self.source_kind, SourceKind):
            raise ConnectorPolicyError("source_kind must be a SourceKind")
        _require_string_tuple(self.permitted_claim_kinds, "permitted_claim_kinds")
        _require_string_tuple(self.claim_limits, "claim_limits")
        _require_string_tuple(self.provenance_keys, "provenance_keys")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"ConnectorBinding(connector_id={self.connector_id!r}, source_card_id={self.source_card_id!r}, source_kind={self.source_kind!r})"


@dataclass(frozen=True, slots=True)
class SourceCardBindingDecision:
    """Fail-closed binding result."""

    passed: bool
    binding: ConnectorBinding | None
    rejection_reasons: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.passed:
            if self.binding is None:
                raise ConnectorPolicyError("passed source-card binding requires binding")
            if self.rejection_reasons:
                raise ConnectorPolicyError("passed source-card binding cannot include rejection_reasons")
        elif not self.rejection_reasons:
            raise ConnectorPolicyError("failed source-card binding requires rejection_reasons")


class DataConnectorCatalog:
    """Read-only connector catalog."""

    def __init__(self, connectors: tuple[DataConnector, ...] | None = None) -> None:
        rows = CONNECTOR_CATALOG if connectors is None else connectors
        by_id: dict[str, DataConnector] = {}
        for connector in rows:
            if connector.connector_id in by_id:
                raise ConnectorPolicyError(f"duplicate connector_id {connector.connector_id!r}")
            by_id[connector.connector_id] = connector
        self._by_id = by_id

    def list_connectors(self) -> tuple[DataConnector, ...]:
        """Return connectors in stable connector_id order."""
        return tuple(self._by_id[key] for key in sorted(self._by_id))

    def get_connector(self, connector_id: str) -> DataConnector:
        """Return one connector or fail closed for unknown policy.

        Returns:
            Resolved connector value.

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        try:
            return self._by_id[connector_id]
        except KeyError as exc:
            raise ConnectorPolicyError(f"unknown connector_id {connector_id!r}") from exc

    def bind_source_card(
        self,
        *,
        connector_id: str,
        source_card: SourceCard,
        claim_kind: str,
        now_utc: datetime | None = None,
    ) -> SourceCardBindingDecision:
        """Bind a source card to a connector, rejecting unknown proof.

        Returns:
            SourceCardBindingDecision value produced by bind_source_card().
        """
        connector = self.get_connector(connector_id)
        return bind_source_card_to_connector(
            connector=connector,
            source_card=source_card,
            claim_kind=claim_kind,
            now_utc=now_utc,
        )


def bind_source_card_to_connector(
    *,
    connector: DataConnector,
    source_card: SourceCard,
    claim_kind: str,
    now_utc: datetime | None = None,
) -> SourceCardBindingDecision:
    """Return a trusted connector binding or explicit rejection reasons.

    Returns:
        SourceCardBindingDecision value produced by bind_source_card_to_connector().

    Raises:
        Exception: Propagates validation or runtime failures from the underlying operation.
    """
    reasons: list[str] = []
    if not isinstance(source_card, SourceCard):
        raise ConnectorPolicyError("source_card must be a SourceCard")
    if source_card.kind is not connector.source_kind:
        reasons.append(
            f"source kind {source_card.kind.value!r} does not match connector policy {connector.source_kind.value!r}"
        )
    if claim_kind not in connector.permitted_claim_kinds:
        reasons.append(f"claim_kind {claim_kind!r} is not permitted for connector {connector.connector_id!r}")

    provenance = dict(source_card.provenance)
    missing = tuple(key for key in connector.required_provenance_keys if not provenance.get(key, "").strip())
    if missing:
        reasons.append(f"missing required provenance keys: {', '.join(missing)}")
    if not source_card.caveats:
        reasons.append("source card must declare caveats before connector binding")
    if connector.requires_freshness:
        freshness = evaluate_freshness(source_card, now_utc=now_utc)
        if not freshness.passed:
            reasons.append(f"freshness failed: {freshness.reason}")

    if reasons:
        return SourceCardBindingDecision(passed=False, binding=None, rejection_reasons=tuple(reasons))
    return SourceCardBindingDecision(
        passed=True,
        binding=ConnectorBinding(
            connector_id=connector.connector_id,
            source_card_id=source_card.source_card_id,
            source_kind=source_card.kind,
            permitted_claim_kinds=connector.permitted_claim_kinds,
            claim_limits=connector.claim_limits,
            provenance_keys=tuple(sorted(provenance)),
        ),
        rejection_reasons=(),
    )


def _require_non_empty(value: str, field_name: str) -> None:
    if not value or not value.strip():
        raise ConnectorPolicyError(f"{field_name} must be non-empty")


def _require_string_tuple(values: tuple[str, ...], field_name: str) -> None:
    if not isinstance(values, tuple) or not values:
        raise ConnectorPolicyError(f"{field_name} must be a non-empty tuple")
    if any(not isinstance(value, str) or not value.strip() for value in values):
        raise ConnectorPolicyError(f"{field_name} must contain non-empty strings")


CONNECTOR_CATALOG: tuple[DataConnector, ...] = (
    DataConnector(
        connector_id="file",
        kind=DataConnectorKind.FILE,
        source_kind=SourceKind.DATASET_REF,
        display_name="File",
        required_provenance_keys=("source", "path", "content_sha256"),
        permitted_claim_kinds=("dataset_observation", "schema_observation"),
        claim_limits=("single-file scope only", "content hash required"),
    ),
    DataConnector(
        connector_id="folder",
        kind=DataConnectorKind.FOLDER,
        source_kind=SourceKind.DATASET_REF,
        display_name="Folder",
        required_provenance_keys=("source", "path", "manifest_sha256"),
        permitted_claim_kinds=("dataset_observation", "schema_observation"),
        claim_limits=("manifest-bounded scope only", "recursive claims require manifest evidence"),
    ),
    DataConnector(
        connector_id="api",
        kind=DataConnectorKind.API,
        source_kind=SourceKind.HTTP_API,
        display_name="HTTP API",
        required_provenance_keys=("source", "endpoint", "observed_at_utc"),
        permitted_claim_kinds=("api_observation", "schema_observation"),
        claim_limits=("fresh response window only", "no private-account claims without credential policy"),
    ),
    DataConnector(
        connector_id="web",
        kind=DataConnectorKind.WEB,
        source_kind=SourceKind.WEB_PAGE,
        display_name="Web page",
        required_provenance_keys=("source", "url", "observed_at_utc"),
        permitted_claim_kinds=("web_observation", "citation"),
        claim_limits=("public page only", "freshness and caveats must be acknowledged"),
    ),
    DataConnector(
        connector_id="repository",
        kind=DataConnectorKind.REPOSITORY,
        source_kind=SourceKind.DATASET_REF,
        display_name="Repository",
        required_provenance_keys=("source", "repo_url", "commit_sha"),
        permitted_claim_kinds=("code_observation", "schema_observation"),
        claim_limits=("commit-pinned facts only", "branch-head claims are not trusted"),
    ),
    DataConnector(
        connector_id="model_hub",
        kind=DataConnectorKind.MODEL_HUB,
        source_kind=SourceKind.DATASET_REF,
        display_name="Model hub",
        required_provenance_keys=("source", "model_id", "revision"),
        permitted_claim_kinds=("model_observation", "license_observation"),
        claim_limits=("revision-pinned metadata only", "no benchmark claims without eval evidence"),
    ),
    DataConnector(
        connector_id="issue_tracker",
        kind=DataConnectorKind.ISSUE_TRACKER,
        source_kind=SourceKind.HTTP_API,
        display_name="Issue tracker",
        required_provenance_keys=("source", "tracker_url", "observed_at_utc"),
        permitted_claim_kinds=("issue_observation", "status_observation"),
        claim_limits=("observed ticket state only", "no assignee intent inference"),
    ),
)

CLAIM_LIMITS_BY_CONNECTOR: Mapping[str, tuple[str, ...]] = MappingProxyType({
    connector.connector_id: connector.claim_limits for connector in CONNECTOR_CATALOG
})

__all__ = [
    "CLAIM_LIMITS_BY_CONNECTOR",
    "CONNECTOR_CATALOG",
    "ConnectorBinding",
    "ConnectorPolicyError",
    "DataConnector",
    "DataConnectorCatalog",
    "DataConnectorKind",
    "SourceCardBindingDecision",
    "bind_source_card_to_connector",
]
