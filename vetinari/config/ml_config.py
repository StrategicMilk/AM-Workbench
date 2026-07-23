"""ML runtime configuration loader."""

from __future__ import annotations

from typing import Any

from vetinari.config.loader import load_backend_runtime_config

ML_SECTION_KEYS: set[str] = {
    "ml",
    "models",
    "inference",
    "training",
    "gguf",
    "thompson_sampling",
    "quality_scoring",
    "model_routing",
    "token_optimization",
    "feedback_loop",
    "auto_tuner",
    "prompt_evolver",
    "decomposition",
    "ponder",
    "cascade_confidence",
}


def _merge_section(target: dict[str, Any], source: dict[str, Any]) -> None:
    for key, value in source.items():
        if key not in ML_SECTION_KEYS or not isinstance(value, dict):
            continue
        current = target.setdefault(key, {})
        if isinstance(current, dict):
            current.update(value)
        else:
            target[key] = dict(value)


def get_ml_config() -> dict[str, Any]:
    """Return ML-related runtime configuration.

    Returns:
        Non-empty mapping containing ML, model, and inference sections.
    """
    config = load_backend_runtime_config()
    ml_config: dict[str, Any] = {}
    if any(key in config for key in ("baked", "project", "user")):
        for layer_name in ("baked", "project", "user"):
            layer = config.get(layer_name)
            if isinstance(layer, dict):
                _merge_section(ml_config, layer)
    else:
        _merge_section(ml_config, config)
    return ml_config or {"models": {}, "inference": {}}


__all__ = ["ML_SECTION_KEYS", "get_ml_config"]
