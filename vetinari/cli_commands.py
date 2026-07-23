"""Core CLI command implementations for Vetinari.

Handles the fundamental operational commands: run, serve, start, status,
health, interactive, prompt versioning, and database migration.

This is step 2 of the CLI pipeline: argument parsing (cli.py) ->
**command execution** (cli_commands.py / cli_devops.py / cli_training.py).
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from typing import Any

from vetinari.cli_health_commands import run_health_check_quiet
from vetinari.cli_management_commands import cmd_capability_packs, cmd_migrate, cmd_prompt
from vetinari.constants import (
    MAIN_LOOP_POLL_INTERVAL,
    SHUTDOWN_TIMEOUT,
    THREAD_JOIN_TIMEOUT,
    THREAD_JOIN_TIMEOUT_SHORT,
    TRUNCATE_OUTPUT_PREVIEW,
    VETINARI_STARTUP_DELAY,
)

logger = logging.getLogger(__name__)


@contextmanager
def _suppress_status_info_logs(enabled: bool):
    if not enabled:
        yield
        return
    previous = logging.root.manager.disable
    logging.disable(logging.WARNING)
    try:
        yield
    finally:
        logging.disable(previous)


def _provider_health_label(provider_info: dict[str, Any]) -> str:
    metrics = provider_info.get("metrics", {}) if isinstance(provider_info, dict) else {}
    health = provider_info.get("health") if isinstance(provider_info, dict) else None
    return str(health or metrics.get("health_status") or "unknown")


def _health_failure_hint(reason: str = "") -> str:
    normalized = reason.lower()
    if "model" in normalized or "gguf" in normalized:
        return (
            "Run `vetinari models scan`, then verify VETINARI_MODELS_DIR points to a directory containing model files."
        )
    if "connection" in normalized or "unreachable" in normalized or "refused" in normalized:
        return "Start the configured local runtime or update the provider endpoint, then rerun `vetinari health`."
    return "Run `vetinari doctor --json` for structured diagnostics and follow the failing check hints."


def _native_kernel_server_command(*, web_host: str, port: int) -> list[str]:
    cargo = os.environ.get("VETINARI_CARGO", "cargo")
    return [
        cargo,
        "run",
        "-p",
        "amw-kernel",
        "--bin",
        "amw-kernel-server",
        "--",
        "--host",
        web_host,
        "--port",
        str(port),
    ]


def _resolve_web_port(value: int | str | None, *, env_var: str = "VETINARI_WEB_PORT") -> int:
    """Resolve and validate the dashboard port from CLI or environment."""
    raw_value: int | str = value if value is not None else os.environ.get(env_var, "5000")
    try:
        port = int(raw_value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{env_var} must be an integer between 1 and 65535, got {raw_value!r}") from exc
    if not 1 <= port <= 65535:
        raise ValueError(f"{env_var} must be between 1 and 65535, got {port}")
    return port


def _resolve_web_host(value: str | None, *, env_var: str = "VETINARI_WEB_HOST") -> str:
    """Resolve the dashboard bind address from CLI or environment."""
    return (value or os.environ.get(env_var, "127.0.0.1")).strip() or "127.0.0.1"


def cmd_run(args: Any) -> int:
    """Execute a goal or manifest task.

    When ``args.goal`` is set, routes through the two-layer orchestrator.
    Otherwise, uses the manifest-based orchestrator for task or full run.

    Args:
        args: Parsed CLI arguments with goal, task, config, mode, verbose.

    Returns:
        Exit code (0 for success, 1 for error).
    """
    from vetinari.cli_startup import _build_orchestrator, _check_drift_at_startup, _setup_logging

    _setup_logging(args.verbose)
    _check_drift_at_startup()

    if args.goal:
        # High-level goal → assembly-line pipeline
        import uuid as _uuid

        trace_id = str(_uuid.uuid4())[:12]
        print(f"[AM Workbench] Running goal: {args.goal[:80]}")
        print(f"[AM Workbench] Trace ID: {trace_id}")
        # Propagate trace_id to all log records for this goal execution
        _trace_logger = logging.LoggerAdapter(logger, {"trace_id": trace_id})
        _trace_logger.info("Starting goal execution with trace_id=%s", trace_id)
        try:
            from vetinari.orchestration.two_layer import get_two_layer_orchestrator

            orch = get_two_layer_orchestrator()
            # Wire agent context if orchestrator is available
            try:
                base_orch = _build_orchestrator(args.config, args.mode)
                orch.set_agent_context(base_orch._agent_context)
            except Exception:
                logger.warning("Could not wire agent context from base orchestrator", exc_info=True)
            results = orch.generate_and_execute(
                goal=args.goal,
                constraints={"mode": args.mode, "trace_id": trace_id},
            )
            print(f"\n[AM Workbench] Completed: {results.get('completed', 0)} tasks")
            print(f"[AM Workbench] Failed:    {results.get('failed', 0)} tasks")
            if results.get("final_output"):
                print("\n--- Final Output ---")
                print(str(results["final_output"])[:TRUNCATE_OUTPUT_PREVIEW])
            return 0
        except Exception as e:
            print(f"[AM Workbench] Error: {e}")
            logger.exception("Goal execution failed")
            return 1

    # Manifest-based task execution
    try:
        orch = _build_orchestrator(args.config, args.mode)
        if args.task:
            print(f"[AM Workbench] Running task: {args.task}")
            orch.run_task(args.task)
        else:
            print("[AM Workbench] Running all manifest tasks...")
            orch.run_all()
        return 0
    except Exception as e:
        print(f"[AM Workbench] Error: {e}")
        logger.exception("Run failed")
        return 1


def cmd_serve(args: Any) -> int:
    """Start the web dashboard.

    Args:
        args: Parsed CLI arguments with port, web_host, debug, verbose.

    Returns:
        Exit code (0 for success, 1 for error).
    """
    from vetinari.cli_startup import _setup_logging, _wire_subsystems

    _setup_logging(args.verbose)
    try:
        port = _resolve_web_port(args.port)
    except ValueError as exc:
        logger.warning("Invalid web port for serve command: %s", exc)
        print(f"[AM Workbench] Invalid web port: {exc}")
        return 1
    web_host = _resolve_web_host(getattr(args, "web_host", None))

    print(f"[AM Workbench] Starting native Rust kernel API server on {web_host}:{port}")
    print(f"[AM Workbench] Server URL: http://{web_host}:{port}")

    try:
        _wire_subsystems()
    except Exception as exc:
        logger.warning("Non-fatal: subsystem wiring failed: %s", exc)

    try:
        subprocess.run(  # noqa: S603 - command is built from the bundled Rust kernel binary and validated host/port.
            _native_kernel_server_command(web_host=web_host, port=port),
            check=True,
        )
        return 0
    except (OSError, subprocess.CalledProcessError) as e:
        print(f"[AM Workbench] Native Rust kernel server unavailable: {e}")
        print("[AM Workbench] Build the Rust workspace with `cargo build -p amw-kernel` and retry.")
        logger.warning(
            "Native Rust kernel server failed to start on port %d: %s - CLI still functional",
            port,
            e,
        )
        return 1


def _run_startup_preflight(args: Any) -> bool:
    if getattr(args, "skip_preflight", False):
        return True
    try:
        from vetinari.preflight import run_preflight

        report = run_preflight(interactive=sys.stdin.isatty())
        missing_required = [
            item.package
            for item in getattr(report, "dependency_matrix", [])
            if getattr(item, "status", "") == "missing-required"
        ]
    except Exception as exc:
        logger.warning("Preflight check failed before startup", exc_info=True)
        print(f"[AM Workbench] Startup preflight failed: {exc}")
        return False
    if not missing_required:
        return True
    print("[AM Workbench] Startup blocked: missing required dependencies: " + ", ".join(missing_required))
    print("[AM Workbench] Install the required packages or rerun with --skip-preflight for diagnostics only.")
    return False


def _auto_run_init_wizard_if_needed(args: Any) -> bool:
    if not getattr(args, "auto_init_if_needed", False):
        return False
    from vetinari.setup import init_wizard

    config_path = init_wizard.DEFAULT_CONFIG_PATH
    if config_path.exists():
        return False
    print("[AM Workbench] First-run config missing; running vetinari init --skip-download.")
    init_wizard.run_wizard(skip_download=True, non_interactive=True, config_path=config_path)
    return True


def _start_dashboard_thread(args: Any, *, web_host: str, port: int) -> tuple[bool, threading.Thread | None]:
    if args.no_dashboard:
        return False, None
    try:
        command = _native_kernel_server_command(web_host=web_host, port=port)

        def _run_dashboard() -> None:
            subprocess.run(  # noqa: S603 - command is built from the bundled Rust kernel binary and validated host/port.
                command,
                check=False,
            )

        thread_name = "native-kernel-server"

        dashboard_thread = threading.Thread(target=_run_dashboard, daemon=True, name=thread_name)
        dashboard_thread.start()
        time.sleep(VETINARI_STARTUP_DELAY)  # Give the native server time to start.
        if not dashboard_thread.is_alive():
            print(f"[AM Workbench] Native API server startup failed - server exited (port {port} may be in use)")
            return False, dashboard_thread

        from vetinari.system import health_monitor

        health_monitor.register_dashboard_thread(dashboard_thread)
        print(f"[AM Workbench] Local API server started: http://{web_host}:{port}")
        return True, dashboard_thread
    except Exception as exc:
        logger.warning("Exception handled by  start dashboard thread fallback", exc_info=True)
        print(f"[AM Workbench] Local API server unavailable: {exc}")
        return False, None


def _start_auto_tuner_thread() -> tuple[threading.Event, threading.Thread]:
    shutdown_event = threading.Event()

    def _auto_tuner_loop() -> None:
        while not shutdown_event.is_set():
            shutdown_event.wait(timeout=SHUTDOWN_TIMEOUT)  # 15 min, interruptible.
            if shutdown_event.is_set():
                break
            try:
                from vetinari.learning.auto_tuner import get_auto_tuner

                get_auto_tuner().run_cycle()
                logger.debug("[AutoTuner] Periodic cycle complete")
            except Exception as exc:
                logger.warning("[AutoTuner] Cycle error (non-fatal): %s", exc)

    tuner_thread = threading.Thread(target=_auto_tuner_loop, daemon=True, name="auto-tuner")
    tuner_thread.start()
    return shutdown_event, tuner_thread


def _wait_for_dashboard_shutdown(
    *,
    web_host: str,
    port: int,
    shutdown_event: threading.Event,
    tuner_thread: threading.Thread,
    dashboard_thread: threading.Thread | None,
) -> None:
    print(f"\n[AM Workbench] Local API server running at http://{web_host}:{port}")
    print("[AM Workbench] Press Ctrl+C to exit")
    try:
        while True:
            time.sleep(MAIN_LOOP_POLL_INTERVAL)
    except KeyboardInterrupt:
        print("\n[AM Workbench] Shutting down...")
        shutdown_event.set()
        tuner_thread.join(timeout=THREAD_JOIN_TIMEOUT)
        if dashboard_thread is not None and dashboard_thread.is_alive():
            dashboard_thread.join(timeout=THREAD_JOIN_TIMEOUT_SHORT)


def cmd_start(args: Any) -> int:
    """Start CLI + optional web dashboard (recommended entry point).

    Args:
        args: Parsed CLI arguments with goal, task, port, no_dashboard, web_host, mode, verbose.

    Returns:
        Exit code (0 for success, 1 for error).
    """
    from vetinari.cli_startup import (
        _check_drift_at_startup,
        _print_banner,
        _setup_logging,
        _wire_subsystems,
    )

    _setup_logging(args.verbose)
    _check_drift_at_startup()
    _print_banner(args.mode)
    _auto_run_init_wizard_if_needed(args)
    try:
        port = _resolve_web_port(args.port)
    except ValueError as exc:
        logger.warning("Invalid web port for start command: %s", exc)
        print(f"[AM Workbench] Invalid web port: {exc}")
        return 1

    if not _run_startup_preflight(args):
        return 1

    _wire_subsystems()

    # Default to loopback — require explicit opt-in for network binding
    web_host = _resolve_web_host(getattr(args, "web_host", None))
    dashboard_started, dashboard_thread = _start_dashboard_thread(args, web_host=web_host, port=port)

    print("\n[AM Workbench] Running startup health checks...")
    if not _health_check_quiet():
        print("[AM Workbench] Startup health checks reported a degraded or failed subsystem.")

    shutdown_event, tuner_thread = _start_auto_tuner_thread()

    if args.goal:
        return cmd_run(args)

    if args.task:
        return cmd_run(args)

    if dashboard_started:
        _wait_for_dashboard_shutdown(
            web_host=web_host,
            port=port,
            shutdown_event=shutdown_event,
            tuner_thread=tuner_thread,
            dashboard_thread=dashboard_thread,
        )
    else:
        return cmd_interactive(args)

    return 0


def cmd_status(args: Any) -> int:
    """Show system status: models loaded, providers, and learning state.

    Args:
        args: Parsed CLI arguments with config, verbose.

    Returns:
        Exit code (0 for success, 1 for error).
    """
    from vetinari.cli_startup import _setup_logging

    _setup_logging(args.verbose)
    previous_log_disable = logging.root.manager.disable
    if not getattr(args, "verbose", False):
        logging.disable(logging.WARNING)

    print("\n[AM Workbench] System Status")
    print(f"  Config:         {args.config}")

    # Check local inference adapter
    try:
        from vetinari.adapters.adapter_cache import get_local_inference_adapter

        adapter = get_local_inference_adapter("cli-status")
        models = adapter.list_loaded_models()
        print(f"  Models loaded:  {len(models)}")
        for m in models[:5]:
            mid = m.get("id", m.get("model", "unknown")) if isinstance(m, dict) else str(m)
            print(f"    - {mid}")
    except Exception as e:
        print(f"  Local inference: UNREACHABLE ({e})")
        try:
            from vetinari.errors import find_remediation

            hint = find_remediation(str(e))
            if hint:
                suggested_action = getattr(hint, "suggested_action", None)
                if suggested_action:
                    print(f"    Hint: {suggested_action}")
        except Exception:
            logger.warning(
                "Remediation hint lookup failed for error %r — status output continues without hint",
                str(e),
                exc_info=True,
            )

    # Adapter manager status
    try:
        from vetinari.adapter_manager import get_adapter_manager

        mgr = get_adapter_manager()
        status = mgr.get_status()
        providers = status.get("providers", {})
        print(f"\n  Providers: {len(providers)}")
        for pname, pinfo in list(providers.items())[:5]:
            health = _provider_health_label(pinfo)
            print(f"    - {pname}: {health}")
    except Exception as e:
        print(f"  Adapter Manager: {e}")

    # Learning system status
    try:
        from vetinari.learning.model_selector import get_thompson_selector

        selector = get_thompson_selector()
        total_arms = len(selector._arms)
        total_pulls = sum(a.total_pulls for a in selector._arms.values())
        print(f"\n  Thompson Sampling: {total_arms} arms, {total_pulls} total pulls")
    except Exception as e:
        print(f"  Learning System: {e}")

    if not getattr(args, "verbose", False):
        logging.disable(previous_log_disable)
    return 0


def cmd_audit_results(args: Any) -> int:
    """Display full-spectrum audit results from the on-disk run index.

    When ``args.run_id`` is set, shows detail for that single run with optional
    finding filters (status, severity, lane, text query).  Otherwise prints a
    summary table of recent runs.

    Args:
        args: Parsed CLI arguments with limit, include_archived, run_id,
            finding_limit, finding_status, severity, lane, query, json.

    Returns:
        Exit code (0 for success, 1 for error).
    """
    import json as _json

    from vetinari import audit_results

    run_id: str | None = getattr(args, "run_id", None)
    include_archived: bool = getattr(args, "include_archived", False)
    as_json: bool = getattr(args, "json", False)

    try:
        if run_id:
            payload = audit_results.load_full_spectrum_audit_run(
                run_id=run_id,
                include_archived=include_archived,
                finding_limit=getattr(args, "finding_limit", 50),
                finding_status=getattr(args, "finding_status", "open"),
                severity=getattr(args, "severity", None),
                lane=getattr(args, "lane", None),
                query=getattr(args, "query", None),
            )
            if as_json:
                print(_json.dumps(payload))
            else:
                run = payload.get("run", {})
                print(f"Full-spectrum audit run: {run.get('run_id', run_id)}")
                print(
                    f"  Status: {run.get('status', 'unknown')}  Phase: {run.get('phase', 'unknown')}  Round: {run.get('current_round', '?')}"
                )
                print(
                    f"  Findings: {run.get('finding_result_count', 0)} shown / {run.get('finding_count', 0)} total  Open: {run.get('open_findings', 0)}"
                )
                for finding in run.get("findings", []):
                    status_label = finding.get("closure_status") or finding.get("status") or "?"
                    print(
                        f"  [{finding.get('severity', '?').upper():<8}] {finding.get('id', '?')}: {finding.get('title', '?')}  ({status_label})"
                    )
        else:
            payload = audit_results.load_full_spectrum_audit_results(
                limit=getattr(args, "limit", 10),
                include_archived=include_archived,
            )
            if as_json:
                print(_json.dumps(payload))
            else:
                summary = payload.get("summary", {})
                print(f"Full-spectrum audit results  ({summary.get('visible_runs', 0)} runs)")
                for run in payload.get("runs", []):
                    print(
                        f"  {run.get('run_id', '?')}  status={run.get('status', '?')}  open={run.get('open_findings', 0)}/{run.get('finding_count', 0)}"
                    )
                    for finding in run.get("top_findings", []):
                        print(
                            f"    [{finding.get('severity', '?').upper():<8}] {finding.get('id', '?')}: {finding.get('title', '?')}"
                        )
    except Exception as exc:
        logger.exception("Audit results command failed: %s", exc)
        return 1
    return 0


def cmd_health(args: Any) -> int:
    """Run health checks on all providers and print a summary.

    Args:
        args: Parsed CLI arguments with verbose.

    Returns:
        Exit code (0 for success, 1 for error).
    """
    from vetinari.cli_startup import _setup_logging

    _setup_logging(args.verbose)

    print("[AM Workbench] Running health checks...")
    return 0 if _health_check_quiet() else 1


def cmd_interactive(args: Any) -> int:
    """Enter interactive REPL mode for iterative goal execution.

    Accepts goals via stdin and dispatches them through the two-layer
    orchestrator.  Special commands: /quit, /status, /review, /help.

    Args:
        args: Parsed CLI arguments with config, mode, verbose.

    Returns:
        Exit code (0 for success, 1 for error).
    """
    from vetinari.cli_startup import _build_orchestrator, _setup_logging

    _setup_logging(args.verbose)

    print("[AM Workbench] Interactive mode. Type your goal and press Enter.")
    print("Commands: /quit, /status, /review, /help")
    print("-" * 50)

    try:
        from vetinari.orchestration.two_layer import get_two_layer_orchestrator

        orch = get_two_layer_orchestrator()
        try:
            base_orch = _build_orchestrator(args.config, args.mode)
            orch.set_agent_context(base_orch._agent_context)
        except Exception:
            logger.warning("Could not wire agent context from base orchestrator", exc_info=True)
    except Exception:
        logger.warning("Two-layer orchestrator unavailable for interactive mode", exc_info=True)
        orch = None

    while True:
        try:
            goal = input("\nGoal> ").strip()
        except (EOFError, KeyboardInterrupt):
            logger.warning("Interactive mode interrupted by user — exiting")
            print("\n[AM Workbench] Exiting interactive mode.")
            return 0

        if not goal:
            continue
        if goal.lower() in ("/quit", "/exit", "quit", "exit"):
            print("[AM Workbench] Goodbye.")
            return 0
        if goal.lower() == "/status":
            cmd_status(args)
            continue
        if goal.lower() == "/review":
            from vetinari.cli_devops import cmd_review

            cmd_review(args)
            continue
        if goal.lower() == "/help":
            print("  /quit   - Exit")
            print("  /status - Show system status")
            print("  /review - Run self-improvement review")
            print("  Any other text - Execute as a goal")
            continue

        print(f"\n[AM Workbench] Working on: {goal[:60]}...")
        try:
            if orch:
                results = orch.generate_and_execute(goal=goal, constraints={"mode": args.mode})
                print(f"\n  Completed: {results.get('completed', 0)} tasks")
                if results.get("final_output"):
                    print("\n--- Output ---")
                    print(str(results["final_output"])[:1500])
            else:
                print("[AM Workbench] Orchestrator not available. Check local inference adapter.")
        except Exception as e:
            print(f"[AM Workbench] Error: {e}")
            logger.warning("Interactive execution error", exc_info=True)


def _health_check_quiet() -> bool:
    """Run health checks on all providers and print results to stdout."""
    return run_health_check_quiet(_health_failure_hint)


__all__ = [
    "_health_check_quiet",
    "_resolve_web_host",
    "_resolve_web_port",
    "cmd_audit_results",
    "cmd_capability_packs",
    "cmd_health",
    "cmd_interactive",
    "cmd_migrate",
    "cmd_prompt",
    "cmd_run",
    "cmd_serve",
    "cmd_start",
    "cmd_status",
]
