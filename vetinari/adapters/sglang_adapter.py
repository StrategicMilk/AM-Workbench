"""SGLang OpenAI-compatible adapter."""

from __future__ import annotations

from typing import Any

from vetinari.adapters.base import InferenceRequest, ModelInfo, ProviderConfig, ProviderType
from vetinari.adapters.openai_server_adapter import OpenAIServerAdapter


class BackendUnavailableError(RuntimeError):
    """Raised when a declared optional backend has no executable implementation."""


class SGLangAdapter(OpenAIServerAdapter):
    """OpenAI-compatible SGLang adapter with prefix-cache metadata."""

    def __init__(self, config: ProviderConfig):
        super().__init__(config)
        self.provider_type = ProviderType.SGLANG

    def tool_call_parser_kwargs(self) -> dict[str, Any]:
        """Return parser hints for SGLang-hosted tool calling."""
        return {"backend": "sglang", "prefix_cache": "radix_attention"}

    def prefix_cache_hint(self) -> str:
        """Return a routing hint for shared-prefix workloads."""
        return "prefer_for_shared_prefix"

    def health_probe(self) -> dict[str, Any]:
        """Probe the configured SGLang OpenAI-compatible endpoint.

        Returns:
            dict[str, Any] value produced by health_probe().
        """
        result = self.health_check()
        return {
            "passed": bool(result.get("healthy")),
            "reason": result.get("reason", ""),
            "timestamp": result.get("timestamp"),
        }


class UnavailableAdapter:
    """Fail-closed base for backends that have not been implemented yet.

    This class intentionally does not return empty model/capability lists or
    synthetic error responses. Those values are too easy for registries and
    training collectors to mistake for a real but idle backend.
    """

    lease_class = "none"
    unavailable_reason = "backend_not_implemented"

    def __init__(self, config: ProviderConfig):
        self.config = config
        self.provider_type = config.provider_type
        self.name = config.name
        self.endpoint = config.endpoint

    def discover_models(self) -> list[ModelInfo]:
        """Reject discovery because this is not a live adapter.

        Raises:
            BackendUnavailableError: Propagated when validation, persistence, or execution fails.
        """
        raise BackendUnavailableError(self.unavailable_reason)

    def health_check(self) -> dict[str, Any]:
        """Optional backend is unavailable unless explicitly configured."""
        return {"healthy": False, "reason": self.unavailable_reason, "timestamp": None}

    def infer(self, request: InferenceRequest) -> None:
        """Reject inference instead of fabricating a zero-token response.

        Raises:
            BackendUnavailableError: Propagated when validation, persistence, or execution fails.
        """
        raise BackendUnavailableError(self.unavailable_reason)

    def get_capabilities(self) -> dict[str, list[str]]:
        """Reject capability reads because there is no executable backend.

        Raises:
            BackendUnavailableError: Propagated when validation, persistence, or execution fails.
        """
        raise BackendUnavailableError(self.unavailable_reason)

    def is_available(self) -> bool:
        """Return explicit availability for UI/registry callers."""
        return False
