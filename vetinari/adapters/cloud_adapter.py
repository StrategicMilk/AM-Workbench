"""Native cloud provider adapter."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

from vetinari.boundary_guards import account_evidence_drop
from vetinari.constants import INFERENCE_STATUS_ERROR, INFERENCE_STATUS_OK
from vetinari.exceptions import ConfigurationError

from .base import InferenceRequest, InferenceResponse, ModelInfo, ProviderAdapter, ProviderConfig, ProviderType
from .cloud_clients.anthropic_client import get_anthropic_client
from .cloud_clients.google_client import get_google_client
from .cloud_clients.openai_client import get_openai_client

logger = logging.getLogger(__name__)

SUPPORTED_CLOUD_PROVIDERS = frozenset({ProviderType.OPENAI, ProviderType.ANTHROPIC, ProviderType.GEMINI})
CLOUD_EGRESS_MODES = frozenset({
    "local_only",
    "explicit_cloud",
    "local_first_with_fallback",
    "explicit_opt_in_only",
})
DEVELOPER_WORKFLOW_CONTRACT_ID = "wave3-rcg0014p06-scope-followup-P09"
CLOUD_ADAPTER_WORKFLOW_GUARDS: tuple[str, ...] = (
    "unsupported provider types raise configuration errors before inference",
    "cloud egress policy denies calls before SDK invocation unless the mode permits them",
    "native provider SDK clients are selected per OpenAI, Anthropic, and Gemini provider ids",
    "provider failures return typed error responses with cloud telemetry evidence",
)


def developer_workflow_contract() -> dict[str, object]:
    """Return cloud adapter workflow guarantees verified by the developer workflow pack."""
    return {
        "pack": DEVELOPER_WORKFLOW_CONTRACT_ID,
        "surface": "vetinari/adapters/cloud_adapter.py",
        "guards": CLOUD_ADAPTER_WORKFLOW_GUARDS,
    }


@dataclass(frozen=True)
class CloudEgressDecision:
    """Result of evaluating whether a cloud request may leave the machine."""

    allowed: bool
    reason: str


def _attr_or_mapping_value(source: object, name: str) -> object | None:
    if isinstance(source, dict):
        return source.get(name)
    return getattr(source, name, None)


def _messages_for_openai(request: InferenceRequest) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    if request.system_prompt:
        messages.append({"role": "system", "content": request.system_prompt})
    messages.append({"role": "user", "content": request.prompt})
    return messages


def _text_from_anthropic_content(content: object) -> str:
    if isinstance(content, str):
        return content
    pieces: list[str] = []
    for item in content if isinstance(content, list) else []:
        text = getattr(item, "text", None)
        if text is None and isinstance(item, dict):
            text = item.get("text")
        if text is not None:
            pieces.append(str(text))
    return "".join(pieces)


class CloudAdapter(ProviderAdapter):
    """Provider adapter backed by native cloud SDK clients."""

    workflow_contract = developer_workflow_contract()

    def __init__(self, config: ProviderConfig) -> None:
        super().__init__(config)
        if config.provider_type not in SUPPORTED_CLOUD_PROVIDERS:
            supported = ", ".join(sorted(provider.value for provider in SUPPORTED_CLOUD_PROVIDERS))
            raise ConfigurationError(
                f"CloudAdapter does not support provider type {config.provider_type.value!r}; "
                f"supported providers: {supported}"
            )
        self._egress_mode = self._resolve_egress_mode()

    def _resolve_egress_mode(self) -> str:
        policy = self.config.extra_config.get("routing_policy")
        raw = (
            self.config.extra_config.get("cloud_egress_mode")
            or _attr_or_mapping_value(policy, "cloud_egress_mode")
            or "explicit_opt_in_only"
        )
        mode = str(raw)
        if mode not in CLOUD_EGRESS_MODES:
            raise ConfigurationError(f"Unsupported cloud egress mode: {mode!r}")
        return mode

    def evaluate_cloud_egress(self, request: InferenceRequest | None = None) -> CloudEgressDecision:
        """Evaluate the pinned cloud-egress mode table without side effects.

        Returns:
            Decision describing whether the cloud call is allowed and why.
        """
        metadata = request.metadata if request is not None else {}
        if self._egress_mode == "local_only":
            return CloudEgressDecision(False, "local_only cloud_egress_mode blocks cloud provider calls")
        if self._egress_mode == "explicit_cloud":
            return CloudEgressDecision(True, "explicit_cloud selects cloud provider routing")
        if self._egress_mode == "local_first_with_fallback":
            if metadata.get("local_unavailable") is True or metadata.get("cloud_fallback_approved") is True:
                return CloudEgressDecision(True, "local_first_with_fallback approved after local path was unavailable")
            return CloudEgressDecision(
                False, "local_first_with_fallback requires local_unavailable or fallback approval"
            )
        if metadata.get("cloud_opt_in") is True or metadata.get("cloud_egress_approved") is True:
            return CloudEgressDecision(True, "explicit_opt_in_only request includes explicit cloud opt-in")
        return CloudEgressDecision(False, "explicit_opt_in_only requires request metadata cloud_opt_in=true")

    def _client(self) -> Any:
        injected = self.config.extra_config.get("client")
        if injected is not None:
            return injected
        factory = self.config.extra_config.get("client_factory")
        if callable(factory):
            return factory(self.config)
        if self.provider_type == ProviderType.OPENAI:
            return get_openai_client(
                api_key=self.api_key,
                base_url=self.endpoint or None,
                timeout_seconds=self.timeout_seconds,
            )
        if self.provider_type == ProviderType.ANTHROPIC:
            return get_anthropic_client(
                api_key=self.api_key,
                base_url=self.endpoint or None,
                timeout_seconds=self.timeout_seconds,
            )
        return get_google_client(api_key=self.api_key)

    def _call_provider(self, client: Any, request: InferenceRequest) -> tuple[str, int, int, int, dict[str, Any]]:
        if self.provider_type == ProviderType.OPENAI:
            response = client.chat.completions.create(
                model=request.model_id,
                messages=_messages_for_openai(request),
                temperature=request.temperature,
                max_tokens=request.max_tokens,
                top_p=request.top_p,
                stop=request.stop_sequences or None,
            )
            output = response.choices[0].message.content if response.choices else ""
            usage = getattr(response, "usage", None)
            input_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
            output_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
            total_tokens = int(getattr(usage, "total_tokens", input_tokens + output_tokens) or 0)
            return output or "", total_tokens, input_tokens, output_tokens, {"cloud_provider": "openai"}
        if self.provider_type == ProviderType.ANTHROPIC:
            kwargs: dict[str, Any] = {
                "model": request.model_id,
                "messages": [{"role": "user", "content": request.prompt}],
                "max_tokens": request.max_tokens,
                "temperature": request.temperature,
                "top_p": request.top_p,
            }
            if request.system_prompt:
                kwargs["system"] = request.system_prompt
            if request.stop_sequences:
                kwargs["stop_sequences"] = request.stop_sequences
            response = client.messages.create(**kwargs)
            usage = getattr(response, "usage", None)
            input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
            output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
            return (
                _text_from_anthropic_content(getattr(response, "content", [])),
                input_tokens + output_tokens,
                input_tokens,
                output_tokens,
                {"cloud_provider": "anthropic"},
            )
        contents = request.prompt if not request.system_prompt else f"{request.system_prompt}\n\n{request.prompt}"
        response = client.models.generate_content(
            model=request.model_id,
            contents=contents,
            config={
                "temperature": request.temperature,
                "max_output_tokens": request.max_tokens,
                "top_p": request.top_p,
            },
        )
        output = str(getattr(response, "text", "") or "")
        usage = getattr(response, "usage_metadata", None)
        input_tokens = int(getattr(usage, "prompt_token_count", 0) or 0)
        output_tokens = int(getattr(usage, "candidates_token_count", 0) or 0)
        total_tokens = int(getattr(usage, "total_token_count", input_tokens + output_tokens) or 0)
        return output, total_tokens, input_tokens, output_tokens, {"cloud_provider": "gemini"}

    def discover_models(self) -> list[ModelInfo]:
        """Return configured cloud model rows without live API discovery.

        Returns:
            ModelInfo rows declared in adapter configuration.
        """
        configured = self.config.extra_config.get("models") or []
        models: list[ModelInfo] = []
        for model in configured if isinstance(configured, list) else []:
            model_id = str(model.get("id") if isinstance(model, dict) else model)
            if not model_id:
                continue
            models.append(
                ModelInfo(
                    id=model_id,
                    name=model_id,
                    provider=self.provider_type.value,
                    endpoint=self.endpoint,
                    capabilities=["chat"],
                    context_len=int(model.get("context_len", 8192)) if isinstance(model, dict) else 8192,
                    memory_gb=0,
                    version="cloud",
                    tags=["cloud", self.provider_type.value],
                )
            )
        self.models = models
        return models

    def health_check(self) -> dict[str, Any]:
        """Report configuration health without sending a provider API request.

        Returns:
            Health payload for the configured cloud provider.
        """
        decision = self.evaluate_cloud_egress(None)
        return {
            "healthy": bool(self.api_key) and decision.allowed,
            "reason": "configured" if self.api_key and decision.allowed else decision.reason,
            "timestamp": time.time(),
            "endpoint": self.endpoint or self.provider_type.value,
            "provider": self.provider_type.value,
        }

    def infer(self, request: InferenceRequest) -> InferenceResponse:
        """Run a cloud completion after fail-closed egress validation.

        Returns:
            Inference response or typed egress/provider error response.
        """
        self._emit_inference_started(request)
        start = time.time()
        decision = self.evaluate_cloud_egress(request)
        if not decision.allowed:
            return InferenceResponse(
                model_id=request.model_id,
                output="",
                latency_ms=0,
                tokens_used=0,
                status=INFERENCE_STATUS_ERROR,
                error=decision.reason,
                metadata={"cloud_egress_mode": self._egress_mode, "cloud_provider": self.provider_type.value},
            )
        try:
            output, total_tokens, input_tokens, output_tokens, metadata = self._call_provider(self._client(), request)
            response = InferenceResponse(
                model_id=request.model_id,
                output=output,
                latency_ms=int((time.time() - start) * 1000),
                tokens_used=total_tokens,
                status=INFERENCE_STATUS_OK,
                metadata={
                    **metadata,
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "cloud_egress_mode": self._egress_mode,
                },
            )
            self._record_telemetry(request, response)
            return response
        except Exception:
            logger.exception(
                "[CloudAdapter] Inference failed for %s via %s", request.model_id, self.provider_type.value
            )
            error_item = InferenceResponse(
                model_id=request.model_id,
                output="",
                latency_ms=int((time.time() - start) * 1000),
                tokens_used=0,
                status=INFERENCE_STATUS_ERROR,
                error="Cloud inference failed - check server logs for details",
                metadata={"cloud_provider": self.provider_type.value, "cloud_egress_mode": self._egress_mode},
            )
            account_evidence_drop(error_item, "cloud_completion_telemetry", logger=logger)
            return error_item

    def stream_infer(self, request: InferenceRequest) -> InferenceResponse:
        """Return non-streaming output through the standard cloud call path."""
        return self.infer(request)

    def get_capabilities(self) -> dict[str, list[str]]:
        """Get capabilities of all discovered models."""
        return {model.id: model.capabilities for model in self.models}
