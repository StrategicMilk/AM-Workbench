"""Public accessors for the owned AM Engine runtime."""

from __future__ import annotations

import threading
from importlib import import_module
from typing import Any

from vetinari.backend_config import get_engine_model_path
from vetinari.engine.supervisor import EngineConfig, EngineSupervisor

_supervisor: EngineSupervisor | None = None
_supervisor_lock = threading.Lock()


def get_supervisor() -> EngineSupervisor:
    """Return the process-wide, lock-safe engine supervisor singleton.

    Returns:
        The sole supervisor instance for this Vetinari process.
    """
    global _supervisor
    if _supervisor is None:
        with _supervisor_lock:
            if _supervisor is None:
                _supervisor = EngineSupervisor(EngineConfig(model_path=get_engine_model_path()))
    return _supervisor


def get_engine_client() -> Any:
    """Return the configured engine data-plane client singleton.

    Returns:
        The configured AM Engine client singleton.

    """
    client_module = import_module("vetinari.engine.client")
    return client_module.get_engine_client()


__all__ = ["get_engine_client", "get_supervisor"]
