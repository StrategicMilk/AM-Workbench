"""Source companion for optional backend extras tests."""

from __future__ import annotations


def _expected_extras() -> tuple[str, ...]:
    """Return the supported optional backend extras for this installation."""
    return ("audio", "video", "comfyui", "speculators", "embeddings_remote")
