"""Credential discovery helpers for runtime configuration."""

from __future__ import annotations

import os

API_KEY_ENV_VARS = {
    "huggingface": ("HF_HUB_TOKEN", "HF_TOKEN", "HUGGINGFACE_TOKEN"),
    "anthropic": ("ANTHROPIC_API_KEY", "CLAUDE_API_KEY"),
    "gemini": ("GEMINI_API_KEY",),
    "openai": ("OPENAI_API_KEY",),
}


def detect_api_keys() -> dict[str, bool]:
    """Detect known provider API keys in the environment.

    Returns:
        Mapping of provider name to whether a supported key is present.
    """
    return {provider: any(os.environ.get(name) for name in names) for provider, names in API_KEY_ENV_VARS.items()}


__all__ = ["API_KEY_ENV_VARS", "detect_api_keys"]
