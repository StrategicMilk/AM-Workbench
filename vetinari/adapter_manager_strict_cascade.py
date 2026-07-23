"""Strict-capability cascade routing mixin for :mod:`vetinari.adapter_manager`.

Extracted from ``adapter_manager.py`` to keep that module under the 500-LOC
target.  The three methods here extend the base ``_AdapterManagerCascadeMixin``
(in ``adapter_manager_cascade.py``) with support-matrix validation, endpoint
capability proof, and durable route-receipt persistence.

``_AdapterManagerStrictCascadeMixin`` is intended to be mixed into
``AdapterManager`` only.  It relies on the following attributes being present
on the host class:

    _cascade_enabled: bool — whether cascade routing is active.
    _cascade_router: CascadeRouter | None — the active router instance.
    _cascade_provider: str | None — default provider for cascade calls.
    _cascade_requires_support_matrix: bool — whether support-matrix proof is required.
    _support_matrix_path: pathlib.Path — path to the support_matrix.yaml file.
    _route_receipt_path: pathlib.Path | None — JSONL store for route receipts.
    _provider_fallback_order: list[str] — registered provider names in priority order.
    _metrics: dict[str, ProviderMetrics] — per-provider metrics tracking.
    _metrics_lock: threading.Lock — guard for metrics updates.
    get_provider: method returning ProviderAdapter | None by name.
    set_route_receipt_store: method that stores the receipt path.
    enable_cascade_routing: the base mixin's version (called as a delegate).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from vetinari.constants import INFERENCE_STATUS_ERROR, INFERENCE_STATUS_OK

if TYPE_CHECKING:
    from vetinari.adapters.base import InferenceRequest, InferenceResponse

logger = logging.getLogger(__name__)

DEVELOPER_WORKFLOW_CONTRACT_ID = "RCG-0014-P11"
STRICT_CASCADE_WORKFLOW_GUARDS: tuple[str, ...] = (
    "strict routing requires support-matrix proof",
    "strict routing requires a route receipt store before inference",
    "unavailable providers return error responses instead of fallback success",
    "receipt persistence failures return error responses",
)


def developer_workflow_contract() -> dict[str, object]:
    """Return the strict cascade workflow guarantees verified by this follow-up pack."""
    return {
        "pack": DEVELOPER_WORKFLOW_CONTRACT_ID,
        "surface": "vetinari/adapter_manager_strict_cascade.py",
        "guards": STRICT_CASCADE_WORKFLOW_GUARDS,
    }


class _AdapterManagerStrictCascadeMixin:
    """Strict-capability cascade routing extension for ``AdapterManager``.

    Extends ``_AdapterManagerCascadeMixin`` with support-matrix validation,
    endpoint capability proof, and durable route-receipt persistence.  Intended
    to be mixed into ``AdapterManager`` only.  Do not instantiate directly.
    """

    if TYPE_CHECKING:
        _cascade_requires_support_matrix: Any
        _cascade_router: Any
        _metrics: Any
        _metrics_lock: Any
        _provider_fallback_order: Any
        _route_receipt_path: Any
        _support_matrix_path: Any
        get_provider: Any
        set_route_receipt_store: Any

    def configure_default_cascade_routing(
        self,
        *,
        model_families_path: str | Path = "config/model_families.yaml",
        support_matrix_path: str | Path = "config/support_matrix.yaml",
        provider_name: str | None = None,
        receipt_path: str | Path | None = None,
        confidence_threshold: float = 0.7,
        max_escalations: int = 2,
    ) -> None:
        """Enable the default cascade from strict endpoint capability records.

        Loads endpoint capability records for each model family, validates that
        every record matches the current support-matrix version, then calls
        ``enable_cascade_routing`` with the resulting tier list.

        Args:
            model_families_path: Path to the model families YAML config.
            support_matrix_path: Path to the support matrix YAML config.
            provider_name: Provider instance to route cascade calls through.
            receipt_path: JSONL path for route-receipt persistence.
            confidence_threshold: Minimum confidence before escalation stops.
            max_escalations: Maximum lower-confidence escalations to attempt.

        Raises:
            CapabilityContractError: If any endpoint capability record's
                ``support_matrix_version`` mismatches the current matrix.
        """
        from vetinari.models.inference_endpoint_capabilities import (
            load_endpoint_capability_records,
            read_support_matrix_version,
        )

        self._support_matrix_path = Path(support_matrix_path)  # type: ignore[attr-defined]
        support_matrix_version = read_support_matrix_version(self._support_matrix_path)  # type: ignore[attr-defined]
        if receipt_path is not None:
            self.set_route_receipt_store(receipt_path)  # type: ignore[attr-defined]
        records = load_endpoint_capability_records(model_families_path)
        mismatched = sorted(
            record.model_id for record in records if record.support_matrix_version != support_matrix_version
        )
        if mismatched:
            from vetinari.models.inference_endpoint_capabilities import CapabilityContractError

            raise CapabilityContractError(
                "endpoint capability records mismatch support matrix version "
                f"{support_matrix_version!r}: {', '.join(mismatched)}"
            )
        tiers = [record.to_tier() for record in records]
        self.enable_cascade_routing(  # type: ignore[attr-defined]
            tiers,
            provider_name=provider_name,
            confidence_threshold=confidence_threshold,
            max_escalations=max_escalations,
            strict_capabilities=True,
        )

    def enable_cascade_routing(
        self,
        tiers: list[dict[str, float | str]] | list[dict[str, Any]],
        provider_name: str | None = None,
        confidence_threshold: float = 0.7,
        max_escalations: int = 2,
        *,
        strict_capabilities: bool = False,
    ) -> None:
        """Configure cascade routing, optionally requiring endpoint capability proof.

        When ``strict_capabilities=True``, validates each tier dict against the
        endpoint capability records and raises if any tier lacks proof.  Then
        delegates to ``_AdapterManagerCascadeMixin.enable_cascade_routing`` to
        build the ``CascadeRouter`` and set the mixin state.

        Args:
            tiers: Cascade tier descriptors ordered by model cost after validation.
            provider_name: Optional provider instance to use for each tier.
            confidence_threshold: Minimum confidence before escalation stops.
            max_escalations: Maximum lower-confidence escalations to attempt.
            strict_capabilities: Require endpoint capability and freshness proof.
        """
        from vetinari.adapter_manager_cascade import _AdapterManagerCascadeMixin
        from vetinari.models.inference_endpoint_capabilities import validate_cascade_tiers

        normalized = validate_cascade_tiers(tiers, strict_capabilities=strict_capabilities)
        _AdapterManagerCascadeMixin.enable_cascade_routing(
            self,  # type: ignore[arg-type]
            normalized,
            provider_name=provider_name,
            confidence_threshold=confidence_threshold,
            max_escalations=max_escalations,
        )
        self._cascade_requires_support_matrix = strict_capabilities  # type: ignore[attr-defined]

    @staticmethod
    def _cascade_error_response(request: InferenceRequest, message: str) -> InferenceResponse:
        """Build a fail-closed cascade error response.

        Args:
            request: The original InferenceRequest (model_id preserved).
            message: Human-readable reason the cascade could not proceed.

        Returns:
            InferenceResponse with status=error and empty output.
        """
        from vetinari.adapters.base import InferenceResponse

        return InferenceResponse(
            model_id=request.model_id,
            output="",
            latency_ms=0,
            tokens_used=0,
            status=INFERENCE_STATUS_ERROR,
            error=message,
        )

    def _infer_via_cascade(
        self,
        request: InferenceRequest,
        provider_name: str | None,
    ) -> InferenceResponse:
        """Run a strict cascade route and persist a redacted route receipt.

        Validates the support-matrix version when strict mode is active, runs
        ``CascadeRouter.route()``, appends a redacted receipt to the configured
        JSONL store, and annotates the response metadata with routing details.

        Args:
            request: The original InferenceRequest.
            provider_name: Provider instance name for all tier calls.

        Returns:
            InferenceResponse from the winning cascade tier, with route
            metadata attached.
        """
        from vetinari.models.inference_endpoint_capabilities import (
            CapabilityContractError,
            append_route_receipt,
            build_route_receipt,
            read_support_matrix_version,
        )

        support_matrix_version = ""
        if self._cascade_requires_support_matrix:  # type: ignore[attr-defined]
            if self._route_receipt_path is None:  # type: ignore[attr-defined]
                return self._cascade_error_response(request, "Cascade routing: route receipt store not configured")
            try:
                support_matrix_version = read_support_matrix_version(self._support_matrix_path)  # type: ignore[attr-defined]
            except CapabilityContractError as exc:
                logger.warning("cascade routing support proof unavailable: %s", exc)
                return self._cascade_error_response(request, f"Cascade routing support proof unavailable: {exc}")

        prov_name = provider_name
        if not prov_name and self._provider_fallback_order:  # type: ignore[attr-defined]
            prov_name = self._provider_fallback_order[0]  # type: ignore[attr-defined]
        if prov_name is None:
            return self._cascade_error_response(request, "Cascade routing: no provider configured")

        adapter = self.get_provider(prov_name)  # type: ignore[attr-defined]
        if adapter is None:
            return self._cascade_error_response(request, "Cascade routing: no provider configured")

        def _adapter_fn(tier_request: InferenceRequest) -> InferenceResponse:
            return adapter.infer(tier_request)

        try:
            cascade_result = self._cascade_router.route(request, adapter_fn=_adapter_fn)  # type: ignore[attr-defined]
        except Exception as exc:
            logger.warning("cascade routing failed: %s", exc)
            return self._cascade_error_response(request, f"Cascade routing failed: {exc}")

        response = cast("InferenceResponse", cascade_result.response)
        metrics = self._metrics.get(prov_name)  # type: ignore[attr-defined]
        provider_health = metrics.to_dict() if metrics is not None else {"status": "unknown"}
        fallback_reason = "confidence_below_threshold" if cascade_result.escalation_count else None
        if self._route_receipt_path is not None:  # type: ignore[attr-defined]
            receipt = build_route_receipt(
                request_model_id=request.model_id,
                prompt=request.prompt,
                tiers_tried=cascade_result.tiers_tried,
                accepted_tier=cascade_result.model_id,
                confidence=cascade_result.confidence,
                confidence_source="strict_cascade_router.score",
                confidence_calibration_ref=f"support-matrix:{support_matrix_version or 'unchecked'}",
                eval_feedback_refs=[
                    f"provider-health:{prov_name or 'unknown'}",
                    f"fallback:{fallback_reason or 'none'}",
                ],
                cost_usd=cascade_result.cost_saved_vs_largest,
                fallback_reason=fallback_reason,
                provider_health=provider_health,
                support_matrix_version=support_matrix_version or "unchecked",
                readback_path=str(self._route_receipt_path),  # type: ignore[attr-defined]
            )
            try:
                append_route_receipt(self._route_receipt_path, receipt)  # type: ignore[attr-defined]
            except OSError as exc:
                logger.warning("cascade route receipt persistence failed: %s", exc)
                return self._cascade_error_response(request, f"Cascade routing receipt persistence failed: {exc}")
            response.metadata = {
                **(response.metadata or {}),
                "route_receipt_id": receipt.receipt_id,
                "route_receipt_path": str(self._route_receipt_path),  # type: ignore[attr-defined]
                "tiers_tried": list(cascade_result.tiers_tried),
                "accepted_tier": cascade_result.model_id,
                "route_confidence": cascade_result.confidence,
                "route_confidence_source": receipt.confidence_source,
                "route_confidence_calibration_ref": receipt.confidence_calibration_ref,
                "route_eval_feedback_refs": list(receipt.eval_feedback_refs),
                "fallback_reason": fallback_reason,
                "redaction_status": receipt.redaction_status,
                "support_matrix_version": receipt.support_matrix_version,
            }

        if prov_name and prov_name in self._metrics:  # type: ignore[attr-defined]
            metrics = self._metrics[prov_name]  # type: ignore[attr-defined]
            with self._metrics_lock:  # type: ignore[attr-defined]
                if getattr(response, "status", INFERENCE_STATUS_ERROR) == INFERENCE_STATUS_OK:
                    metrics.successful_inferences += 1
                    metrics.total_tokens_used += getattr(response, "tokens_used", 0)
                else:
                    metrics.failed_inferences += 1

        return response
