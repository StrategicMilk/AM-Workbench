"""ComfyUI workflow adapter."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from vetinari.adapters.base import InferenceRequest, InferenceResponse, ModelInfo, ProviderConfig, ProviderType
from vetinari.boundary_guards import account_evidence_drop
from vetinari.constants import INFERENCE_STATUS_ERROR, INFERENCE_STATUS_OK

logger = logging.getLogger(__name__)
WorkflowExecutor = Callable[[Mapping[str, Any]], Mapping[str, Any]]


class ComfyUIAdapter:
    """HTTP-backed ComfyUI adapter with fail-closed offline behavior."""

    lease_class = "diffusion"

    def __init__(self, config: ProviderConfig):
        self.config = config
        self.provider_type = ProviderType.COMFYUI
        self.name = config.name
        self.endpoint = config.endpoint.rstrip("/")
        self._executor = config.extra_config.get("workflow_executor")

    def discover_models(self) -> list[ModelInfo]:
        """Expose a workflow endpoint only when a ComfyUI endpoint or executor is configured.

        Returns:
            Value produced for the caller.
        """
        if not self.endpoint and self._executor is None:
            return []
        return [
            ModelInfo(
                id=self.config.extra_config.get("workflow_model_id", "comfyui-workflow"),
                name="ComfyUI workflow endpoint",
                provider=self.provider_type.value,
                endpoint=self.endpoint,
                capabilities=["image_generation", "workflow_execution"],
                context_len=0,
                memory_gb=int(self.config.extra_config.get("memory_gb", 8)),
                version="local",
                tags=["diffusion", "comfyui"],
            )
        ]

    def health_check(self) -> dict[str, Any]:
        """Probe the configured ComfyUI endpoint without submitting work.

        Returns:
            Value produced for the caller.
        """
        if self._executor is not None:
            return {"healthy": True, "reason": "workflow_executor_configured", "timestamp": None}
        if not self.endpoint:
            return {"healthy": False, "reason": "endpoint_required", "timestamp": None}
        try:
            payload = self._http_json("GET", f"{self.endpoint}/system_stats")
        except Exception as exc:
            logger.warning(
                "ComfyUI health check failed for endpoint=%s; adapter reports unhealthy and will retry: %s",
                _safe_endpoint(self.endpoint),
                exc,
                exc_info=True,
            )
            return {"healthy": False, "reason": f"comfyui_probe_failed: {exc}", "timestamp": None}
        return {"healthy": True, "reason": "comfyui_reachable", "timestamp": None, "details": payload}

    def infer(self, request: InferenceRequest) -> InferenceResponse:
        """Submit a ComfyUI workflow JSON payload.

        Returns:
            Value produced for the caller.
        """
        start = time.monotonic()
        workflow = _workflow_from_request(request)
        if workflow is None:
            response = _error_response(request, "workflow_json_required", start)
            account_evidence_drop(response, "comfyui_telemetry", logger=logger)
            return response
        try:
            output = dict(self._executor(workflow)) if self._executor is not None else self._submit_workflow(workflow)
        except Exception as exc:
            logger.warning(
                "ComfyUI workflow submission failed for model_id=%s; returning failed inference response: %s",
                request.model_id,
                exc,
                exc_info=True,
            )
            response = _error_response(request, f"workflow_submission_failed: {exc}", start)
            account_evidence_drop(response, "comfyui_telemetry", logger=logger)
            return response
        response = InferenceResponse(
            model_id=request.model_id,
            output=json.dumps(output, sort_keys=True),
            latency_ms=_latency_ms(start),
            tokens_used=0,
            status=INFERENCE_STATUS_OK,
            metadata={"provider": self.provider_type.value, "endpoint": _safe_endpoint(self.endpoint)},
        )
        account_evidence_drop(response, "comfyui_telemetry", logger=logger)
        return response

    def get_capabilities(self) -> dict[str, list[str]]:
        """Return diffusion workflow capabilities exposed by this adapter."""
        return {self.provider_type.value: ["image_generation", "workflow_execution"]}

    def _submit_workflow(self, workflow: Mapping[str, Any]) -> dict[str, Any]:
        if not self.endpoint:
            raise ValueError("endpoint_required")
        return self._http_json("POST", f"{self.endpoint}/prompt", payload={"prompt": dict(workflow)})

    def _http_json(self, method: str, url: str, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
        _require_http_url(url)
        import httpx

        response = httpx.request(method, url, json=payload, timeout=self.config.timeout_seconds)
        response.raise_for_status()
        decoded = response.json() if response.content else {}
        if not isinstance(decoded, dict):
            raise ValueError("comfyui response must be a JSON object")
        return decoded


def _require_http_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("ComfyUI endpoint must be an http(s) URL")
    if parsed.username or parsed.password:
        raise ValueError("ComfyUI endpoint must not include credentials")


def _safe_endpoint(url: str) -> str:
    parsed = urlparse(url)
    netloc = parsed.hostname or ""
    if parsed.port is not None:
        netloc = f"{netloc}:{parsed.port}"
    safe_query = urlencode([
        (key, "[redacted]" if key.lower() in {"token", "api_key", "key", "secret", "password"} else value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
    ])
    return urlunparse((parsed.scheme, netloc, parsed.path, parsed.params, safe_query, ""))


def _workflow_from_request(request: InferenceRequest) -> dict[str, Any] | None:
    candidate = request.metadata.get("workflow") or request.prompt
    if isinstance(candidate, Mapping):
        return dict(candidate)
    if isinstance(candidate, str) and candidate.strip():
        try:
            decoded = json.loads(candidate)
        except json.JSONDecodeError:
            logger.warning(
                "ComfyUI workflow payload is not valid JSON; provide a JSON object for workflow execution",
                exc_info=True,
            )
            return None
        return decoded if isinstance(decoded, dict) else None
    return None


def _error_response(request: InferenceRequest, error: str, start: float) -> InferenceResponse:
    return InferenceResponse(
        model_id=request.model_id,
        output="",
        latency_ms=_latency_ms(start),
        tokens_used=0,
        status=INFERENCE_STATUS_ERROR,
        error=error,
    )


def _latency_ms(start: float) -> int:
    return max(0, int((time.monotonic() - start) * 1000))


__all__ = ["ComfyUIAdapter"]
