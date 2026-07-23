"""Centralized application configuration for Vetinari.

Replaces hardcoded config dicts scattered across web_ui.py, cli.py, and dashboard modules.
All values are loaded from environment variables with sensible defaults pulled from
those existing locations.
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass, field
from typing import Any

from vetinari.utils.serialization import dataclass_to_dict

logger = logging.getLogger(__name__)


def _env_int(name: str, default: int, *, minimum: int | None = None, maximum: int | None = None) -> int:
    """Read an integer environment variable with bounded fallback behavior.

    Args:
        name: Environment variable name.
        default: Value to use when the environment variable is unset,
            malformed, or outside the configured bounds.
        minimum: Inclusive minimum accepted value.
        maximum: Inclusive maximum accepted value.

    Returns:
        Parsed integer value, or ``default`` when the configured value is not
        trustworthy.
    """
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        logger.warning("Ignoring invalid integer environment value %s=%r; using default %s", name, raw, default)
        return default
    if minimum is not None and value < minimum:
        logger.warning("Ignoring out-of-range integer environment value %s=%r; using default %s", name, raw, default)
        return default
    if maximum is not None and value > maximum:
        logger.warning("Ignoring out-of-range integer environment value %s=%r; using default %s", name, raw, default)
        return default
    return value


@dataclass
class VetinariConfig:
    """Centralized configuration for the Vetinari system.

    All fields are accessed as plain attributes (``config.models_dir``).
    Runtime state such as ``active_model_id`` is mutated directly on the
    singleton instance returned by :func:`get_config`.
    """

    # Server
    host: str = "127.0.0.1"
    port: int = 5000
    debug: bool = False

    # Local inference (llama-cpp-python)
    models_dir: str = "./models"
    local_gpu_layers: int = -1  # -1 = offload all layers to GPU
    local_context_length: int = 8192
    api_token: str = ""
    hf_token: str = ""  # HuggingFace API token for private-model downloads

    # Model defaults (from current_config in web_ui.py)
    default_models: list[str] = field(
        default_factory=lambda: [
            "qwen2.5-coder-7b",
            "qwen3-30b-a3b",
        ],
    )
    fallback_models: list[str] = field(
        default_factory=lambda: [
            "llama-3.3-70b",
            "qwen2.5-72b",
        ],
    )
    uncensored_fallback_models: list[str] = field(
        default_factory=lambda: [
            "qwen3-vl-32b-gemini-heretic-uncensored-thinking",
            "qwen2.5-vl-32b",
        ],
    )

    # Paths (from constants.py and cli.py defaults)
    config_path: str = "manifest/vetinari.yaml"
    project_dir: str = "projects"
    output_dir: str = "outputs"

    # Resource limits (from web_ui.py current_config and constants.py)
    memory_budget_gb: int = 48
    max_concurrent_tasks: int = 4

    # Timeouts (seconds)
    default_timeout: int = 120
    llm_timeout: int = 300

    # External discovery feature flag (from web_ui.py)
    enable_external_discovery: bool = True

    # Active model (mutable runtime state, None = no override)
    active_model_id: str | None = None

    @classmethod
    def from_env(cls) -> VetinariConfig:
        """Load configuration from environment variables with sensible defaults.

        Returns:
            A ``VetinariConfig`` populated entirely from the process environment.
        """
        return cls(
            host=os.environ.get("VETINARI_WEB_HOST", "127.0.0.1"),
            port=_env_int("VETINARI_WEB_PORT", 5000, minimum=1, maximum=65535),
            debug=os.environ.get("VETINARI_DEBUG", "").lower() in ("1", "true", "yes"),
            models_dir=os.environ.get("VETINARI_MODELS_DIR", "./models"),
            local_gpu_layers=_env_int("VETINARI_GPU_LAYERS", -1, minimum=-1),
            local_context_length=_env_int("VETINARI_CONTEXT_LENGTH", 8192, minimum=1),
            api_token=os.environ.get("VETINARI_API_TOKEN", ""),
            hf_token=os.environ.get("HF_TOKEN", ""),
            config_path=os.environ.get("VETINARI_CONFIG", "manifest/vetinari.yaml"),
            project_dir=os.environ.get("VETINARI_PROJECT_DIR", "projects"),
            output_dir=os.environ.get("VETINARI_OUTPUT_DIR", "outputs"),
            memory_budget_gb=_env_int("VETINARI_MEMORY_GB", 48, minimum=1),
            max_concurrent_tasks=_env_int("VETINARI_MAX_CONCURRENT", 4, minimum=1),
            default_timeout=_env_int("VETINARI_TIMEOUT", 120, minimum=1),
            llm_timeout=_env_int("VETINARI_LLM_TIMEOUT", 300, minimum=1),
            enable_external_discovery=os.environ.get("ENABLE_EXTERNAL_DISCOVERY", "true").lower()
            in ("1", "true", "yes"),
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-compatible dictionary."""
        return dataclass_to_dict(self)

    def __repr__(self) -> str:
        """Return a concise representation showing key runtime fields."""
        return (
            f"VetinariConfig(host={self.host!r}, port={self.port!r}, "
            f"config_path={self.config_path!r}, models_dir={self.models_dir!r})"
        )


# Singleton — populated on first call to get_config()
_config: VetinariConfig | None = None
_config_lock = threading.Lock()


def get_config() -> VetinariConfig:
    """Return the global configuration singleton, creating it from the environment on first call.

    Returns:
        The process-wide ``VetinariConfig`` instance.
    """
    global _config
    if _config is None:
        with _config_lock:
            if _config is None:
                _config = VetinariConfig.from_env()
    return _config
