"""Vetinari workbench package - typed metadata spine surface.

Re-exports the public symbols of the spine subpackage so callers can
write ``from vetinari.workbench import WorkbenchSpine`` rather than
hunting through individual modules.

This module is import-safe: no I/O, no module-level state, no hooks.
The first ``WorkbenchSpine`` instance constructed creates the on-disk
store directory under ``outputs/workbench/spine``.
"""

from __future__ import annotations

from importlib import import_module
from types import ModuleType

_public_exports_module = import_module("vetinari.workbench._public_exports")
_PUBLIC_ALL = import_module("vetinari.workbench._public_all").PUBLIC_ALL

for _name, _value in _public_exports_module.PUBLIC_EXPORTS.items():
    globals()[_name] = _value

_EFFECTIVE_CONFIG_EXPORTS = (
    "EffectiveConfigDiff",
    "EffectiveConfigEntry",
    "EffectiveConfigError",
    "EffectiveConfigSnapshot",
    "capture_embedding_config_snapshot",
    "capture_model_selection_config_snapshot",
    "capture_retrieval_config_snapshot",
    "capture_tool_use_config_snapshot",
    "capture_training_config_snapshot",
    "diff_effective_config_snapshots",
    "sample_effective_config_explorer",
)


def _refresh_effective_config_exports() -> None:
    """Bind effective-config exports after import fan-in settles."""
    module = import_module("vetinari.workbench.effective_config")
    for name in _EFFECTIVE_CONFIG_EXPORTS:
        globals()[name] = getattr(module, name)


_refresh_effective_config_exports()


_LAZY_SUBMODULES = {
    "annotation_templates",
    "adaptive_tuning",
    "artifact_pair_miner",
    "automation",
    "cards",
    "collaboration",
    "competitive_drift",
    "command_safety",
    "conversation",
    "context_enrichment",
    "costing",
    "correction_decomposer",
    "data_assets",
    "dataset_revisions",
    "domain_review",
    "durable_workflow_adapter",
    "effective_config",
    "exports",
    "gateway_policy",
    "habit_health",
    "knowledge_graph",
    "local_runtime_onboarding",
    "memory",
    "managed_agents",
    "memory_scopes",
    "migration",
    "model_choices",
    "model_foundry",
    "multimodal",
    "openinference",
    "outcomes",
    "playground",
    "plugin_runtime",
    "preferences",
    "personalization",
    "promotion_inbox",
    "promotions",
    "query",
    "rag_autopsy",
    "rag_debugger",
    "rigor",
    "redteam_adapter",
    "self_improvement",
    "shadow_tester",
    "simulation",
    "source_health",
    "specialists",
    "extensions",
    "mcp_marketplace",
    "shields",
    "trace_interop",
    "work_graph",
    "trace_to_eval",
    "training",
    "training_sets",
    "tuning_data",
    "user_observability",
    "weaving",
    "why",
}


def __getattr__(name: str) -> ModuleType:
    if name in _EFFECTIVE_CONFIG_EXPORTS:
        return getattr(import_module("vetinari.workbench.effective_config"), name)
    if name not in _LAZY_SUBMODULES:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module = import_module(f"{__name__}.{name}")
    globals()[name] = module
    return module


__all__ = list(_PUBLIC_ALL)

for _name in _EFFECTIVE_CONFIG_EXPORTS:
    globals().pop(_name, None)
