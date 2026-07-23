"""Model configuration loader — reads agent model assignments from YAML."""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


_DEFAULT_MODEL = "qwen2.5-72b"
_DEFAULT_PROVIDER = "local"
_CONFIG_PATH = Path(__file__).parent / "models.yaml"

_cached_config: dict[str, Any] | None = None


def load_model_config(config_path: Path | None = None) -> dict[str, Any]:
    """Load model configuration from YAML.

    Args:
        config_path: Optional override for the YAML file path.
            Defaults to vetinari/config/models.yaml.

    Returns:
        Configuration dict with 'default' and 'agents' keys.

    Raises:
        ValueError: If an existing YAML config is malformed.
    """
    global _cached_config
    if _cached_config is not None and config_path is None:
        return _cached_config

    path = config_path or _CONFIG_PATH
    config = _build_default_config()

    if path.exists():
        try:
            import yaml

            with Path(path).open(encoding="utf-8") as f:
                loaded = yaml.safe_load(f)
            if isinstance(loaded, dict):
                # Merge loaded config over defaults
                if "default" in loaded:
                    config["default"].update(loaded["default"])
                if "agents" in loaded and isinstance(loaded["agents"], dict):
                    for agent_name, agent_config in loaded["agents"].items():
                        if isinstance(agent_config, dict):
                            config["agents"][agent_name] = agent_config
                logger.info("Model config loaded from %s", path)
            else:
                raise ValueError(f"Invalid model config format in {path}: expected mapping root")
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError(f"Failed to load model config from {path}") from exc
    else:
        logger.debug("Model config file not found at %s — using defaults", path)

    if config_path is None:
        _cached_config = config
    return config


@lru_cache(maxsize=256)
def get_model_for_agent(agent_type: str, mode: str = "") -> dict[str, str]:
    """Get model assignment for a specific agent type and mode.

    Args:
        agent_type: The agent type (e.g. 'planner', 'builder').
        mode: Optional mode (e.g. 'build', 'code_review').

    Returns:
        Dict with 'model' and 'provider' keys.
    """
    config = load_model_config()
    agent_key = agent_type.lower()
    agents = config.get("agents", {})

    if agent_key in agents:
        agent_config = agents[agent_key]
        # Check mode-specific config first
        if mode and mode in agent_config:
            return agent_config[mode]
        # Fall back to agent default
        if "default" in agent_config:
            return agent_config["default"]

    # Global default
    return config.get("default", {"model": _DEFAULT_MODEL, "provider": _DEFAULT_PROVIDER})


def get_task_default_model(task_type: str) -> str:
    """Select the best available model for a task type based on discovered models.

    Uses the models.yaml task_defaults as hints, then falls back to capability
    matching against actually-discovered local models. This avoids hardcoding
    model IDs that may not exist on the user's system.

    Args:
        task_type: Task category (coding, research, reasoning, planning, etc.).

    Returns:
        Model ID string of the best available model for this task type.
    """
    config = load_model_config()
    yaml_path = Path(__file__).parent / "models.yaml"

    # Capability preferences by task type
    _TASK_CAPABILITIES: dict[str, list[str]] = {
        "coding": ["coding", "fast"],
        "research": ["reasoning"],
        "reasoning": ["reasoning"],
        "planning": ["reasoning"],
        "review": ["coding", "reasoning"],
        "security": ["reasoning", "analysis"],
        "documentation": ["coding"],
        "creative": ["creative", "reasoning"],
        "classification": ["fast", "classification"],
        "general": ["reasoning"],
    }

    # Try to find from discovered models
    try:
        import yaml

        if yaml_path.exists():
            with yaml_path.open(encoding="utf-8") as f:
                yaml_cfg = yaml.safe_load(f) or {}

            # Check task_defaults first (user-configured preference)
            task_defaults = yaml_cfg.get("task_defaults", {})
            if task_type in task_defaults:
                return task_defaults[task_type]

            # Auto-select from local models by capability match
            models = yaml_cfg.get("models", [])
            available = [m for m in models if isinstance(m, dict) and m.get("status") == "available"]
            if available:
                desired = _TASK_CAPABILITIES.get(task_type, ["reasoning"])
                for model in available:
                    caps = model.get("capabilities", [])
                    if any(c in caps for c in desired):
                        return model["model_id"]
                # No capability match — return first available
                return available[0]["model_id"]
    except Exception:
        logger.warning("Could not auto-select task default for %s", task_type)

    return config.get("default", {}).get("model", _DEFAULT_MODEL)


_DEFAULT_TIMEOUTS: dict[str, int] = {
    "foreman": 120,  # Planning and decomposition — moderate latency expected
    "worker": 300,  # Code generation and long-running tasks need extra time
    "inspector": 60,  # Review passes are focused and should complete quickly
}

# Fallback timeout when no agent-specific value is configured
_DEFAULT_TIMEOUT = 120


def get_agent_timeout(agent_type: str) -> int:
    """Return the configured timeout in seconds for an agent type.

    Reads ``timeout_seconds`` from the agent's block in models.yaml.  Falls
    back to the hardcoded defaults for the three canonical agents (foreman,
    worker, inspector) and to ``_DEFAULT_TIMEOUT`` for unknown agents.

    Args:
        agent_type: The agent type string (e.g. ``"foreman"``, ``"WORKER"``).
            Case-insensitive.

    Returns:
        Timeout in seconds as a positive integer.
    """
    key = agent_type.lower()
    config = load_model_config()
    agent_cfg = config.get("agents", {}).get(key, {})
    if "timeout_seconds" in agent_cfg:
        return int(agent_cfg["timeout_seconds"])
    return _DEFAULT_TIMEOUTS.get(key, _DEFAULT_TIMEOUT)


def get_model_config() -> dict[str, Any]:
    """Return the full model configuration dict.

    Convenience wrapper around :func:`load_model_config` for callers that
    want the complete config without specifying a path.

    Returns:
        Configuration dict with ``'default'`` and ``'agents'`` keys.
    """
    return load_model_config()


def reset_cache() -> None:
    """Clear the cached configuration. Useful for testing."""
    global _cached_config
    _cached_config = None


def reset_model_config_cache() -> None:
    """Clear the model configuration cache."""
    reset_cache()


def _build_default_config() -> dict[str, Any]:
    """Build the default configuration dict.

    Includes the three canonical factory-pipeline agents (foreman, worker,
    inspector) with their default timeouts, plus the sub-role aliases used
    by the multi-mode planner.

    Returns:
        Default config with all agents set to the default model.
    """
    default = {"model": _DEFAULT_MODEL, "provider": _DEFAULT_PROVIDER}
    agents: dict[str, Any] = {}
    for agent_name, timeout in _DEFAULT_TIMEOUTS.items():
        agents[agent_name] = {"default": dict(default), "timeout_seconds": timeout}
    for agent_name in ("planner", "researcher", "oracle", "builder", "quality", "operations"):
        agents[agent_name] = {"default": dict(default)}
    return {"default": default, "agents": agents}
