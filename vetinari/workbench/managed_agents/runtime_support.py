"""Support helpers for managed-agent runtime."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from vetinari.workbench.managed_agents.contracts import (
    BLOCKER_DEPENDENCY_UNAVAILABLE,
    BLOCKER_MEMORY_POLICY_RECEIPT_REQUIRED,
    BLOCKER_MEMORY_SCOPE_NOT_ALLOWED,
    BLOCKER_PROJECT_SCOPE_UNSAFE,
    BLOCKER_TOOL_NOT_ALLOWED,
    ManagedAgentDependencyRefs,
    ManagedAgentKind,
    ManagedAgentRecord,
    ManagedAgentState,
)

_CONFIG_SCHEMA_VERSION = 1


def _fallback_config() -> dict[str, Any]:
    return {
        "schema_version": _CONFIG_SCHEMA_VERSION,
        "dependency_defaults": {
            "mailbox_channel": "agent_queue",
            "route_ledger_ref": "route-ledger:managed-agent",
            "watcher_policy_ref": "watcher-policy:managed-agent",
            "automation_recipe_refs": ["automation-recipe:managed-agent-default"],
            "conversation_ref": "conversation",
            "promotion_targets": ["plan", "evidence", "dataset", "prompt", "automation", "durable_memory"],
            "memory_policy_ref": "memory-scope-policy",
            "monitoring_signal_ref": "monitoring-signal:managed-agent",
            "trace_eval_ref": "trace-eval:managed-agent",
        },
        "cost_ceilings": {
            "resource-governor:default-prosumer": {
                "max_cost_usd": 1.0,
                "estimated_cost_per_run_usd": 0.05,
            },
        },
    }


def _project_scope_blocker(project_id: str) -> str:
    if "/" in project_id or "\\" in project_id or ".." in project_id:
        return BLOCKER_PROJECT_SCOPE_UNSAFE
    return ""


def _tool_blockers(requested: tuple[str, ...], allowed: tuple[str, ...]) -> tuple[str, ...]:
    return (BLOCKER_TOOL_NOT_ALLOWED,) if set(requested) - set(allowed) else ()


def _memory_blockers(
    requested: tuple[str, ...],
    allowed: tuple[str, ...],
    *,
    policy_receipt_ref: str,
) -> tuple[str, ...]:
    blockers: list[str] = []
    if set(requested) - set(allowed):
        blockers.append(BLOCKER_MEMORY_SCOPE_NOT_ALLOWED)
    if tuple(requested) != tuple(allowed[: len(requested)]) and not policy_receipt_ref.strip():
        blockers.append(BLOCKER_MEMORY_POLICY_RECEIPT_REQUIRED)
    return tuple(dict.fromkeys(blockers))


def _operator_action_for_blockers(blockers: tuple[str, ...]) -> str:
    if BLOCKER_TOOL_NOT_ALLOWED in blockers:
        return "choose_template_allowed_tools"
    if BLOCKER_MEMORY_POLICY_RECEIPT_REQUIRED in blockers:
        return "approve_memory_scope_policy"
    if BLOCKER_MEMORY_SCOPE_NOT_ALLOWED in blockers:
        return "choose_template_allowed_memory_scope"
    if BLOCKER_DEPENDENCY_UNAVAILABLE in blockers:
        return "repair_dependency_config"
    return "review_managed_agent_request"


def _intervention_for_state(state: ManagedAgentState, reason: str, cost_ref: str) -> dict[str, Any]:
    return {
        "state": state.value,
        "why_running": reason,
        "cost_ref": cost_ref,
        "cost_gate": "enforced_before_run",
        "safe_actions": ["pause", "inspect", "retire"] if state is ManagedAgentState.ACTIVE else ["inspect", "retire"],
        "requires_user_attention": state in {ManagedAgentState.PAUSED, ManagedAgentState.RETIRED},
    }


def _intervention_summary(records: list[dict[str, Any]], degradation_reasons: list[str]) -> dict[str, Any]:
    attention = [
        record["agent_id"]
        for record in records
        if record["state"] != ManagedAgentState.ACTIVE.value or record["intervention"].get("requires_user_attention")
    ]
    return {
        "attention_agent_ids": attention,
        "degradation_reasons": degradation_reasons,
        "safe_global_actions": ["install", "inspect", "pause", "retire"],
    }


def _replace_record(record: ManagedAgentRecord, **overrides: Any) -> ManagedAgentRecord:
    values = record.to_dict()
    values.update(overrides)
    if isinstance(values["kind"], str):
        values["kind"] = ManagedAgentKind(values["kind"])
    if isinstance(values["state"], str):
        values["state"] = ManagedAgentState(values["state"])
    values["requested_tools"] = tuple(values["requested_tools"])
    values["permissions"] = tuple(values["permissions"])
    values["memory_scope"] = tuple(values["memory_scope"])
    values["policy_receipt_refs"] = tuple(values["policy_receipt_refs"])
    values["run_ids"] = tuple(values["run_ids"])
    if isinstance(values["dependencies"], dict):
        values["dependencies"] = ManagedAgentDependencyRefs(
            template_id=values["dependencies"]["template_id"],
            mailbox_channel=values["dependencies"]["mailbox_channel"],
            sandbox_profile=values["dependencies"]["sandbox_profile"],
            route_ledger_ref=values["dependencies"]["route_ledger_ref"],
            watcher_policy_ref=values["dependencies"]["watcher_policy_ref"],
            automation_recipe_refs=tuple(values["dependencies"]["automation_recipe_refs"]),
            conversation_ref=values["dependencies"]["conversation_ref"],
            promotion_targets=tuple(values["dependencies"]["promotion_targets"]),
            memory_policy_ref=values["dependencies"]["memory_policy_ref"],
            monitoring_signal_ref=values["dependencies"]["monitoring_signal_ref"],
            resource_lease_ref=values["dependencies"]["resource_lease_ref"],
            trace_eval_ref=values["dependencies"]["trace_eval_ref"],
        )
    return ManagedAgentRecord(**values)


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
