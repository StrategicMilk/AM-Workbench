"""Process-scope cache for the local inference adapter."""

from __future__ import annotations

import os
import threading

from vetinari.adapters.llama_cpp_local_adapter import LocalInferenceAdapter

# Single-tenant assumption: this cache holds one LocalInferenceAdapter instance per
# process. Vetinari runs as a local-first single-user workbench; there is no per-user
# adapter isolation. If multi-tenant deployment is added in future, this module must
# be replaced with a per-user keyed cache.

_lock: threading.Lock = threading.Lock()
_adapter_slot: _AdapterSlot | None = None


class _AdapterSlot:
    """Owns the heavyweight adapter construction outside per-call accessors."""

    def __init__(self) -> None:
        self.adapter = LocalInferenceAdapter()


def _assert_single_tenant_mode() -> None:
    """Fail closed when process-scope adapter cache is used in multi-tenant mode."""
    if os.environ.get("VETINARI_MULTI_TENANT", "").strip().lower() in {"1", "true", "yes", "on"}:
        raise RuntimeError(
            "LocalInferenceAdapter process cache is single-tenant; disable VETINARI_MULTI_TENANT "
            "or replace adapter_cache with a tenant-keyed cache."
        )


def get_local_inference_adapter(model_id: str | None = None) -> LocalInferenceAdapter:
    """Return the process-scope cached LocalInferenceAdapter.

    Args:
        model_id: Optional model identifier accepted for call-site compatibility.
            The adapter itself is not model-specific; callers pass the model id to
            ``LocalInferenceAdapter.chat``.

    Returns:
        Cached LocalInferenceAdapter instance.
    """
    del model_id
    _assert_single_tenant_mode()
    global _adapter_slot
    with _lock:
        if _adapter_slot is None:
            _adapter_slot = _AdapterSlot()
        adapter = _adapter_slot.adapter
    return adapter


def clear_adapter_cache() -> None:
    """Clear the process-scope adapter cache.

    Args:
        None.

    Returns:
        None.
    """
    global _adapter_slot
    with _lock:
        _adapter_slot = None
