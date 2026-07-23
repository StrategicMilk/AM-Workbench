"""Pydantic Settings models for Vetinari configuration.

Provides validated, type-safe configuration models that replace raw JSON
loading.  Models can be populated from JSON config files, environment
variables (prefixed ``VETINARI_``), or programmatic defaults.

Usage::

    from vetinari.config.settings import VetinariSettings, get_settings

    settings = get_settings()
    profile = settings.get_inference_profile("coding")
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from vetinari.exceptions import ConfigurationError

logger = logging.getLogger(__name__)


# ── Project root detection ────────────────────────────────────────────────────

_CONFIG_DIR = Path(__file__).resolve().parent.parent.parent / "config"


# ── Inference profile model ───────────────────────────────────────────────────


class InferenceProfileModel(BaseModel):
    """Validated inference parameters for a single task type.

    Attributes:
        temperature: Sampling temperature (0.0-1.5).
        top_p: Nucleus sampling probability ceiling (0.0-1.0).
        top_k: Top-k token count for sampling (1-100).
        max_tokens: Maximum tokens to generate.
        stop_sequences: Sequences that halt generation.
        prefer_json: Whether to request JSON output format.
        description: Human-readable description of the profile purpose.
    """

    temperature: float = Field(default=0.3, ge=0.0, le=1.5)
    top_p: float = Field(default=0.9, ge=0.0, le=1.0)
    top_k: int = Field(default=40, ge=1, le=100)
    max_tokens: int = Field(default=2048, ge=1, le=65536)
    stop_sequences: list[str] = Field(default_factory=list)
    prefer_json: bool = False
    description: str = ""


class ModelSizeAdjustment(BaseModel):
    """Offset adjustments applied based on model parameter count.

    Attributes:
        note: Human-readable size tier description.
        temperature_offset: Added to base temperature.
        top_p_offset: Added to base top_p.
        top_k_offset: Added to base top_k.
    """

    note: str = ""
    temperature_offset: float = 0.0
    top_p_offset: float = 0.0
    top_k_offset: int = 0


class ModelOverride(BaseModel):
    """Per-model parameter overrides.

    Attributes:
        note: Reason for the override.
        temperature_offset: Added to base temperature.
        top_p_offset: Added to base top_p.
        top_k_offset: Added to base top_k.
    """

    note: str = ""
    temperature_offset: float = 0.0
    top_p_offset: float = 0.0
    top_k_offset: int = 0


# ── Inference config aggregate ────────────────────────────────────────────────


class InferenceConfig(BaseModel):
    """Complete inference configuration loaded from task_inference_profiles.json.

    Attributes:
        version: Schema version string.
        profiles: Mapping of task type to inference profile.
        model_size_adjustments: Offsets keyed by size tier.
        model_overrides: Per-model-id offsets.
    """

    version: str = "1.0"
    profiles: dict[str, InferenceProfileModel] = Field(default_factory=dict)
    model_size_adjustments: dict[str, ModelSizeAdjustment] = Field(default_factory=dict)
    model_overrides: dict[str, ModelOverride] = Field(default_factory=dict)

    @classmethod
    def from_json_file(cls, path: Path) -> InferenceConfig:
        """Load and validate inference config from a JSON file.

        Args:
            path: Path to the JSON configuration file.

        Returns:
            Validated InferenceConfig instance.

        Raises:
            FileNotFoundError: If the config file does not exist.
            ValueError: If the JSON is malformed or fails validation.
        """
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ConfigurationError(f"Invalid JSON in {path}: {exc}") from exc
        return cls.model_validate(raw)


# ── Top-level application settings ────────────────────────────────────────────


class VetinariSettings(BaseSettings):
    """Top-level application settings populated from env vars and config files.

    Environment variables are prefixed with ``VETINARI_`` and use double
    underscores for nesting (e.g. ``VETINARI_LOG_LEVEL=DEBUG``).

    Attributes:
        log_level: Root logging level.
        inference_config_path: Path to task_inference_profiles.json.
        llm_guard_config_path: Path to llm_guard.yaml.
        local_models_dir: Directory containing GGUF model files for llama-cpp-python.
        local_gpu_layers: Number of transformer layers to offload to GPU; -1 means all.
        local_context_length: Default context window size in tokens.
        local_inference_timeout: Request timeout in seconds for local inference calls.
        max_agent_retries: Default retry limit for agent invocations.
        enable_observability: Whether OpenTelemetry tracing is active.
    """

    model_config = SettingsConfigDict(
        env_prefix="VETINARI_",
        env_nested_delimiter="__",
        case_sensitive=False,
    )

    log_level: str = "INFO"  # Root logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
    inference_config_path: Path = _CONFIG_DIR / "task_inference_profiles.json"
    llm_guard_config_path: Path = _CONFIG_DIR / "llm_guard.yaml"
    local_models_dir: str = ""  # Directory containing GGUF model files; empty = auto-detect
    local_gpu_layers: int = -1  # Layers to offload to GPU; -1 = all layers (auto)
    local_context_length: int = 8192  # Context window in tokens — safe default; ModelProfiler overrides per-model
    local_batch_size: int = 512  # Batch size for prompt processing (higher = faster, more VRAM)
    local_flash_attn: bool = False  # Flash attention (set True with CUDA GPU for faster inference)
    local_cache_type_k: str = "f16"  # KV cache key quantization: f16 (default), q8_0, q4_0
    local_cache_type_v: str = "f16"  # KV cache value quantization: f16 (default), q8_0, q4_0
    local_n_threads: int = 0  # CPU threads for inference; 0 = auto (half of logical cores)
    local_inference_timeout: float = 120.0  # Max seconds to wait for a model response
    max_agent_retries: int = 3  # How many times to retry a failed agent task
    enable_observability: bool = False  # Enable OpenTelemetry distributed tracing
    agent_timeouts: dict[str, float] = Field(
        default_factory=dict,
        description="Per-agent timeout overrides in seconds, keyed by AgentType value",
    )

    # ── Speculative decoding settings ──────────────────────────────────────────
    speculative_decoding_enabled: bool = False  # Master switch — off by default
    speculative_draft_model_id: str | None = None  # Model ID of the small draft model
    speculative_draft_n_tokens: int = 5  # Tokens to speculate per step (5 = balanced)

    def detect_api_keys(self) -> dict[str, bool]:
        """Check which API keys are available in environment variables.

        Returns:
            Dict mapping provider name to whether its API key was detected.
        """
        import os as _os

        return {
            "openai": bool(_os.environ.get("OPENAI_API_KEY")),
            "anthropic": bool(_os.environ.get("ANTHROPIC_API_KEY")),
            "gemini": bool(_os.environ.get("GEMINI_API_KEY")),
            "huggingface": bool(
                _os.environ.get("HF_TOKEN") or _os.environ.get("HF_HUB_TOKEN") or _os.environ.get("HUGGINGFACE_TOKEN")
            ),
            "groq": bool(_os.environ.get("GROQ_API_KEY")),
        }

    def get_agent_timeout(self, agent_type: str) -> float:
        """Get the timeout for a specific agent type.

        Args:
            agent_type: The agent type value string (e.g. "WORKER", "INSPECTOR").

        Returns:
            Timeout in seconds (falls back to local_inference_timeout).
        """
        return self.agent_timeouts.get(agent_type, self.local_inference_timeout)

    @field_validator("log_level")
    @classmethod
    def _validate_log_level(cls, v: str) -> str:
        """Ensure log_level is a recognized Python logging level name."""
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        upper = v.upper()
        if upper not in allowed:
            raise ValueError(f"log_level must be one of {allowed}, got {v!r}")
        return upper

    @field_validator("local_cache_type_k", "local_cache_type_v")
    @classmethod
    def _validate_kv_cache_type(cls, v: str) -> str:
        """Ensure KV cache quantization type is a supported llama.cpp format.

        Args:
            v: The quantization type string from config or environment.

        Returns:
            Validated quantization type string (unchanged).

        Raises:
            ValueError: If the type is not one of the supported options.
        """
        allowed = {"f16", "q8_0", "q4_0"}
        if v not in allowed:
            raise ValueError(f"KV cache type must be one of {allowed}, got {v!r}")
        return v

    def load_inference_config(self) -> InferenceConfig:
        """Load and validate the inference config from the configured path.

        Returns:
            Validated InferenceConfig.

        Raises:
            ConfigurationError: If the configured file is missing, unreadable,
                or invalid.
        """
        path = self.inference_config_path
        if not path.exists():
            raise ConfigurationError(f"Inference config not found at {path}")
        try:
            cfg = InferenceConfig.from_json_file(path)
            logger.info("Loaded %d inference profiles from %s", len(cfg.profiles), path)
            return cfg
        except (ValueError, OSError) as exc:
            raise ConfigurationError(f"Failed to load inference config from {path}: {exc}") from exc

    def get_inference_profile(self, task_type: str) -> InferenceProfileModel:
        """Convenience method to load config and retrieve a single profile.

        Args:
            task_type: The task type key (e.g. ``"coding"``, ``"planning"``).

        Returns:
            The matching profile, or the ``"general"`` fallback, or defaults.
        """
        cfg = self.load_inference_config()
        if task_type in cfg.profiles:
            return cfg.profiles[task_type]
        if "general" in cfg.profiles:
            return cfg.profiles["general"]
        return InferenceProfileModel()


# ── Singleton accessor ────────────────────────────────────────────────────────

_settings_instance: VetinariSettings | None = None
_settings_lock = threading.Lock()


def get_settings() -> VetinariSettings:
    """Return the process-global VetinariSettings instance.

    Returns:
        The singleton VetinariSettings.
    """
    global _settings_instance
    if _settings_instance is None:
        with _settings_lock:
            if _settings_instance is None:
                _settings_instance = VetinariSettings()
    return _settings_instance


def reset_settings(config_path: str | None = None) -> bool | None:
    """Reset the singleton (intended for testing).

    Args:
        config_path: Optional persisted settings path used by compatibility callers.

    Returns:
        True when a settings path was supplied, otherwise None.
    """
    global _settings_instance
    _settings_instance = None
    if config_path is not None:
        return True
    return None


Settings = VetinariSettings
