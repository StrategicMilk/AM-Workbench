"""Model management, routing, and optimization subsystem."""

from __future__ import annotations

from vetinari.models.best_of_n import BestOfNSelector, get_n_for_tier
from vetinari.models.calibration import (
    CalibrationResult,
    calibrate_model,
    seed_thompson_priors,
)
from vetinari.models.capability import (
    CapabilityMaturity,
    CapabilityMaturityStore,
    append_capability_record,
    can_promote,
    load_capability_records,
)
from vetinari.models.draft_pair_resolver import (
    DraftPairResolver,
    get_draft_pair_resolver,
    reset_draft_pair_resolver,
)
from vetinari.models.dynamic_model_router import (
    DynamicModelRouter,
    get_model_router,
    infer_task_type,
    init_model_router,
)
from vetinari.models.inference_config import (
    BudgetPolicy,
    InferenceConfig,
    get_budget_policy,
)
from vetinari.models.inference_endpoint_capabilities import (
    CapabilityContractError,
    EndpointCapabilityRecord,
    RouteReceipt,
    load_endpoint_capability_records,
    read_route_receipts,
)
from vetinari.models.kv_state_cache import (
    KVStateCache,
    get_kv_state_cache,
    reset_kv_state_cache,
)
from vetinari.models.model_pool import ModelPool
from vetinari.models.model_profiler import (
    ModelProfiler,
    get_model_profiler,
    reset_model_profiler,
)
from vetinari.models.model_registry import (
    ModelRegistry,
    get_model_registry,
)
from vetinari.models.model_relay import (
    get_model_relay,
)
from vetinari.models.model_scout import (
    ModelRecommendation,
    ModelScout,
    get_model_scout,
)
from vetinari.models.ponder import (
    PonderEngine,
    get_available_models,
    get_ponder_health,
    ponder_project_for_plan,
    rank_models,
)
from vetinari.models.scan import (
    ModelFormat,
    ModelRecord,
    RuntimeRequirements,
    scan,
)

__all__ = [
    "BestOfNSelector",
    "BudgetPolicy",
    "CalibrationResult",
    "CapabilityContractError",
    "CapabilityMaturity",
    "CapabilityMaturityStore",
    "DraftPairResolver",
    "DynamicModelRouter",
    "EndpointCapabilityRecord",
    "InferenceConfig",
    "KVStateCache",
    "ModelFormat",
    "ModelPool",
    "ModelProfiler",
    "ModelRecommendation",
    "ModelRecord",
    "ModelRegistry",
    "ModelScout",
    "PonderEngine",
    "RouteReceipt",
    "RuntimeRequirements",
    "append_capability_record",
    "calibrate_model",
    "can_promote",
    "get_available_models",
    "get_budget_policy",
    "get_draft_pair_resolver",
    "get_kv_state_cache",
    "get_model_profiler",
    "get_model_registry",
    "get_model_relay",
    "get_model_router",
    "get_model_scout",
    "get_n_for_tier",
    "get_ponder_health",
    "infer_task_type",
    "init_model_router",
    "load_capability_records",
    "load_endpoint_capability_records",
    "ponder_project_for_plan",
    "rank_models",
    "read_route_receipts",
    "reset_draft_pair_resolver",
    "reset_kv_state_cache",
    "reset_model_profiler",
    "scan",
    "seed_thompson_priors",
]
