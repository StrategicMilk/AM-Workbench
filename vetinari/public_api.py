"""Static public API registry used by type checkers and wiring audits."""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING

from vetinari.skills.catalog_loader import get_catalog_by_agent, get_catalog_by_capability, get_catalog_by_tag

_LAZY_EXPORTS = {
    "available_install_scripts": ("vetinari.setup.backend_installer", "available_install_scripts"),
    "clear_agent_control_state": ("vetinari.orchestration.agent_control", "clear_agent_control_state"),
    "detect_merge_conflicts": ("vetinari.project.git_integration", "detect_merge_conflicts"),
    "failure_registry": ("vetinari.diagnostics.failure_registry", None),
    "generate_commit_message_for_path": ("vetinari.project.git_integration", "generate_commit_message_for_path"),
    "get_agent_dashboard": ("vetinari.dashboard.agent_dashboard", "get_agent_dashboard"),
    "get_gate_runner": ("vetinari.workflow.quality_gates", "get_gate_runner"),
    "get_quality_drift_stats": ("vetinari.analytics.wiring", "get_quality_drift_stats"),
    "get_structural_map": ("vetinari.code_search", "get_structural_map"),
    "gpu": ("vetinari.hardware.gpu", None),
    "handle_mcp_resources_list": ("vetinari.mcp.http_transport", "handle_mcp_resources_list"),
    "normalize_collaboration_user_id": ("vetinari.workbench.collaboration.runtime", "normalize_collaboration_user_id"),
    "package_repair": ("vetinari.startup.package_repair", None),
    "quantize_model": ("vetinari.model_discovery_downloads", "quantize_model"),
    "redirect_agent": ("vetinari.orchestration.agent_control", "redirect_agent"),
    "register_webhook_event_callback": ("vetinari.notifications.webhook", "register_webhook_event_callback"),
    "run_arena_match": ("vetinari.evaluation.arena", "run_arena_match"),
    "run_automated_eval": ("vetinari.evaluation.arena", "run_automated_eval"),
    "run_install_script": ("vetinari.setup.backend_installer", "run_install_script"),
    "user_span": ("vetinari.observability.tracing", "user_span"),
    "validate_key_value": ("vetinari.config.schema", "validate_key_value"),
}


def __getattr__(name: str) -> object:
    try:
        module_name, attribute_name = _LAZY_EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(name) from exc
    module = import_module(module_name)
    value = module if attribute_name is None else getattr(module, attribute_name)
    globals()[name] = value
    return value


if TYPE_CHECKING:
    from vetinari.analytics.wiring import get_quality_drift_stats
    from vetinari.code_search import get_structural_map
    from vetinari.config.schema import validate_key_value
    from vetinari.dashboard.agent_dashboard import get_agent_dashboard
    from vetinari.diagnostics import failure_registry
    from vetinari.evaluation.arena import run_arena_match, run_automated_eval
    from vetinari.hardware import gpu
    from vetinari.mcp.http_transport import handle_mcp_resources_list
    from vetinari.model_discovery_downloads import quantize_model
    from vetinari.notifications.webhook import register_webhook_event_callback
    from vetinari.observability.tracing import user_span
    from vetinari.orchestration.agent_control import clear_agent_control_state, redirect_agent
    from vetinari.project.git_integration import detect_merge_conflicts, generate_commit_message_for_path
    from vetinari.setup.backend_installer import available_install_scripts, run_install_script
    from vetinari.startup import package_repair
    from vetinari.workbench.collaboration.runtime import normalize_collaboration_user_id
    from vetinari.workflow.quality_gates import get_gate_runner

__all__ = [
    "available_install_scripts",
    "clear_agent_control_state",
    "detect_merge_conflicts",
    "failure_registry",
    "generate_commit_message_for_path",
    "get_agent_dashboard",
    "get_catalog_by_agent",
    "get_catalog_by_capability",
    "get_catalog_by_tag",
    "get_gate_runner",
    "get_quality_drift_stats",
    "get_structural_map",
    "gpu",
    "handle_mcp_resources_list",
    "normalize_collaboration_user_id",
    "package_repair",
    "quantize_model",
    "redirect_agent",
    "register_webhook_event_callback",
    "run_arena_match",
    "run_automated_eval",
    "run_install_script",
    "user_span",
    "validate_key_value",
]
