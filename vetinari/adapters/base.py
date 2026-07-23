"""Base provider adapter interface for multi-LLM orchestration."""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, TypeAlias

from vetinari.adapters.base_telemetry import (
    AdapterCostEntry,
    get_anomaly_detector,
    get_cost_tracker,
    get_forecaster,
    get_genai_tracer,
    get_sla_tracker,
    get_telemetry_collector,
    log_event,
    record_model_call_failure,
)
from vetinari.constants import (
    INFERENCE_STATUS_ERROR,
    INFERENCE_STATUS_OK,
    MODEL_SCORE_WEIGHT_CAPABILITY,
    MODEL_SCORE_WEIGHT_CONTEXT,
    MODEL_SCORE_WEIGHT_COST,
    MODEL_SCORE_WEIGHT_FREE_TIER,
    MODEL_SCORE_WEIGHT_LATENCY,
)
from vetinari.types import ModelProvider, PriorityClass

logger = logging.getLogger(__name__)


def _exact_response_tokens(response: InferenceResponse) -> tuple[int, int]:
    """Return typed engine token counts, failing closed when either is absent."""
    if response.input_tokens is None or response.output_tokens is None:
        logger.warning("Inference response omitted exact input/output token counts; recording zero cost")
        return 0, 0
    return max(0, response.input_tokens), max(0, response.output_tokens)


def _record_model_call_failure_metric(
    *,
    project_id: str,
    task_id: str,
    agent_type: str,
    model_id: str,
    failure_class: str,
) -> None:
    """Record a failed model-call metric through the live metrics module."""
    record_model_call_failure(
        project_id=project_id,
        task_id=task_id,
        agent_type=agent_type,
        model_id=model_id,
        failure_class=failure_class,
    )


# Canonical enum lives in vetinari.types.ModelProvider.
# Domain alias — adapters use ``ProviderType`` throughout.
ProviderType: TypeAlias = ModelProvider


@dataclass(frozen=True, slots=True)
class ProviderConfig:
    """Configuration for a provider."""

    provider_type: ProviderType
    name: str
    endpoint: str
    api_key: str | None = None
    max_retries: int = 3
    timeout_seconds: int = 120
    memory_budget_gb: int = 32
    extra_config: dict[str, Any] = field(default_factory=dict)

    def __repr__(self) -> str:
        return f"ProviderConfig(name={self.name!r}, provider_type={self.provider_type!r}, endpoint={self.endpoint!r})"


@dataclass
class ProviderModelInfo:
    """Information about a model available from a provider."""

    id: str
    name: str
    provider: str
    endpoint: str
    capabilities: list[str]
    context_len: int
    memory_gb: int
    version: str
    latency_estimate_ms: int = 1000
    throughput_tokens_per_sec: float = 50.0
    cost_per_1k_tokens: float = 0.0
    free_tier: bool = False
    tags: list[str] = field(default_factory=list)

    def __repr__(self) -> str:
        return f"ModelInfo(id={self.id!r}, provider={self.provider!r}, context_len={self.context_len!r})"


ModelInfo = ProviderModelInfo


def derive_model_cache_maxsize(
    config: ProviderConfig,
    *,
    model_memory_gb: int | float | None = None,
    default_model_memory_gb: int = 4,
) -> int:
    """Derive a bounded local model cache size from provider memory budget.

    Returns:
        Maximum number of local models that fit within the configured memory budget.
    """
    budget_gb = max(1, int(getattr(config, "memory_budget_gb", 1) or 1))
    configured_model_gb = model_memory_gb
    if configured_model_gb is None:
        configured_model_gb = config.extra_config.get("memory_gb", default_model_memory_gb)
    try:
        per_model_gb = max(1, int(float(configured_model_gb)))
    except (TypeError, ValueError):
        per_model_gb = max(1, int(default_model_memory_gb))
    return max(1, budget_gb // per_model_gb)


@dataclass
class InferenceRequest:
    """Request to run inference on a model.

    Fields beyond model_id and prompt are optional sampling parameters.
    Sentinel values (None, -1, 0.0) indicate "use model/profile default".
    """

    model_id: str
    prompt: str
    system_prompt: str | None = None
    max_tokens: int = 2048
    temperature: float = 0.7
    top_p: float = 0.9
    top_k: int = 40
    stop_sequences: list[str] = field(default_factory=list)
    repeat_penalty: float = 1.1  # Penalize token repetition (1.0 = disabled, >1.0 = penalize)
    frequency_penalty: float = 0.0  # Penalize frequent tokens (0.0 = disabled)
    # -- Extended sampling parameters (Phase B, Session 11) --
    min_p: float = 0.0  # Minimum probability threshold (0.0 = disabled, 0.05 = recommended)
    presence_penalty: float = 0.0  # Penalize tokens already present (0.0 = disabled)
    mirostat_mode: int = 0  # Mirostat sampling mode (0=disabled, 1=v1, 2=v2)
    mirostat_tau: float = 5.0  # Mirostat target entropy
    mirostat_eta: float = 0.1  # Mirostat learning rate
    seed: int = -1  # RNG seed (-1 = random)
    response_format: str | None = None  # "json" for structured output
    grammar: str | None = None  # BNF grammar string for constrained generation
    task_type: str | None = None  # Task type key for automatic grammar selection
    logit_bias: dict[int, float] | None = None  # Token ID -> bias adjustments
    typical_p: float = 0.0  # Locally typical sampling (0.0 = disabled, 1.0 = default)
    tfs_z: float = 0.0  # Tail-free sampling z parameter (0.0 = disabled, 1.0 = default)
    # FSA-0047: optional image inputs for multimodal (vision) models.  Each
    # entry is either a data: URL ("data:image/png;base64,...") or a file://
    # path.  Text-only adapters MUST reject requests where this list is
    # non-empty (see llama_cpp_adapter_inference._build_messages).
    images: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    priority_class: PriorityClass | str | None = None
    eval_slot: int | None = None
    session_id: str | None = None
    prefix_refs: list[str] = field(default_factory=list)
    dry_multiplier: float | None = None
    dry_base: float | None = None
    dry_allowed_length: int | None = None
    xtc_probability: float | None = None
    xtc_threshold: float | None = None
    top_n_sigma: float | None = None

    def __repr__(self) -> str:
        return f"InferenceRequest(model_id={self.model_id!r}, max_tokens={self.max_tokens!r}, stream=False)"


@dataclass
class InferenceResponse:
    """Response from inference."""

    model_id: str
    output: str
    latency_ms: int
    tokens_used: int
    status: str  # Use INFERENCE_STATUS_* constants from vetinari.constants
    error: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    confidence: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Coerce output to str to prevent callers crashing on .strip() or string ops.

        None becomes "".  Lists are joined with newlines.  Any other non-string
        type is coerced via str() so callers always receive a valid string.
        """
        if self.output is None:
            self.output = ""
        elif isinstance(self.output, list):
            self.output = "\n".join(str(item) for item in self.output)
        elif not isinstance(self.output, str):
            self.output = str(self.output)

    def __repr__(self) -> str:
        return (
            f"InferenceResponse(model_id={self.model_id!r}, status={self.status!r}, "
            f"tokens_used={self.tokens_used!r}, content={self.output[:50]!r})"
        )


class ProviderAdapter(ABC):
    """Abstract base class for all provider adapters.

    Also known as: LLM Bridge — translates between Vetinari's internal
    InferenceRequest / InferenceResponse contracts and the wire format
    expected by a specific LLM provider (llama-cpp-python, LiteLLM, NIM, etc.).

    Each concrete subclass handles one provider type and implements a
    consistent interface for:
    - Model discovery
    - Health checks
    - Inference execution
    - Capability querying
    """

    def __init__(self, config: ProviderConfig):
        """Store provider configuration and unpack frequently-accessed fields as attributes.

        Args:
            config: The provider configuration specifying endpoint, credentials, and limits.
        """
        self.config = config
        self.provider_type = config.provider_type
        self.name = config.name
        self.endpoint = config.endpoint
        self.api_key = config.api_key
        self.max_retries = config.max_retries
        self.timeout_seconds = config.timeout_seconds
        self.models: list[ModelInfo] = []

    @abstractmethod
    def discover_models(self) -> list[ModelInfo]:
        """Discover available models from the provider.

        Returns:
            List of ModelInfo objects representing available models.
        """

    @abstractmethod
    def health_check(self) -> dict[str, Any]:
        """Check health/status of the provider.

        Returns:
            Dict with keys: {"healthy": bool, "reason": str, "timestamp": str}
        """

    @abstractmethod
    def infer(self, request: InferenceRequest) -> InferenceResponse:
        """Run inference on a model.

        Args:
            request: InferenceRequest with model_id, prompt, and options

        Returns:
            InferenceResponse with output, latency, tokens_used, status
        """

    def _emit_inference_started(self, request: InferenceRequest) -> None:
        """Emit an inference_started structured-log event before inference begins.

        Must be called at the top of each concrete adapter's infer() method, before
        any model API call, so that inference_started is always emitted prior to
        inference_completed (which fires via _record_telemetry after the call).
        Failures are silently suppressed so telemetry never blocks inference.

        Args:
            request: The InferenceRequest about to be submitted.
        """
        try:
            log_event(
                "debug",
                "vetinari.adapters.base",
                "inference_started",
                model_id=request.model_id,
            )
        except Exception:  # Broad: telemetry is best-effort; never blocks inference
            logger.warning(
                "Failed to emit inference_started structured event for %s",
                request.model_id,
                exc_info=True,
            )

    @abstractmethod
    def get_capabilities(self) -> dict[str, list[str]]:
        """Get capabilities of all available models.

        Returns:
            Dict mapping model_id to list of capabilities
            (e.g., ["code_gen", "chat", "summarization"])
        """

    def score_model_for_task(self, model: ModelInfo, task_requirements: dict[str, Any]) -> float:
        """Score a model for a given task.

        Factors: capability match, context fit, latency, cost

        Args:
            model: ModelInfo to score
            task_requirements: Dict with keys like "required_capabilities", "input_tokens", "max_latency_ms"

        Returns:
            Score between 0 and 1 (higher is better)
        """
        score = 0.0

        # Capability match (35%)
        required_caps = set(task_requirements.get("required_capabilities", []))
        model_caps = set(model.capabilities)
        if required_caps:
            cap_match = len(required_caps & model_caps) / len(required_caps)
        else:
            cap_match = 1.0
        score += cap_match * MODEL_SCORE_WEIGHT_CAPABILITY

        # Context fit (20%)
        input_tokens = task_requirements.get("input_tokens", 1000)
        if input_tokens <= model.context_len:
            context_fit = 1.0
        else:
            context_fit = max(0.0, model.context_len / input_tokens)
        score += context_fit * MODEL_SCORE_WEIGHT_CONTEXT

        # Latency (20%)
        max_latency_ms = task_requirements.get("max_latency_ms", 30000)
        if model.latency_estimate_ms <= max_latency_ms:
            latency_score = 1.0
        else:
            latency_score = max(0.0, 1.0 - (model.latency_estimate_ms - max_latency_ms) / max_latency_ms)
        score += latency_score * MODEL_SCORE_WEIGHT_LATENCY

        # Cost (15%)
        max_cost = task_requirements.get("max_cost_per_1k_tokens", 0.1)
        if model.cost_per_1k_tokens <= max_cost:
            cost_score = 1.0
        else:
            cost_score = max(0.0, 1.0 - (model.cost_per_1k_tokens / max_cost))
        score += cost_score * MODEL_SCORE_WEIGHT_COST

        # Free tier bonus (10%)
        if model.free_tier:
            score += MODEL_SCORE_WEIGHT_FREE_TIER

        return float(min(1.0, score))

    def _record_telemetry(self, request: InferenceRequest, response: InferenceResponse) -> None:
        """Record inference telemetry to all analytics/learning modules.

        Called automatically after each infer() call in concrete adapters.
        Failures are silently suppressed — telemetry must never crash inference.
        """
        provider = self.provider_type.value
        try:
            genai_tracer = get_genai_tracer()
            llm_span = genai_tracer.start_agent_span(agent_name="llm", operation="inference", model=request.model_id)
            llm_span.attributes["latency_ms"] = response.latency_ms
            llm_span.attributes["gen_ai.usage.input_tokens"] = getattr(response, "input_tokens", 0)
            llm_span.attributes["gen_ai.usage.output_tokens"] = getattr(response, "output_tokens", 0) or 0
            llm_span.attributes["gen_ai.response.model"] = getattr(response, "model_id", request.model_id)
            genai_tracer.end_agent_span(
                llm_span,
                status=INFERENCE_STATUS_OK if response.status == INFERENCE_STATUS_OK else INFERENCE_STATUS_ERROR,
                tokens_used=response.tokens_used,
            )
        except Exception:
            logger.warning("GenAI tracer unavailable for LLM inference span", exc_info=True)
        try:
            input_tokens, output_tokens = _exact_response_tokens(response)
            log_event(
                "info" if response.status == INFERENCE_STATUS_OK else "warning",
                "vetinari.adapters.base",
                "inference_completed",
                model_id=request.model_id,
                latency_ms=response.latency_ms,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                status="completed" if response.status == INFERENCE_STATUS_OK else "failed",
            )
        except Exception:
            logger.warning(
                "Failed to emit inference_completed structured event for %s", request.model_id, exc_info=True
            )
        try:
            get_telemetry_collector().record_adapter_latency(
                provider=provider,
                model=request.model_id,
                latency_ms=response.latency_ms,
                tokens_used=response.tokens_used,
                success=response.status == INFERENCE_STATUS_OK,
            )
        except Exception:
            logger.warning("Failed to record adapter telemetry for %s", request.model_id, exc_info=True)
        if response.status != INFERENCE_STATUS_OK:
            metadata = request.metadata or {}
            _record_model_call_failure_metric(
                project_id=str(metadata.get("project_id") or "unknown"),
                task_id=str(metadata.get("task_id") or "unknown"),
                agent_type=str(metadata.get("agent_type") or metadata.get("agent") or "unknown"),
                model_id=request.model_id,
                failure_class=str(response.error or response.status or "failed"),
            )
        try:
            input_tokens, output_tokens = _exact_response_tokens(response)
            metadata = request.metadata or {}
            get_cost_tracker().record(
                AdapterCostEntry(
                    provider=provider,
                    model=request.model_id,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    agent=metadata.get("agent"),
                    task_id=metadata.get("task_id"),
                    latency_ms=float(response.latency_ms),
                )
            )
        except Exception:
            logger.warning("Failed to record cost tracking entry for %s", request.model_id, exc_info=True)
        try:
            tracker = get_sla_tracker()
            tracker.record_latency(
                f"{provider}:{request.model_id}",
                latency_ms=float(response.latency_ms),
                success=response.status == INFERENCE_STATUS_OK,
            )
            tracker.record_request(success=response.status == INFERENCE_STATUS_OK)
        except Exception:
            logger.warning("Failed to record SLA metrics for %s", request.model_id, exc_info=True)
        try:
            forecaster = get_forecaster()
            forecaster.ingest("adapter.latency", float(response.latency_ms))
            forecaster.ingest("adapter.tokens", float(response.tokens_used or 0))
        except Exception:
            logger.warning("Failed to ingest forecaster data for %s", request.model_id, exc_info=True)
        try:
            result = get_anomaly_detector().detect("adapter.latency", float(response.latency_ms))
            if result.is_anomaly:
                logger.warning(
                    "Anomaly detected: %s=%s (%s, score=%.2f)",
                    result.metric,
                    result.value,
                    result.method,
                    result.score,
                )
        except Exception:
            logger.warning("Failed to run anomaly detection for %s", request.model_id, exc_info=True)

    async def async_infer(self, request: InferenceRequest) -> InferenceResponse:
        """Run inference asynchronously.

        Default implementation wraps the synchronous ``infer()`` in an executor.
        Subclasses with native async support should override this method.

        Args:
            request: InferenceRequest with model_id, prompt, and options.

        Returns:
            InferenceResponse with output, latency, tokens_used, status.
        """
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self.infer, request)

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(provider={self.provider_type.value}, endpoint={self.endpoint})"


async def run_async_infer(
    adapter: ProviderAdapter,
    request: InferenceRequest,
) -> InferenceResponse:
    """Convenience function to run async inference on an adapter.

    Args:
        adapter: The provider adapter to use.
        request: The inference request.

    Returns:
        InferenceResponse from the adapter.
    """
    return await adapter.async_infer(request)
