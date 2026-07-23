"""Pydantic validation schemas for ``config/models.yaml``.

Defines hardware profiles, local and cloud model entries, routing policy,
and the top-level :class:`ModelsConfig` that validates the full file.
Also provides the shared :func:`_load_yaml` helper used by all schema modules.
"""

from __future__ import annotations

import functools
import logging
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, model_validator

from vetinari.exceptions import ConfigurationError

logger = logging.getLogger(__name__)


# ── Shared helpers ─────────────────────────────────────────────────────────────


@functools.lru_cache(maxsize=32)  # Cache per path — config files rarely change at runtime
def _load_yaml(path: Path) -> dict[str, Any]:
    """Read and parse a YAML file.

    Args:
        path: Absolute path to the YAML file.

    Returns:
        Parsed YAML content as a plain dictionary.

    Raises:
        FileNotFoundError: If the file does not exist at ``path``.
        ValueError: If the file contains invalid YAML syntax.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise FileNotFoundError(f"Config file not found: {path}") from None
    try:
        return yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise ConfigurationError(f"Invalid YAML in {path}: {exc}") from exc


# ── models.yaml ───────────────────────────────────────────────────────────────


class HardwareConfig(BaseModel):
    """Hardware profile used by VRAMManager and model routing decisions.

    Attributes:
        gpu_vram_gb: Total GPU VRAM in gigabytes.
        system_ram_gb: Total system RAM in gigabytes.
        cpu_offload_enabled: Whether CPU-offload mode is permitted.
        max_cpu_offload_gb: RAM available for model CPU-offload (GB).
        throughput_estimates: Estimated tokens/sec keyed by size tier name.
    """

    gpu_vram_gb: float = Field(default=0.0, ge=0)
    system_ram_gb: float = Field(default=0.0, ge=0)
    cpu_offload_enabled: bool = False
    max_cpu_offload_gb: float = Field(default=0.0, ge=0)
    throughput_estimates: dict[str, float] = Field(default_factory=dict)


class LocalModelEntry(BaseModel):
    """A single local model definition (llama-cpp-python or server backend metadata).

    Attributes:
        model_id: Unique identifier used in API calls.
        provider: Provider slug (e.g. ``"local"``).
        display_name: Human-readable model label.
        capabilities: List of capability tags (e.g. ``["coding", "vision"]``).
        context_window: Maximum context length in tokens.
        latency_hint: Qualitative latency tier (``"fast"``, ``"medium"``, ``"slow"``).
        privacy_level: Data privacy level (``"local"`` or ``"public"``).
        memory_requirements_gb: Estimated VRAM / RAM required in GB.
        quantization: Quantization scheme (e.g. ``"q4_k_m"``).
        status: Operational status (e.g. ``"available"``).
        endpoint: URL template for the model's API endpoint.
        preferred_for: Task types this model is preferred for.
        requires_cpu_offload: Whether the model needs partial CPU offload.
    """

    model_id: str
    provider: str
    display_name: str
    capabilities: list[str] = Field(default_factory=list)
    context_window: int = Field(ge=1)
    latency_hint: str = "medium"
    privacy_level: str = "local"
    memory_requirements_gb: float = Field(default=0.0, ge=0)
    quantization: str = ""
    status: str = "available"
    endpoint: str = ""
    preferred_for: list[str] = Field(default_factory=list)
    requires_cpu_offload: bool = False


class CloudModelEntry(BaseModel):
    """A single cloud model definition (Anthropic, OpenAI, Gemini, etc.).

    Attributes:
        model_id: Unique identifier used in API calls.
        provider: Provider slug (e.g. ``"claude"``).
        display_name: Human-readable model label.
        capabilities: List of capability tags.
        context_window: Maximum context length in tokens.
        latency_hint: Qualitative latency tier.
        privacy_level: Data privacy level (typically ``"public"``).
        cost_per_1k_tokens: Cost in USD per 1 000 tokens.
        status: Operational status.
        endpoint: Full API endpoint URL.
        requires_env: Environment variable names that must be set.
        memory_requirements_gb: Memory estimate in GB (may be 0 for cloud models).
        quantization: Quantization scheme if applicable.
        preferred_for: Task types this model is preferred for.
    """

    model_id: str
    provider: str
    display_name: str
    capabilities: list[str] = Field(default_factory=list)
    context_window: int = Field(ge=1)
    latency_hint: str = "medium"
    privacy_level: str = "public"
    cost_per_1k_tokens: float = Field(default=0.0, ge=0)
    status: str = "available"
    endpoint: str = ""
    requires_env: list[str] = Field(default_factory=list)
    memory_requirements_gb: float = Field(default=0.0, ge=0)
    quantization: str = ""
    preferred_for: list[str] = Field(default_factory=list)


CloudEgressMode = Literal["local_only", "local_first_with_fallback", "explicit_cloud", "explicit_opt_in_only"]
CloudFallbackTrigger = Literal[
    "disabled",
    "local_unavailable",
    "explicit_request",
    "policy_approved",
    "explicit_operator_override",
]


class RoutingPolicy(BaseModel):
    """Model routing policy controlling provider preference and cost caps.

    Attributes:
        local_first: Prefer local models over cloud when possible.
        privacy_weight: Scoring weight for privacy level (higher = more important).
        latency_weight: Scoring weight for latency preference.
        cost_weight: Scoring weight for cost efficiency.
        max_cost_per_1k_tokens: Hard cap in USD; ``None`` means no cap.
        preferred_providers: Ordered list of provider slugs by priority.
        allow_cloud_fallback: Whether cloud escalation is allowed when local fails.
        cloud_fallback_trigger: Condition that triggers the cloud fallback.
        cloud_egress_mode: Product-level cloud egress policy. ``local_only``
            means cloud routing is disabled, ``local_first_with_fallback`` means
            local models are preferred and cloud is limited to the configured
            fallback trigger, and ``explicit_cloud`` means callers must
            intentionally select cloud.
    """

    local_first: bool = True
    privacy_weight: float = Field(default=1.0, ge=0)
    latency_weight: float = Field(default=0.5, ge=0)
    cost_weight: float = Field(default=0.3, ge=0)
    max_cost_per_1k_tokens: float | None = None
    preferred_providers: list[str] = Field(default_factory=list)
    allow_cloud_fallback: bool = False
    cloud_fallback_trigger: CloudFallbackTrigger = "disabled"
    cloud_egress_mode: CloudEgressMode = "local_only"

    @model_validator(mode="after")
    def validate_cloud_egress_contract(self) -> RoutingPolicy:
        """Reject contradictory cloud-egress settings.

        Returns:
            The validated routing policy when cloud-egress settings are consistent.

        Raises:
            ValueError: If fallback trigger, fallback enablement, and egress mode
                describe contradictory routing behavior.
        """
        if not self.allow_cloud_fallback and self.cloud_fallback_trigger != "disabled":
            raise ValueError("cloud_fallback_trigger must be 'disabled' when allow_cloud_fallback is false")
        if self.cloud_egress_mode == "local_only" and self.allow_cloud_fallback:
            raise ValueError("local_only cloud_egress_mode cannot allow cloud fallback")
        if self.cloud_egress_mode == "local_only" and self.cloud_fallback_trigger != "disabled":
            raise ValueError("local_only cloud_egress_mode requires cloud_fallback_trigger='disabled'")
        return self


class ModelsConfig(BaseModel):
    """Complete models.yaml configuration.

    Attributes:
        hardware: Hardware profile for the local inference machine.
        models: List of local model definitions.
        policy: Routing policy controlling local-first / cloud-fallback behaviour.
        cloud_models: List of cloud model definitions.
    """

    hardware: HardwareConfig = Field(default_factory=HardwareConfig)
    models: list[LocalModelEntry] = Field(default_factory=list)
    policy: RoutingPolicy = Field(default_factory=RoutingPolicy)
    cloud_models: list[CloudModelEntry] = Field(default_factory=list)

    @classmethod
    def from_yaml_file(cls, path: Path) -> ModelsConfig:
        """Load and validate models.yaml from the given path.

        Args:
            path: Path to ``models.yaml``.

        Returns:
            Validated :class:`ModelsConfig` instance.

        Raises:
            FileNotFoundError: If the file does not exist.
            ValueError: If the YAML is malformed or fails schema validation.
        """
        return cls.model_validate(_load_yaml(path))
