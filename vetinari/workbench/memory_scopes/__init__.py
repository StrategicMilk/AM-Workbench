"""User-understandable Workbench memory scopes."""

from __future__ import annotations

from vetinari.workbench.memory_scopes.runtime import (
    BLOCKER_CROSS_SCOPE_NOT_ALLOWED,
    BLOCKER_EXPLICIT_SAVE_REQUIRED,
    BLOCKER_PROMOTION_REQUIRED,
    MemoryScope,
    MemoryScopeDecision,
    MemoryScopeError,
    MemoryScopePolicy,
    SensitiveMemoryCategory,
    decide_memory_save,
    default_memory_scope_policies,
)

__all__ = [
    "BLOCKER_CROSS_SCOPE_NOT_ALLOWED",
    "BLOCKER_EXPLICIT_SAVE_REQUIRED",
    "BLOCKER_PROMOTION_REQUIRED",
    "MemoryScope",
    "MemoryScopeDecision",
    "MemoryScopeError",
    "MemoryScopePolicy",
    "SensitiveMemoryCategory",
    "decide_memory_save",
    "default_memory_scope_policies",
]
