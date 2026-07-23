"""Compatibility exports for the Rust scheduler authority bridge.

The implementation lives in :mod:`vetinari.runtime._workbench_rust_bridge`.
This public module remains for callers and tests that import the older path.
"""

from __future__ import annotations

from vetinari.runtime._workbench_rust_bridge import (
    RustSchedulerBridge,
    RustSchedulerBridgeSnapshot,
    RustSchedulerBridgeUnavailable,
)

__all__ = [
    "RustSchedulerBridge",
    "RustSchedulerBridgeSnapshot",
    "RustSchedulerBridgeUnavailable",
]
