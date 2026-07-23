"""Support helpers for AKS-compatible bundle records."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from vetinari.workbench.knowledge.aks_bundle_records import AKSBundleProvenance


class BundleExportError(ValueError):
    """Raised when an AKS bundle cannot be serialized or trusted."""


class BundleAuthorityRefused(ValueError):
    """Raised when exporting would invent authority Workbench does not have."""

    def __init__(self, *, refused_field: str, reason: str) -> None:
        if not refused_field.strip():
            raise BundleExportError("refused_field must be non-empty")
        if not reason.strip():
            raise BundleExportError("reason must be non-empty")
        self.refused_field = refused_field
        self.reason = reason
        super().__init__(f"{refused_field}: {reason}")


def _record_payload(
    *,
    record_id_key: str,
    record_id: str,
    provenance_refs: tuple[AKSBundleProvenance, ...],
    **payload: Any,
) -> dict[str, Any]:
    out = {record_id_key: record_id, **payload, "provenance_refs": _provenance_payloads(provenance_refs)}
    if "metadata" in out:
        out["metadata"] = dict(out["metadata"])
    return out


def _provenance_payloads(refs: Iterable[AKSBundleProvenance]) -> list[dict[str, str]]:
    return [ref.to_payload() for ref in refs]


def _tuple_of(values: Iterable[Any], cls: type[Any], field_name: str) -> tuple[Any, ...]:
    result = tuple(values)
    if not all(isinstance(value, cls) for value in result):
        raise BundleExportError(f"{field_name} must contain {cls.__name__}")
    return result


def _require_sequence(payload: Mapping[str, Any], field_name: str) -> Sequence[Any]:
    value = payload.get(field_name)
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise BundleExportError(f"{field_name} must be a sequence")
    return value


def _require_mapping(payload: Mapping[str, Any], field_name: str) -> Mapping[str, Any]:
    value = payload.get(field_name) if isinstance(payload, Mapping) and field_name in payload else payload
    if not isinstance(value, Mapping):
        raise BundleExportError(f"{field_name} must be a mapping")
    return value


def _require_str(payload: Mapping[str, Any], field_name: str) -> str:
    if field_name not in payload:
        raise BundleExportError(f"{field_name} is required")
    value = payload[field_name]
    if not isinstance(value, str) or not value.strip():
        raise BundleExportError(f"{field_name} must be a non-empty string")
    return value


def _require_non_empty(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise BundleExportError(f"{field_name} must be non-empty")


def _require_bool(value: Any, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise BundleExportError(f"{field_name} must be bool")
    return value


def _string_mapping(value: Any, field_name: str) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise BundleExportError(f"{field_name} must be a mapping")
    return {str(key): str(item) for key, item in value.items()}


def _enum_or_value(value: Any) -> str:
    return value.value if isinstance(value, Enum) else str(value)
