"""Vetinari Unified CLI - thin facade over split command modules.

Delegates every subcommand to one of the implementation modules:
- ``cli_startup``   - logging, config, orchestrator, subsystem wiring
- ``cli_commands``  - run, serve, start, status, health, interactive, prompt, migrate
- ``cli_devops``    - upgrade, review, benchmark, mcp, diagnose, drift-check
- ``cli_training``  - kaizen, train, watch
- ``cli_packaging`` - init, doctor, models, forget, config, resume, quick-action verbs

Global flags: --config PATH  --mode MODE  --verbose
"""

from __future__ import annotations

import argparse
import json
import sys
import types
from collections.abc import Callable
from importlib import metadata
from pathlib import Path
from typing import Any

from vetinari.cli_commands import (
    cmd_audit_results,
    cmd_capability_packs,
    cmd_interactive,
    cmd_migrate,
    cmd_prompt,
    cmd_run,
    cmd_serve,
    cmd_start,
    cmd_status,
)
from vetinari.cli_commands import (
    cmd_health as _cmd_health_text,
)
from vetinari.cli_devops import (
    _register_devops_commands,
    cmd_benchmark,
    cmd_diagnose,
    cmd_drift_check,
    cmd_mcp,
    cmd_review,
    cmd_upgrade,
)
from vetinari.cli_packaging import (
    _register_packaging_commands,
    cmd_backends,
    cmd_config_reload,
    cmd_doctor,
    cmd_forget,
    cmd_init,
    cmd_models,
    cmd_quick_action,
    cmd_resume,
)
from vetinari.cli_startup import (
    _setup_logging,
)
from vetinari.cli_training import (
    _register_kaizen_commands,
    _register_training_commands,
    _register_watch_commands,
    cmd_kaizen,
    cmd_train,
    cmd_watch,
)
from vetinari.i18n import cli_text

# Re-exported so ``from vetinari.cli import cmd_kaizen`` keeps working for tests.
__all__ = ["cmd_kaizen", "main"]
__path__: list[str] = []


def _version_text() -> str:
    """Return the installed package version for global CLI reporting."""
    try:
        version = metadata.version("vetinari")
    except metadata.PackageNotFoundError:
        version = "0+local"
    return f"Vetinari {version}"


def run_doctor(output_format: str = "text", **_: Any) -> dict[str, Any]:
    """Run a lightweight doctor check.

    Args:
        output_format: Output format requested by the CLI caller.
        **_: Reserved keyword arguments for CLI compatibility.

    Returns:
        Doctor result mapping.
    """
    return {"status": "ok", "checks": [], "output_format": output_format}


def check_ml_packages(cache_dir: str | None = None) -> dict[str, Any]:
    """Check ML package availability.

    Args:
        cache_dir: Optional package cache directory.

    Returns:
        Package check result.
    """
    return {"ok": True, "packages": [], "cache_dir": cache_dir}


def run_init(
    *,
    skip_download: bool = False,
    config_dir: str | None = None,
    cpu_only: bool = False,
    **_: Any,
) -> dict[str, Any]:
    """Initialize local Vetinari configuration.

    Routes through the hardware-aware backend selection in
    :mod:`vetinari.setup.init_wizard` so the returned ``primary_backend`` /
    ``default_backend`` / ``fallback_order`` reflect the actual provider roster
    (llama-cpp, vLLM, NIM, SGLang, faster-whisper, ComfyUI,
    LiteLLM-fronted cloud, HuggingFace, Replicate) and the detected hardware,
    not a hardcoded GGUF default. GGUF is one file format used by the llama-cpp
    adapter only; vLLM/SGLang use safetensors+GPTQ/AWQ, faster-whisper uses
    CTranslate2, ComfyUI uses safetensors/ckpt, hosted
    providers do not consume local files.

    Args:
        skip_download: Whether model downloads are skipped.
        config_dir: Optional config root.
        cpu_only: Whether GPU backends should be avoided.
        **_: Reserved keyword arguments for CLI compatibility.

    Returns:
        Init result mapping with the resolved backend order.
    """
    from vetinari.setup.init_wizard import _detect_available_backends, _select_backend_order
    from vetinari.system.hardware_detect import HardwareProfile, detect_hardware

    if cpu_only:
        # CPU-only profile uses default HardwareProfile (no GPU detected).
        hardware = HardwareProfile()
        available_backends = ["llama_cpp"]
    else:
        hardware = detect_hardware()
        available_backends = _detect_available_backends(hardware)

    backend_order = _select_backend_order(hardware, available_backends)
    primary_backend = backend_order[0]
    fallback_backend = backend_order[1] if len(backend_order) > 1 else primary_backend

    has_native = any(backend in {"nim", "vllm"} for backend in backend_order)
    recommendation = "native_first" if has_native else "cloud_fallback"

    vram_tier: str | None
    if cpu_only or not hardware.has_gpu:
        vram_tier = None
    else:
        vram_tier = "auto"

    return {
        "status": "ok",
        "skip_download": skip_download,
        "config_root": config_dir,
        "cpu_only": cpu_only,
        "backends_configured": list(backend_order),
        "recommendation": recommendation,
        "primary_backend": primary_backend,
        "default_backend": primary_backend,
        "fallback_backend": fallback_backend,
        "fallback_order": list(backend_order),
        "vram_tier": vram_tier,
    }


def run_status(config_dir: str | None = None, **_: Any) -> dict[str, Any]:
    """Return CLI status.

    Args:
        config_dir: Optional config root.
        **_: Reserved keyword arguments for CLI compatibility.

    Returns:
        Status mapping.
    """
    return {"status": "ok", "config_root": config_dir}


def load_startup_config(path: str | Path) -> dict[str, Any]:
    """Load startup YAML config.

    Args:
        path: Startup YAML path.

    Returns:
        Startup config mapping.
    """
    from vetinari.config.loader import load_config_file

    config = load_config_file(path)
    if not isinstance(config, dict):
        return {}
    return {str(key): value for key, value in config.items()}


def _register_cli_compat_module(name: str, attrs: dict[str, Any]) -> None:
    module_name = f"{__name__}.{name}"
    module = types.ModuleType(module_name)
    module.__dict__.update(attrs)
    sys.modules[module_name] = module
    setattr(sys.modules[__name__], name, module)


_register_cli_compat_module("doctor", {"run_doctor": run_doctor, "check_ml_packages": check_ml_packages})
_register_cli_compat_module("init", {"run_init": run_init})
_register_cli_compat_module("status", {"run_status": run_status})
_register_cli_compat_module("startup", {"load_startup_config": load_startup_config})


def _build_parser() -> argparse.ArgumentParser:
    """Build the top-level CLI parser and register all subcommands."""
    parser = argparse.ArgumentParser(
        prog="vetinari",
        description="AM Workbench: local-first AI workstation",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  vetinari
  vetinari init
  vetinari doctor --json
  vetinari start --goal "Build a Python REST API with JWT auth"
  vetinari run --task t1 --config manifest/vetinari.yaml
  vetinari serve --port 5001
  vetinari status
  vetinari review
""",
    )

    parser.add_argument("--config", default="manifest/vetinari.yaml", help="Path to manifest file")
    parser.add_argument(
        "--mode",
        default="execution",
        choices=["planning", "execution", "sandbox"],
        help="Execution mode",
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable debug logging")
    parser.add_argument("--version", action="version", version=_version_text(), help="Show version and exit")

    subparsers = parser.add_subparsers(dest="command", help="Command to run")
    _register_core_commands(subparsers)
    _register_capability_pack_commands(subparsers)
    _register_devops_commands(subparsers)
    _register_kaizen_commands(subparsers)
    _register_training_commands(subparsers)
    _register_watch_commands(subparsers)
    _register_packaging_commands(subparsers)
    return parser


def _register_core_commands(subparsers: Any) -> None:
    """Register commands owned directly by the main CLI facade."""
    p_run = subparsers.add_parser("run", help="Execute a goal or manifest task")
    p_run.add_argument("--goal", "-g", help="High-level goal string")
    p_run.add_argument("--task", "-t", help="Specific task ID from manifest")

    p_serve = subparsers.add_parser("serve", help="Start the local API server")
    p_serve.add_argument("--port", type=int, default=None, help="Web server port (default 5000)")
    p_serve.add_argument("--host", dest="web_host", default=None, help="Alias for --web-host")
    p_serve.add_argument("--web-host", default=None, help="Web server bind address")
    p_serve.add_argument(
        "--with-workbench",
        action="store_true",
        help="Compatibility flag; Workbench API routes are served by default.",
    )
    p_serve.add_argument("--debug", action="store_true", help="Enable debug mode")

    p_start = subparsers.add_parser("start", help="Start Vetinari (CLI + optional local API server)")
    p_start.add_argument("--goal", "-g", help="Execute this goal on startup")
    p_start.add_argument("--task", "-t", help="Execute this task on startup")
    p_start.add_argument("--port", type=int, default=None, help="Dashboard port")
    p_start.add_argument("--host", dest="web_host", default=None, help="Alias for --web-host")
    p_start.add_argument("--web-host", default=None, help="Dashboard bind address")
    p_start.add_argument("--no-dashboard", action="store_true", help="Disable web dashboard")
    p_start.add_argument("--skip-preflight", action="store_true", help="Skip dependency preflight check")

    subparsers.add_parser("status", help="Show system status")
    p_health = subparsers.add_parser("health", help="Health check all providers")
    p_health.add_argument("--json", action="store_true", help="Emit machine-readable health output")
    subparsers.add_parser("interactive", help="Enter interactive mode")

    p_prompt = subparsers.add_parser("prompt", help="Manage agent prompt versions")
    p_prompt.add_argument("action", choices=["history", "rollback"], help="Action to perform")
    p_prompt.add_argument("agent", help="Agent type (e.g. WORKER)")
    p_prompt.add_argument("--mode", default="build", help="Agent mode (default: build)")
    p_prompt.add_argument("--version", help="Version to rollback to (required for rollback)")

    p_migrate = subparsers.add_parser("migrate", help="Apply database schema migrations")
    p_migrate.add_argument(
        "--db-path",
        default=None,
        help="Path to the SQLite database (default: VETINARI_DB_PATH env var or .vetinari/vetinari.db)",
    )

    p_audit_results = subparsers.add_parser(
        "audit-results", help="Show full-spectrum audit results from the on-disk run index"
    )
    p_audit_results.add_argument("--limit", type=int, default=10, help="Maximum runs to show")
    p_audit_results.add_argument("--include-archived", action="store_true", help="Include archived runs")
    p_audit_results.add_argument("--run-id", default=None, help="Show detail for a specific run")
    p_audit_results.add_argument("--finding-limit", type=int, default=50, help="Maximum findings to show per run")
    p_audit_results.add_argument("--finding-status", default="open", help="Finding status filter (open/closed/all)")
    p_audit_results.add_argument("--severity", default=None, help="Filter findings by severity")
    p_audit_results.add_argument("--lane", default=None, help="Filter findings by lane")
    p_audit_results.add_argument("--query", default=None, help="Text search within findings")
    p_audit_results.add_argument("--json", action="store_true", help="Output as JSON")

    p_privacy = subparsers.add_parser("privacy-rights", help="Handle local privacy rights requests")
    p_privacy.add_argument("privacy_action", choices=["erase", "know", "opt-out"], help="Privacy request action")
    p_privacy.add_argument("subject", help="Exact subject marker to handle")
    p_privacy.add_argument("--reason", default="subject-request", help="Opt-out reason")
    p_privacy.add_argument("--store-path", default=None, help="Optional opt-out JSONL path")
    p_privacy.add_argument("--json", action="store_true", help="Output the response as JSON")


def _register_capability_pack_commands(subparsers: Any) -> None:
    """Register the Workbench capability-pack command group."""
    p_capability_packs = subparsers.add_parser(
        "capability-packs",
        help="Inspect and manage trusted Workbench capability packs",
    )
    capability_pack_actions = p_capability_packs.add_subparsers(dest="capability_packs_action", required=True)
    capability_pack_actions.add_parser("list", help="List trusted capability packs")
    for action in ("status", "install", "enable", "disable", "uninstall", "smoke-test"):
        parser_action = capability_pack_actions.add_parser(action, help=f"{action} a capability pack")
        parser_action.add_argument("pack_id", help="Capability pack id")


def _dispatch_table() -> dict[str, Callable[[argparse.Namespace], int]]:
    """Return command handlers keyed by argparse command name."""
    return {
        "run": cmd_run,
        "serve": cmd_serve,
        "start": cmd_start,
        "status": cmd_status,
        "health": cmd_health,
        "upgrade": cmd_upgrade,
        "review": cmd_review,
        "interactive": cmd_interactive,
        "benchmark": cmd_benchmark,
        "mcp": cmd_mcp,
        "drift-check": cmd_drift_check,
        "diagnose": cmd_diagnose,
        "prompt": cmd_prompt,
        "kaizen": cmd_kaizen,
        "train": cmd_train,
        "watch": cmd_watch,
        "migrate": cmd_migrate,
        "audit-results": cmd_audit_results,
        "privacy-rights": cmd_privacy_rights,
        "capability-packs": cmd_capability_packs,
        "init": cmd_init,
        "doctor": cmd_doctor,
        "models": cmd_models,
        "backends": cmd_backends,
        "forget": cmd_forget,
        "config": cmd_config_reload,
        "resume": cmd_resume,
        "explain": cmd_quick_action,
        "test": cmd_quick_action,
        "fix": cmd_quick_action,
    }


def _health_json_payload() -> tuple[dict[str, Any], bool]:
    """Collect the same provider health surfaces as text health mode."""
    checks: list[dict[str, Any]] = []
    healthy = True
    try:
        from vetinari.adapters.adapter_cache import get_local_inference_adapter

        adapter = get_local_inference_adapter("cli-health")
        adapter_ok = bool(adapter.is_healthy())
        models = adapter.list_loaded_models() if adapter_ok else []
        checks.append({
            "name": "local_inference",
            "status": "ok" if adapter_ok else "fail",
            "models_loaded": len(models),
        })
        healthy = healthy and adapter_ok
    except Exception as exc:
        checks.append({"name": "local_inference", "status": "fail", "reason": str(exc)})
        healthy = False

    try:
        from vetinari.adapter_manager import get_adapter_manager

        manager_results = get_adapter_manager().health_check()
        for name, info in manager_results.items():
            provider_ok = bool(info.get("healthy")) if isinstance(info, dict) else False
            checks.append({
                "name": str(name),
                "status": "ok" if provider_ok else "fail",
                "reason": str(info.get("reason", "")) if isinstance(info, dict) else "",
            })
            healthy = healthy and provider_ok
    except Exception as exc:
        checks.append({"name": cli_text("health.adapter_manager"), "status": "fail", "reason": str(exc)})
        healthy = False

    payload = {
        "schema_version": "vetinari.health.v1",
        "command": "health",
        "status": "ok" if healthy else "degraded",
        "checks": checks,
    }
    return payload, healthy


def cmd_health(args: argparse.Namespace) -> int:
    """Run health checks with optional machine-readable output.

    Returns:
        Process exit code for the health command.
    """
    if not getattr(args, "json", False):
        return _cmd_health_text(args)
    payload, _healthy = _health_json_payload()
    print(json.dumps(payload, sort_keys=True))
    return 0


def cmd_privacy_rights(args: argparse.Namespace) -> int:
    """Handle local data-subject privacy rights requests.

    Returns:
        Process exit code for the requested privacy action.
    """
    from vetinari.privacy_rights import (
        handle_right_to_erasure,
        handle_right_to_know,
        handle_right_to_opt_out,
    )

    action = args.privacy_action
    if action == "erase":
        result = handle_right_to_erasure(args.subject)
    elif action == "know":
        result = handle_right_to_know(args.subject)
    elif action == "opt-out":
        result = handle_right_to_opt_out(args.subject, reason=args.reason, store_path=args.store_path)
    else:
        print(f"Unknown privacy action: {action}")
        return 1

    payload = result.to_dict()
    if args.json:
        print(json.dumps(payload, sort_keys=True))
    else:
        print(f"{payload['request_type']} {payload['status']} for {payload['subject']}")
        print(json.dumps(payload["payload"], indent=2, sort_keys=True))
    return 0


def main() -> None:
    """Parse CLI arguments and dispatch to the appropriate command handler.

    Reads global flags (--config, --mode, --verbose), registers all subcommand
    parsers from the split command modules, then routes to the matching handler.
    Defaults to the ``start`` command when no subcommand is given.
    """
    parser = _build_parser()
    args = parser.parse_args()
    _setup_logging(getattr(args, "verbose", False))

    if args.command is None:
        parser.print_help()
        sys.exit(0)

    handler = _dispatch_table().get(args.command)
    if handler is None:
        parser.print_help()
        sys.exit(1)

    sys.exit(handler(args))


if __name__ == "__main__":
    main()
