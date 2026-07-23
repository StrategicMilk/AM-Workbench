"""Lock-safe lazy Anthropic SDK client factory."""

from __future__ import annotations

import importlib
import importlib.util
import sys
import threading
from typing import Any

_LOCK = threading.Lock()
_CLIENT: Any | None = None
_CLIENT_KEY: tuple[str | None, str | None, int | None] | None = None


def _require_module(module_name: str, package_name: str) -> Any:
    cached = sys.modules.get(module_name)
    if cached is not None:
        return cached
    if importlib.util.find_spec(module_name) is None:
        raise ImportError(f"{package_name} is required for the native Anthropic cloud adapter")
    return importlib.import_module(module_name)


def get_anthropic_client(
    *, api_key: str | None, base_url: str | None = None, timeout_seconds: int | None = None
) -> Any:
    """Return a cached native Anthropic client for the exact connection tuple.

    Returns:
        Anthropic SDK client instance.

    Raises:
        ImportError: If the Anthropic SDK is not installed.
    """
    global _CLIENT, _CLIENT_KEY
    key = (api_key, base_url, timeout_seconds)
    with _LOCK:
        if _CLIENT is not None and key == _CLIENT_KEY:
            return _CLIENT
        Anthropic = _require_module("anthropic", "anthropic").Anthropic
        kwargs: dict[str, Any] = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
        if timeout_seconds is not None:
            kwargs["timeout"] = timeout_seconds
        _CLIENT = Anthropic(**kwargs)
        _CLIENT_KEY = key
        return _CLIENT


def _reset_anthropic_client_for_test() -> None:
    """Clear the cached client; used by tests."""
    global _CLIENT, _CLIENT_KEY
    with _LOCK:
        _CLIENT = None
        _CLIENT_KEY = None
