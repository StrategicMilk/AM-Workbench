"""Adapter builders for unified Workbench experiment variants."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from vetinari.workbench.experiments.model import ExperimentVariant, VariantKind
from vetinari.workbench.method_library import MethodKind
from vetinari.workbench.runs import RunMetric


def build_prompt_variant(
    variant_id: str,
    *,
    prompt_ref: str,
    parameters: Mapping[str, Any] | None = None,
    asset_refs: tuple[str, ...] = (),
) -> ExperimentVariant:
    return _build_variant(
        variant_id,
        VariantKind.PROMPT,
        {"prompt_ref": prompt_ref, **dict(parameters or {})},
        asset_refs=asset_refs or (prompt_ref,),
    )


def build_model_variant(
    variant_id: str,
    *,
    model_ref: str,
    parameters: Mapping[str, Any] | None = None,
    asset_refs: tuple[str, ...] = (),
) -> ExperimentVariant:
    return _build_variant(
        variant_id,
        VariantKind.MODEL,
        {"model_ref": model_ref, **dict(parameters or {})},
        asset_refs=asset_refs or (model_ref,),
    )


def build_retrieval_variant(
    variant_id: str,
    *,
    retrieval_ref: str,
    parameters: Mapping[str, Any] | None = None,
    asset_refs: tuple[str, ...] = (),
) -> ExperimentVariant:
    return _build_variant(
        variant_id,
        VariantKind.RETRIEVAL,
        {"retrieval_ref": retrieval_ref, **dict(parameters or {})},
        asset_refs=asset_refs or (retrieval_ref,),
    )


def build_route_variant(
    variant_id: str,
    *,
    route_ref: str,
    parameters: Mapping[str, Any] | None = None,
    asset_refs: tuple[str, ...] = (),
) -> ExperimentVariant:
    return _build_variant(
        variant_id,
        VariantKind.ROUTE,
        {"route_ref": route_ref, **dict(parameters or {})},
        asset_refs=asset_refs or (route_ref,),
    )


def build_runtime_variant(
    variant_id: str,
    *,
    runtime_ref: str,
    parameters: Mapping[str, Any] | None = None,
    asset_refs: tuple[str, ...] = (),
) -> ExperimentVariant:
    return _build_variant(
        variant_id,
        VariantKind.BACKEND,
        {"runtime_ref": runtime_ref, **dict(parameters or {})},
        asset_refs=asset_refs or (runtime_ref,),
    )


def build_backend_variant(
    variant_id: str,
    *,
    backend_ref: str,
    parameters: Mapping[str, Any] | None = None,
    asset_refs: tuple[str, ...] = (),
) -> ExperimentVariant:
    return _build_variant(
        variant_id,
        VariantKind.BACKEND,
        {"backend_ref": backend_ref, **dict(parameters or {})},
        asset_refs=asset_refs or (backend_ref,),
    )


def build_policy_variant(
    variant_id: str,
    *,
    policy_ref: str,
    parameters: Mapping[str, Any] | None = None,
    asset_refs: tuple[str, ...] = (),
) -> ExperimentVariant:
    return _build_variant(
        variant_id,
        VariantKind.POLICY,
        {"policy_ref": policy_ref, **dict(parameters or {})},
        asset_refs=asset_refs or (policy_ref,),
    )


def build_fine_tune_variant(
    variant_id: str,
    *,
    tuning_ref: str,
    parameters: Mapping[str, Any] | None = None,
    asset_refs: tuple[str, ...] = (),
) -> ExperimentVariant:
    return _build_variant(
        variant_id,
        VariantKind.FINE_TUNE,
        {"tuning_ref": tuning_ref, **dict(parameters or {})},
        asset_refs=asset_refs or (tuning_ref,),
    )


def method_kind_metric(kind: MethodKind) -> RunMetric:
    """Return the shared method-library evidence tag metric.

    Returns:
        RunMetric value produced by method_kind_metric().
    """
    if not isinstance(kind, MethodKind):
        kind = MethodKind(kind)
    return RunMetric(name="method_kind", value=1.0, unit=kind.value)


def _build_variant(
    variant_id: str,
    kind: VariantKind,
    parameters: Mapping[str, Any],
    *,
    asset_refs: tuple[str, ...],
) -> ExperimentVariant:
    return ExperimentVariant(
        variant_id=variant_id,
        kind=kind,
        parameters=dict(parameters),
        asset_refs=asset_refs,
    )


__all__ = [
    "build_backend_variant",
    "build_fine_tune_variant",
    "build_model_variant",
    "build_policy_variant",
    "build_prompt_variant",
    "build_retrieval_variant",
    "build_route_variant",
    "build_runtime_variant",
    "method_kind_metric",
]
