"""Inference and request-identity mixin for OpenAIServerAdapter."""

from __future__ import annotations

import sys
import time
from collections.abc import Callable
from typing import Any

from vetinari.constants import INFERENCE_STATUS_ERROR, INFERENCE_STATUS_OK

from .base import InferenceRequest, InferenceResponse, ModelInfo, ProviderConfig, ProviderType
from .openai_server_adapter_helpers import (
    _DEFAULT_INFERENCE_TIMEOUT_S,
    _cache_safe_payload,
    _coerce_bool,
    _detect_provider_label,
    _get_semantic_cache,
    _httpx,
    _semantic_cache_identity,
    _stable_hash,
    logger,
)


def _httpx_runtime() -> Any:
    facade = sys.modules.get("vetinari.adapters.openai_server_adapter")
    facade_httpx = getattr(facade, "_httpx", None) if facade is not None else None
    if facade_httpx is not None and facade_httpx is not _httpx:
        return facade_httpx()
    return _httpx()


class OpenAIServerInferenceMixin:
    """Provide request construction, inference execution, and auth helpers."""

    _api_base: str
    _cache_is_stale: bool
    _discovered_models: list[ModelInfo]
    _emit_inference_started: Callable[[InferenceRequest], None]
    _gpu_only: bool
    _last_discovery_ts: float
    _raw_model_entries: dict[str, dict[str, Any]]
    _record_telemetry: Callable[[InferenceRequest, InferenceResponse], None]
    _semantic_cache_enabled: bool
    api_key: str | None
    config: ProviderConfig
    discover_models: Callable[[], list[ModelInfo]]
    name: str
    provider_type: ProviderType
    timeout_seconds: int

    def _provider_label(self) -> str:
        """Return the endpoint-aware provider label used for model metadata."""
        return _detect_provider_label(self._api_base, self.provider_type.value)

    def _raw_model_entry_for(self, model_id: str) -> dict[str, Any]:
        """Return provider-reported model metadata captured during discovery."""
        if model_id not in self._raw_model_entries and _coerce_bool(
            self.config.extra_config.get("refresh_model_identity_on_infer"), default=False
        ):
            try:
                self.discover_models()
            except Exception:
                logger.warning("Could not refresh model identity for %s", model_id, exc_info=True)
        return dict(self._raw_model_entries.get(model_id, {}))

    def _server_cache_payload_controls(self, request: InferenceRequest) -> dict[str, Any]:
        """Return cache-related request fields supported by this server backend."""
        controls: dict[str, Any] = {}
        metadata = request.metadata or {}
        extra = self.config.extra_config
        supports_cache_salt = self.provider_type.value == "vllm" or _coerce_bool(
            extra.get("supports_cache_salt", False),
            default=False,
        )

        if supports_cache_salt:
            cache_salt = metadata.get("cache_salt", extra.get("cache_salt"))
            if cache_salt not in (None, ""):
                controls["cache_salt"] = str(cache_salt)

        if self.provider_type.value == "vllm":
            for key in ("kv_transfer_params", "vllm_xargs"):
                value = metadata.get(key, extra.get(key))
                if value not in (None, ""):
                    controls[key] = value

        return controls

    def _cache_identity_for_request(
        self,
        request: InferenceRequest,
        payload: dict[str, Any],
    ) -> tuple[str, str, dict[str, Any]]:
        """Build this request's strict semantic-cache identity."""
        return _semantic_cache_identity(
            request=request,
            provider_type=self.provider_type.value,
            provider_name=self.name,
            provider_label=self._provider_label(),
            api_base=self._api_base,
            gpu_only=self._gpu_only,
            payload=payload,
            raw_model_entry=self._raw_model_entry_for(request.model_id),
            extra_config=self.config.extra_config,
        )

    def infer(self, request: InferenceRequest) -> InferenceResponse:
        """Send a chat completion request to the OpenAI-compatible endpoint.

        Args:
            request: InferenceRequest with model_id, prompt, and sampling params.

        Returns:
            InferenceResponse with output text, or an error response when the
            server is unreachable or returns zero choices.
        """
        self._emit_inference_started(request)
        self._warn_if_model_cache_is_stale()
        if not self._api_base:
            return self._no_endpoint_response(request)

        payload = self._build_completion_payload(request)
        server_cache_controls = self._server_cache_payload_controls(request)
        payload.update(server_cache_controls)
        cache_model_id, cache_context, cache_identity = self._cache_identity_parts(request, payload)

        cached_response = self._semantic_cache_response(
            request=request,
            cache_model_id=cache_model_id,
            cache_context=cache_context,
            cache_identity=cache_identity,
            server_cache_controls=server_cache_controls,
        )
        if cached_response is not None:
            return cached_response

        result, latency_ms, error_response = self._send_chat_completion(request, payload)
        if error_response is not None:
            return error_response

        response = self._response_from_completion_result(
            request=request,
            result=result,
            latency_ms=latency_ms,
            cache_identity=cache_identity,
            server_cache_controls=server_cache_controls,
        )
        self._record_telemetry(request, response)
        self._store_semantic_cache_response(request, response, cache_model_id, cache_context)
        return response

    def _warn_if_model_cache_is_stale(self) -> None:
        if not self._cache_is_stale:
            return
        age_s = int(time.time() - self._last_discovery_ts) if self._last_discovery_ts > 0 else 0
        logger.warning(
            "Serving stale model list for %s - last successful discovery was %ds ago",
            self.name,
            age_s,
        )

    def _no_endpoint_response(self, request: InferenceRequest) -> InferenceResponse:
        return InferenceResponse(
            model_id=request.model_id,
            output="",
            latency_ms=0,
            tokens_used=0,
            status=INFERENCE_STATUS_ERROR,
            error=f"No endpoint configured for {self.name}",
        )

    def _build_completion_payload(self, request: InferenceRequest) -> dict[str, Any]:
        messages: list[dict[str, str]] = []
        if request.system_prompt:
            messages.append({"role": "system", "content": request.system_prompt})
        messages.append({"role": "user", "content": request.prompt})

        payload: dict[str, Any] = {
            "model": request.model_id,
            "messages": messages,
            "max_tokens": request.max_tokens,
            "temperature": request.temperature,
            "top_p": request.top_p,
            "stream": False,
        }
        if request.stop_sequences:
            payload["stop"] = request.stop_sequences
        if request.frequency_penalty != 0.0:
            payload["frequency_penalty"] = request.frequency_penalty
        if request.presence_penalty != 0.0:
            payload["presence_penalty"] = request.presence_penalty
        if request.seed >= 0:
            payload["seed"] = request.seed
        if request.response_format == "json":
            payload["response_format"] = {"type": "json_object"}
        if request.logit_bias:
            payload["logit_bias"] = {str(k): v for k, v in request.logit_bias.items()}
        self._add_extended_sampler_fields(request, payload)
        return payload

    @staticmethod
    def _add_extended_sampler_fields(request: InferenceRequest, payload: dict[str, Any]) -> None:
        if request.top_k > 0:
            payload["top_k"] = request.top_k
        if request.repeat_penalty != 1.0:
            payload["repetition_penalty"] = request.repeat_penalty
        if request.min_p > 0.0:
            payload["min_p"] = request.min_p
        if request.typical_p not in (0.0, 1.0):
            payload["typical_p"] = request.typical_p
        if request.tfs_z not in (0.0, 1.0):
            payload["tfs_z"] = request.tfs_z
        if request.grammar:
            payload["grammar"] = request.grammar

    def _cache_identity_parts(
        self,
        request: InferenceRequest,
        payload: dict[str, Any],
    ) -> tuple[str, str, dict[str, Any]]:
        try:
            return self._cache_identity_for_request(request, payload)
        except Exception:
            logger.warning("Could not build server cache identity for %s", request.model_id, exc_info=True)
            return "", "", {}

    def _semantic_cache_response(
        self,
        *,
        request: InferenceRequest,
        cache_model_id: str,
        cache_context: str,
        cache_identity: dict[str, Any],
        server_cache_controls: dict[str, Any],
    ) -> InferenceResponse | None:
        if not (self._semantic_cache_enabled and cache_model_id and cache_context):
            return None
        try:
            cache = _get_semantic_cache()
            cached = cache.get(
                request.prompt,
                task_type=request.task_type or "",
                model_id=cache_model_id,
                system_prompt=cache_context,
            )
            if cached is None:
                return None
            logger.debug("Semantic cache hit for %s via %s", request.model_id, self.name)
            response = self._semantic_cache_hit_response(request, cached, cache_identity, server_cache_controls)
            self._record_telemetry(request, response)
            return response
        except Exception:
            logger.warning("Semantic cache unavailable for %s; proceeding without cache", self.name, exc_info=True)
            return None

    def _semantic_cache_hit_response(
        self,
        request: InferenceRequest,
        cached: str,
        cache_identity: dict[str, Any],
        server_cache_controls: dict[str, Any],
    ) -> InferenceResponse:
        return InferenceResponse(
            model_id=request.model_id,
            output=cached,
            latency_ms=0,
            tokens_used=0,
            status=INFERENCE_STATUS_OK,
            metadata={
                "cache_hit": True,
                "cache_tier": "semantic",
                "endpoint_sha256": cache_identity.get("endpoint_sha256", ""),
                "gpu_only": self._gpu_only,
                "model_entry_sha256": cache_identity.get("model_entry_sha256", ""),
                "provider": self.name,
                "provider_label": self._provider_label(),
                "provider_type": self.provider_type.value,
                "semantic_cache_enabled": True,
                "server_cache_controls": _cache_safe_payload(server_cache_controls),
                "server_identity_sha256": _stable_hash(cache_identity),
                "server_provenance_sha256": cache_identity.get("server_provenance_sha256", ""),
            },
        )

    def _send_chat_completion(
        self,
        request: InferenceRequest,
        payload: dict[str, Any],
    ) -> tuple[dict[str, Any], int, InferenceResponse | None]:
        httpx = _httpx_runtime()
        start_ms = time.perf_counter_ns() // 1_000_000
        url = f"{self._api_base}/v1/chat/completions"
        try:
            resp = httpx.post(
                url,
                json=payload,
                headers=self._auth_headers(),
                timeout=self.timeout_seconds or _DEFAULT_INFERENCE_TIMEOUT_S,
            )
            resp.raise_for_status()
        except Exception as exc:
            logger.warning("Exception handled by  send chat completion fallback", exc_info=True)
            latency_ms = (time.perf_counter_ns() // 1_000_000) - start_ms
            return {}, int(latency_ms), self._http_failure_response(request, int(latency_ms), exc)

        latency_ms = (time.perf_counter_ns() // 1_000_000) - start_ms
        return resp.json(), int(latency_ms), None

    def _http_failure_response(
        self,
        request: InferenceRequest,
        latency_ms: int,
        exc: Exception,
    ) -> InferenceResponse:
        logger.warning(
            "Inference failed on %s via %s - %s",
            request.model_id,
            self.name,
            exc,
        )
        response = InferenceResponse(
            model_id=request.model_id,
            output="",
            latency_ms=latency_ms,
            tokens_used=0,
            status=INFERENCE_STATUS_ERROR,
            error=f"Inference request to {self.name} failed: {exc}",
        )
        self._record_telemetry(request, response)
        return response

    def _response_from_completion_result(
        self,
        *,
        request: InferenceRequest,
        result: dict[str, Any],
        latency_ms: int,
        cache_identity: dict[str, Any],
        server_cache_controls: dict[str, Any],
    ) -> InferenceResponse:
        choices = result.get("choices", [])
        if not choices:
            return self._zero_choice_response(request, result, latency_ms)

        message = choices[0].get("message", {})
        usage = result.get("usage", {})
        return InferenceResponse(
            model_id=result.get("model", request.model_id),
            output=message.get("content", ""),
            latency_ms=latency_ms,
            tokens_used=usage.get("total_tokens", 0),
            status=INFERENCE_STATUS_OK,
            metadata=self._completion_metadata(result, usage, cache_identity, server_cache_controls),
        )

    def _zero_choice_response(
        self,
        request: InferenceRequest,
        result: dict[str, Any],
        latency_ms: int,
    ) -> InferenceResponse:
        logger.warning(
            "Inference on %s via %s returned zero choices - treating as error",
            request.model_id,
            self.name,
        )
        return InferenceResponse(
            model_id=result.get("model", request.model_id),
            output="",
            latency_ms=latency_ms,
            tokens_used=0,
            status=INFERENCE_STATUS_ERROR,
            error=f"Server returned zero choices for model {request.model_id} on {self.name}",
        )

    def _completion_metadata(
        self,
        result: dict[str, Any],
        usage: dict[str, Any],
        cache_identity: dict[str, Any],
        server_cache_controls: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "cache_hit": False,
            "completion_tokens": usage.get("completion_tokens", 0),
            "endpoint_sha256": cache_identity.get("endpoint_sha256", ""),
            "gpu_only": self._gpu_only,
            "model_entry_sha256": cache_identity.get("model_entry_sha256", ""),
            "prompt_tokens": usage.get("prompt_tokens", 0),
            "provider": self.name,
            "provider_label": self._provider_label(),
            "provider_type": self.provider_type.value,
            "semantic_cache_enabled": self._semantic_cache_enabled,
            "server_cache_controls": _cache_safe_payload(server_cache_controls),
            "server_identity_sha256": _stable_hash(cache_identity) if cache_identity else "",
            "server_model": result.get("model"),
            "server_provenance_sha256": cache_identity.get("server_provenance_sha256", ""),
            "system_fingerprint": result.get("system_fingerprint"),
        }

    def _store_semantic_cache_response(
        self,
        request: InferenceRequest,
        response: InferenceResponse,
        cache_model_id: str,
        cache_context: str,
    ) -> None:
        if not (self._semantic_cache_enabled and response.output and cache_model_id and cache_context):
            return
        try:
            _get_semantic_cache().put(
                request.prompt,
                response.output,
                model_id=cache_model_id,
                system_prompt=cache_context,
            )
        except Exception:
            logger.warning(
                "Semantic cache store failed for %s; result will not be cached",
                self.name,
                exc_info=True,
            )

    @property
    def gpu_only(self) -> bool:
        """Whether this backend requires models to fit entirely in VRAM."""
        return bool(self._gpu_only)

    def _auth_headers(self) -> dict[str, str]:
        """Build HTTP headers with optional Bearer auth.

        Returns:
            Headers dict with Content-Type and optional Authorization.
        """
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers


def build_fim_prompt(prefix: str, suffix: str) -> str:
    """Wrap prefix and suffix in fill-in-the-middle sentinel tokens.

    Used by ``/v1/completions`` when the request carries an OpenAI-style
    ``suffix`` field, so a single endpoint can serve both plain completion
    and FIM workflows without callers needing to construct the tokens
    themselves.

    Args:
        prefix: Text preceding the fill location.
        suffix: Text following the fill location.

    Returns:
        FIM-formatted prompt ``"<PRE>{prefix}<SUF>{suffix}<MID>"``.
    """
    return f"<PRE>{prefix}<SUF>{suffix}<MID>"
