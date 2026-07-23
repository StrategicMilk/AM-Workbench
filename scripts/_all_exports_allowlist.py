"""Allowlisted public export contracts for repository checks."""

from __future__ import annotations

PUBLIC_MODULES = (
    "vetinari.adapters.base",
    "vetinari.agents",
    "vetinari.db",
    "vetinari.diagnostics",
    "vetinari.drift",
    "vetinari.governance",
    "vetinari.governance.approvals",
    "vetinari.guardrails.prompt_security",
    "vetinari.inference",
    "vetinari.inference.constrained",
    "vetinari.learning",
    "vetinari.memory",
    "vetinari.memory.episode_recorder",
    "vetinari.memory.unified",
    "vetinari.orchestration",
    "vetinari.orchestration.checkpoint_store",
    "vetinari.orchestration.durable_db",
    "vetinari.orchestration.durable_execution",
    "vetinari.orchestration.execution_graph",
    "vetinari.orchestration.types",
    "vetinari.planning",
    "vetinari.planning.decomposition",
    "vetinari.sandbox_policy",
    "vetinari.training",
    "vetinari.training.continual_learning",
    "vetinari.training.pipeline",
    "vetinari.training.synthetic_data",
    "vetinari.workbench",
    "vetinari.workbench.managed_agents",
    "vetinari.workbench.managed_agents.contracts",
    "vetinari.workbench.simulation",
    "vetinari.workbench.source_cards",
    "vetinari.workbench.tool_cards",
)

LEGACY_PRIVATE_EXPORTS = {
    "vetinari.agents": frozenset({"_self_critique"}),
    "vetinari.memory.unified": frozenset({
        "_embed_via_local_inference",
        "_pack_embedding",
        "_unpack_embedding",
    }),
    "vetinari.orchestration.durable_db": frozenset({"_DatabaseManager", "_SCHEMA_SQL"}),
    "vetinari.planning.decomposition": frozenset({"_DOD_CRITERIA", "_DOR_CRITERIA"}),
    "vetinari.sandbox_policy": frozenset({
        "_ALLOWED_COMMANDS",
        "_DANGEROUS_ATTRS",
        "_DANGEROUS_NAMES",
        "_SAFE_ENV_VARS",
        "_SandboxAuditLogger",
        "_SandboxPolicyLoader",
        "_SandboxRateLimiter",
        "_execute_plugin_hook_entry",
    }),
    "vetinari.training.pipeline": frozenset({"_ensure_packages"}),
}

LEGACY_DEPRECATED_EXPORTS = {
    "vetinari.planning": frozenset({
        "PlanManager",
        "PlanningExecutionPlan",
        "Wave",
        "WaveStatus",
        "get_plan_manager",
    }),
}
