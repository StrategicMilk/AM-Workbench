"""Cloud adapter catalog helpers."""

from __future__ import annotations

from typing import Any

from .base import ProviderType
from .capabilities import default_backend_capabilities
from .registry import AdapterRegistry

_LOCAL_ONLY_PROVIDERS = {ProviderType.LOCAL}
DEVELOPER_WORKFLOW_CONTRACT_ID = "wave3-rcg0014p06-scope-followup-P09"
CLOUD_ADAPTER_WORKFLOW_GUARDS: tuple[str, ...] = (
    "local-only providers are rejected from cloud catalogs",
    "metadata mode returns adapter class and capability records",
    "catalog rows are sorted by provider id",
    "provider dispatch is sourced from the live adapter registry",
)


def developer_workflow_contract() -> dict[str, object]:
    """Return cloud-adapter catalog workflow guarantees verified by this follow-up pack."""
    return {
        "pack": DEVELOPER_WORKFLOW_CONTRACT_ID,
        "surface": "vetinari/adapters/cloud.py",
        "guards": CLOUD_ADAPTER_WORKFLOW_GUARDS,
    }


def get_cloud_catalog(*, include_metadata: bool = False) -> list[str] | list[dict[str, Any]]:
    """Return configured cloud adapter identifiers or capability records.

    Returns:
        List of advertised cloud provider ids by default. When
        ``include_metadata`` is true, returns records with adapter and
        capability metadata from the live registry.
    """
    capability_matrix = default_backend_capabilities()
    records: list[dict[str, Any]] = []
    for provider in AdapterRegistry.providers():
        if provider in _LOCAL_ONLY_PROVIDERS:
            continue
        adapter_cls = AdapterRegistry.dispatch(provider)
        capability = capability_matrix.get(provider)
        records.append({
            "provider": provider.value,
            "adapter_class": f"{adapter_cls.__module__}.{adapter_cls.__name__}",
            "capabilities": capability.to_dict() if capability else {},
            "catalog_source": "adapter_registry",
        })
    records = sorted(records, key=lambda item: item["provider"])
    if include_metadata:
        return records
    return [str(item["provider"]) for item in records]


__all__ = ["developer_workflow_contract", "get_cloud_catalog"]
