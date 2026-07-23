"""AM Workbench Python worker SDK.

The SDK is intentionally small: workers declare typed inputs, typed outputs,
resource limits, and crash-recovery behavior in a manifest, then execute
through a runner that validates inputs and fail-closes output shape before a
receipt is emitted.
"""

from __future__ import annotations

from .manifest import (
    CrashRecoveryPolicy,
    ResourceDeclaration,
    WorkerIOField,
    WorkerManifest,
    WorkerManifestError,
)
from .runner import (
    WorkerExecutionReceipt,
    WorkerOutputValidationError,
    WorkerRunner,
)

__all__ = [
    "CrashRecoveryPolicy",
    "ResourceDeclaration",
    "WorkerExecutionReceipt",
    "WorkerIOField",
    "WorkerManifest",
    "WorkerManifestError",
    "WorkerOutputValidationError",
    "WorkerRunner",
]
