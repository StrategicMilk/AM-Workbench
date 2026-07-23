"""Context management subsystem — window management, compaction, budget tracking, restoration, and ACON compression."""

from __future__ import annotations

from vetinari.context.acon import (
    ACONCompressor,
    CompressionOutcome,
    CompressionRule,
    InfoCategory,
    get_acon_compressor,
)
from vetinari.context.budget import (
    BudgetCheck,
    BudgetStatus,
    BudgetThresholds,
    ContextBudget,
    StageUsage,
    create_budget_for_model,
)
from vetinari.context.compaction import (
    CompactionResult,
    CompactionTier,
    ContextCompactor,
    get_compactor,
)
from vetinari.context.condenser import ContextCondenser, get_context_condenser
from vetinari.context.pipeline_integration import (
    ContextBudgetExceeded,
    PipelineContextManager,
    create_pipeline_context_manager,
)
from vetinari.context.restoration import (
    ContextRestorer,
    RestorationBudget,
    RestorationContext,
    RestorationResult,
    get_restorer,
)
from vetinari.context.session_state import (
    SessionState,
    SessionStateExtractor,
    get_session_state_extractor,
)
from vetinari.context.tool_persistence import ToolResultStore
from vetinari.context.window_manager import (
    CompressionConfig,
    CompressionResult,
    ContextCompressor,
    ContextWindowManager,
    WindowConversationMessage,
    count_tokens,
    fallback_estimate_tokens,
    get_context_compressor,
    get_window_manager,
)

__all__ = [
    "ACONCompressor",
    "BudgetCheck",
    "BudgetStatus",
    "BudgetThresholds",
    "CompactionResult",
    "CompactionTier",
    "CompressionConfig",
    "CompressionOutcome",
    "CompressionResult",
    "CompressionRule",
    "ContextBudget",
    "ContextBudgetExceeded",
    "ContextCompactor",
    "ContextCompressor",
    "ContextCondenser",
    "ContextRestorer",
    "ContextWindowManager",
    "InfoCategory",
    "PipelineContextManager",
    "RestorationBudget",
    "RestorationContext",
    "RestorationResult",
    "SessionState",
    "SessionStateExtractor",
    "StageUsage",
    "ToolResultStore",
    "WindowConversationMessage",
    "count_tokens",
    "create_budget_for_model",
    "create_pipeline_context_manager",
    "fallback_estimate_tokens",
    "get_acon_compressor",
    "get_compactor",
    "get_context_compressor",
    "get_context_condenser",
    "get_restorer",
    "get_session_state_extractor",
    "get_window_manager",
]
