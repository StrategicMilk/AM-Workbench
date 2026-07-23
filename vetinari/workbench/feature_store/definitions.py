"""Feature-store definition contracts for Workbench context assembly."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any


class FeatureStoreDefinitionError(ValueError):
    """Raised when feature-store definitions are incomplete or ungoverned."""


class FeatureValueType(str, Enum):
    """Supported feature value families."""

    STRING = "string"
    INTEGER = "integer"
    FLOAT = "float"
    BOOLEAN = "boolean"
    JSON = "json"


class TransformationKind(str, Enum):
    """Kinds of declared feature transformations."""

    SQL = "sql"
    PYTHON = "python"
    AGGREGATION = "aggregation"
    EMBEDDING = "embedding"


class FeatureStalenessAction(str, Enum):
    """Fail-closed action when feature data is too old."""

    REJECT = "reject"
    WARN = "warn"


@dataclass(frozen=True, slots=True)
class FeatureFreshnessPolicy:
    """Freshness limits for one feature or context view."""

    max_age_seconds: int
    stale_action: str | FeatureStalenessAction = FeatureStalenessAction.REJECT

    def __post_init__(self) -> None:
        if self.max_age_seconds < 0:
            raise FeatureStoreDefinitionError("max_age_seconds must be >= 0")
        action = self.stale_action.value if isinstance(self.stale_action, FeatureStalenessAction) else self.stale_action
        if action not in {item.value for item in FeatureStalenessAction}:
            raise FeatureStoreDefinitionError("stale_action must be reject or warn")
        object.__setattr__(self, "stale_action", action)


@dataclass(frozen=True, slots=True)
class LineageReference:
    """Dataset, quality, and lineage evidence attached to a feature surface."""

    dataset_revision_id: str
    quality_report_id: str
    lineage_graph_id: str
    evidence_ref: str

    def __post_init__(self) -> None:
        _require_non_empty(self.dataset_revision_id, "dataset_revision_id")
        _require_non_empty(self.quality_report_id, "quality_report_id")
        _require_non_empty(self.lineage_graph_id, "lineage_graph_id")
        _require_non_empty(self.evidence_ref, "evidence_ref")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"LineageReference(dataset_revision_id={self.dataset_revision_id!r}, quality_report_id={self.quality_report_id!r}, lineage_graph_id={self.lineage_graph_id!r})"


@dataclass(frozen=True, slots=True)
class EntityDefinition:
    """Entity key declaration used by offline and online feature lookups."""

    entity_id: str
    owner: str
    join_keys: tuple[str, ...]
    description: str = ""

    def __post_init__(self) -> None:
        _require_non_empty(self.entity_id, "entity_id")
        _require_non_empty(self.owner, "owner")
        _require_non_empty_tuple(self.join_keys, "join_keys")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"EntityDefinition(entity_id={self.entity_id!r}, owner={self.owner!r}, join_keys={self.join_keys!r})"


@dataclass(frozen=True, slots=True)
class FeatureDefinition:
    """Governed feature declaration bound to dataset, quality, and lineage evidence."""

    feature_id: str
    entity_id: str
    owner: str
    value_type: FeatureValueType | str
    freshness_policy: FeatureFreshnessPolicy
    lineage: LineageReference
    description: str = ""
    transformation_id: str = ""

    def __post_init__(self) -> None:
        _require_non_empty(self.feature_id, "feature_id")
        _require_non_empty(self.entity_id, "entity_id")
        _require_non_empty(self.owner, "owner")
        if not isinstance(self.freshness_policy, FeatureFreshnessPolicy):
            raise FeatureStoreDefinitionError("freshness_policy must be a FeatureFreshnessPolicy")
        if not isinstance(self.lineage, LineageReference):
            raise FeatureStoreDefinitionError("lineage must be a LineageReference")
        object.__setattr__(self, "value_type", _coerce_enum(self.value_type, FeatureValueType, "value_type"))

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"FeatureDefinition(feature_id={self.feature_id!r}, entity_id={self.entity_id!r}, owner={self.owner!r})"


@dataclass(frozen=True, slots=True)
class TransformationDefinition:
    """Declared feature transformation with explicit inputs, outputs, and lineage."""

    transformation_id: str
    owner: str
    kind: TransformationKind | str
    input_feature_ids: tuple[str, ...]
    output_feature_ids: tuple[str, ...]
    lineage: LineageReference
    code_ref: str
    description: str = ""

    def __post_init__(self) -> None:
        _require_non_empty(self.transformation_id, "transformation_id")
        _require_non_empty(self.owner, "owner")
        _require_non_empty_tuple(self.input_feature_ids, "input_feature_ids")
        _require_non_empty_tuple(self.output_feature_ids, "output_feature_ids")
        if not isinstance(self.lineage, LineageReference):
            raise FeatureStoreDefinitionError("lineage must be a LineageReference")
        _require_non_empty(self.code_ref, "code_ref")
        object.__setattr__(self, "kind", _coerce_enum(self.kind, TransformationKind, "kind"))

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"TransformationDefinition(transformation_id={self.transformation_id!r}, owner={self.owner!r}, kind={self.kind!r})"


@dataclass(frozen=True, slots=True)
class ContextViewDefinition:
    """Governed online/offline feature context view."""

    context_view_id: str
    owner: str
    entity_id: str
    feature_ids: tuple[str, ...]
    freshness_policy: FeatureFreshnessPolicy
    lineage: LineageReference
    description: str = ""
    required: bool = True

    def __post_init__(self) -> None:
        _require_non_empty(self.context_view_id, "context_view_id")
        _require_non_empty(self.owner, "owner")
        _require_non_empty(self.entity_id, "entity_id")
        _require_non_empty_tuple(self.feature_ids, "feature_ids")
        if not isinstance(self.freshness_policy, FeatureFreshnessPolicy):
            raise FeatureStoreDefinitionError("freshness_policy must be a FeatureFreshnessPolicy")
        if not isinstance(self.lineage, LineageReference):
            raise FeatureStoreDefinitionError("lineage must be a LineageReference")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"ContextViewDefinition(context_view_id={self.context_view_id!r}, owner={self.owner!r}, entity_id={self.entity_id!r})"


Definition = EntityDefinition | FeatureDefinition | TransformationDefinition | ContextViewDefinition


def definition_to_payload(definition: Definition) -> dict[str, Any]:
    """Serialize a feature-store definition to stable JSON-compatible data.

    Returns:
        dict[str, Any] value produced by definition_to_payload().

    Raises:
        Exception: Propagates validation or runtime failures from the underlying operation.
    """
    if not isinstance(
        definition,
        (EntityDefinition, FeatureDefinition, TransformationDefinition, ContextViewDefinition),
    ):
        raise FeatureStoreDefinitionError("unknown feature-store definition type")
    return _to_jsonable(asdict(definition))


def definition_from_payload(definition_type: str, payload: dict[str, Any]) -> Definition:
    """Rebuild a definition from registry JSON payload.

    Args:
        definition_type: Definition type value consumed by definition_from_payload().
        payload: Payload data validated or transformed by the operation.

    Returns:
        Definition value produced by definition_from_payload().

    Raises:
        Exception: Propagates validation or runtime failures from the underlying operation.
    """
    if definition_type == "entity":
        data = dict(payload)
        data["join_keys"] = tuple(data["join_keys"])
        return EntityDefinition(**data)
    if definition_type == "feature":
        data = dict(payload)
        data["freshness_policy"] = FeatureFreshnessPolicy(**data["freshness_policy"])
        data["lineage"] = LineageReference(**data["lineage"])
        return FeatureDefinition(**data)
    if definition_type == "transformation":
        data = dict(payload)
        data["input_feature_ids"] = tuple(data["input_feature_ids"])
        data["output_feature_ids"] = tuple(data["output_feature_ids"])
        data["lineage"] = LineageReference(**data["lineage"])
        return TransformationDefinition(**data)
    if definition_type == "context_view":
        data = dict(payload)
        data["feature_ids"] = tuple(data["feature_ids"])
        data["freshness_policy"] = FeatureFreshnessPolicy(**data["freshness_policy"])
        data["lineage"] = LineageReference(**data["lineage"])
        return ContextViewDefinition(**data)
    raise FeatureStoreDefinitionError(f"unknown definition type {definition_type!r}")


def definition_type(definition: Definition) -> str:
    """Return the registry discriminator for a definition.

    Returns:
        str value produced by definition_type().

    Raises:
        Exception: Propagates validation or runtime failures from the underlying operation.
    """
    if isinstance(definition, EntityDefinition):
        return "entity"
    if isinstance(definition, FeatureDefinition):
        return "feature"
    if isinstance(definition, TransformationDefinition):
        return "transformation"
    if isinstance(definition, ContextViewDefinition):
        return "context_view"
    raise FeatureStoreDefinitionError("unknown feature-store definition type")


def _require_non_empty(value: str, field_name: str) -> None:
    if not value or not value.strip():
        raise FeatureStoreDefinitionError(f"{field_name} must be non-empty")


def _require_non_empty_tuple(values: tuple[str, ...], field_name: str) -> None:
    if not isinstance(values, tuple) or not values:
        raise FeatureStoreDefinitionError(f"{field_name} must be a non-empty tuple")
    for value in values:
        _require_non_empty(value, f"{field_name}[]")


def _coerce_enum(value: Enum | str, enum_type: type[Enum], field_name: str) -> Enum:
    if isinstance(value, enum_type):
        return value
    raw_value = value.value if isinstance(value, Enum) else value
    try:
        return enum_type(raw_value)
    except ValueError as exc:
        raise FeatureStoreDefinitionError(f"{field_name} is not a valid {enum_type.__name__}") from exc


def _to_jsonable(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, tuple):
        return [_to_jsonable(item) for item in value]
    if isinstance(value, list):
        return [_to_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _to_jsonable(item) for key, item in value.items()}
    return value


__all__ = [
    "ContextViewDefinition",
    "Definition",
    "EntityDefinition",
    "FeatureDefinition",
    "FeatureFreshnessPolicy",
    "FeatureStalenessAction",
    "FeatureStoreDefinitionError",
    "FeatureValueType",
    "LineageReference",
    "TransformationDefinition",
    "TransformationKind",
    "definition_from_payload",
    "definition_to_payload",
    "definition_type",
]
