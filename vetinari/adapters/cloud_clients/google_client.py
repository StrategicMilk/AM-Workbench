"""Lock-safe lazy Google GenAI SDK client factory."""

from __future__ import annotations

import importlib
import importlib.util
import sys
import threading
from typing import Any

_LOCK = threading.Lock()
_CLIENT: Any | None = None
_CLIENT_KEY: tuple[str | None] | None = None


def _require_module(module_name: str, package_name: str) -> Any:
    cached = sys.modules.get(module_name)
    if cached is not None:
        return cached
    if importlib.util.find_spec(module_name) is None:
        raise ImportError(f"{package_name} is required for the native Gemini cloud adapter")
    return importlib.import_module(module_name)


def get_google_client(*, api_key: str | None) -> Any:
    """Return a cached native Google GenAI client for the exact API key.

    Returns:
        Google GenAI SDK client instance.

    Raises:
        ImportError: If the Google GenAI SDK is not installed.
    """
    global _CLIENT, _CLIENT_KEY
    key = (api_key,)
    with _LOCK:
        if _CLIENT is not None and key == _CLIENT_KEY:
            return _CLIENT
        genai = _require_module("google.genai", "google-genai")
        _CLIENT = genai.Client(api_key=api_key)
        _CLIENT_KEY = key
        return _CLIENT


def _reset_google_client_for_test() -> None:
    """Clear the cached client; used by tests."""
    global _CLIENT, _CLIENT_KEY
    with _LOCK:
        _CLIENT = None
        _CLIENT_KEY = None
