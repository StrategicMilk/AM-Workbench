"""Protocol contracts for durable execution recovery mixins."""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from vetinari.typing_support import MixinProtocol

if TYPE_CHECKING:
    from vetinari.orchestration.checkpoint_store import CheckpointStore
    from vetinari.orchestration.execution_graph import ExecutionGraph


@runtime_checkable
class DurableExecutionRecoveryHost(MixinProtocol, Protocol):
    """Host attributes required by ``_DurableExecutionRecoveryMixin``."""

    _execution_lock: threading.Lock
    _active_executions: dict[str, ExecutionGraph]
    _checkpoint_store: CheckpointStore


@runtime_checkable
class _PausedQuestionsDb(Protocol):
    """Minimal contract for save_paused_questions: an object exposing ``._db``."""

    _db: Any


__all__ = ["DurableExecutionRecoveryHost", "_PausedQuestionsDb"]
