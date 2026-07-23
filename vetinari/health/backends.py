"""Backend health helpers."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any

from vetinari.boundary_guards import assert_dependency_success

logger = logging.getLogger(__name__)

_REGISTERED_BACKENDS: dict[str, Any] = {}
_REGISTRATION_ERRORS: dict[str, str] = {}
_BACKEND_REGISTRY_LOCK = threading.RLock()


def check_backend_health(config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Check backend health.

    Args:
        config: Optional backend config.

    Returns:
        Backend health mapping.
    """
    llama_enabled = bool((config or {}).get("llama_cpp", {}).get("enabled", True))
    health: dict[str, Any] = {}
    try:
        assert_dependency_success(llama_enabled, dependency_id="llama_cpp_probe")
    except RuntimeError as exc:
        health["llama_cpp"] = {"available": False, "error": str(exc)}
    else:
        health["llama_cpp"] = {"available": True}
    with _BACKEND_REGISTRY_LOCK:
        registered_backends = tuple(_REGISTERED_BACKENDS.items())
        registration_errors = tuple(_REGISTRATION_ERRORS.items())
    for name, backend in registered_backends:
        try:
            assert_dependency_success(backend is not None, dependency_id=f"{name}_probe")
        except RuntimeError as exc:
            health[name] = {"available": False, "error": str(exc)}
        else:
            health[name] = {"available": True}
    for name, error in registration_errors:
        health[name] = {"available": False, "error": error}
    return health


def _default_backend_factory(name: str) -> dict[str, str]:
    return {"backend": name}


def register_backend(name: str, factory: Callable[[str], Any] | None = None) -> dict[str, str]:
    """Register a backend.

    Args:
        name: Backend name.
        factory: Optional backend factory used to construct and validate the backend.

    Returns:
        Registration result.

    Raises:
        Exception: Re-raises backend factory errors after recording registration failure.
    """
    backend_factory = factory or _default_backend_factory
    try:
        backend = backend_factory(name)
    except Exception as exc:
        with _BACKEND_REGISTRY_LOCK:
            _REGISTERED_BACKENDS.pop(name, None)
            _REGISTRATION_ERRORS[name] = str(exc)
        logger.warning("Backend registration failed for %s: %s", name, type(exc).__name__)
        raise
    assert_dependency_success(backend is not None, dependency_id=f"{name}_registration")
    with _BACKEND_REGISTRY_LOCK:
        _REGISTRATION_ERRORS.pop(name, None)
        _REGISTERED_BACKENDS[name] = backend
    return {"backend": name}


__all__ = ["check_backend_health", "register_backend"]
