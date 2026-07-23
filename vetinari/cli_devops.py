"""DevOps and maintenance CLI commands for Vetinari.

Handles system health, model management, contract drift, MCP server
integration, diagnostics, and the self-improvement review cycle.

This module is part of the CLI pipeline:
argument parsing (cli.py) -> **devops commands** (cli_devops.py).
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

from vetinari.types import AgentType

logger = logging.getLogger(__name__)


def cmd_upgrade(args: Any) -> int:
    """Check for model upgrades by discovering available local models.

    Queries each configured adapter (llama-cpp, LiteLLM, etc.) for its
    available models and prints a summary. This is used to verify that
    newly downloaded GGUF files or newly configured cloud providers are
    visible to the system.

    Args:
        args: Parsed CLI arguments with config, mode, verbose.

    Returns:
        Exit code (0 for success, 1 for error).
    """
    from vetinari.cli_startup import _setup_logging

    _setup_logging(args.verbose)

    try:
        from vetinari.adapter_manager import get_adapter_manager

        mgr = get_adapter_manager()
        # discover_models() returns dict[provider_name, list[ModelInfo]]
        discovered = mgr.discover_models()
        total = sum(len(v) for v in discovered.values())
        print(f"[AM Workbench] Discovered {total} models across {len(discovered)} provider(s)")
        for provider, models in discovered.items():
            if models:
                print(f"  [{provider}]")
                for m in models:
                    size = f"{m.memory_gb} GB" if m.memory_gb else "size unknown"
                    print(f"    - {m.name or m.id} ({size})")
        print("[AM Workbench] Upgrade check complete")
        return 0
    except Exception as exc:
        print(f"[AM Workbench] Upgrade check failed: {exc}")
        logger.warning(
            "cmd_upgrade failed during model discovery — check adapter configuration: %s",
            exc,
        )
        return 1


def cmd_review(args: Any) -> int:
    """Run the self-improvement agent to generate performance recommendations.

    Args:
        args: Parsed CLI arguments with verbose.

    Returns:
        Exit code (0 for success, 1 for error).
    """
    from vetinari.cli_startup import _setup_logging

    _setup_logging(args.verbose)

    print("[AM Workbench] Running self-improvement review...")
    try:
        from vetinari.adapter_manager import get_adapter_manager
        from vetinari.agents import get_worker_agent
        from vetinari.agents.contracts import AgentTask

        agent = get_worker_agent()
        try:
            agent.initialize({"adapter_manager": get_adapter_manager()})
        except Exception:
            logger.warning("Could not initialize improvement agent with adapter manager", exc_info=True)

        task = AgentTask(
            task_id="review-cli",
            agent_type=AgentType.WORKER,
            description="Run system performance review",
            context={"review_type": "full"},
        )
        result = agent.execute(task)

        if result.success and result.output:
            recs = result.output.get("recommendations", [])
            applied = result.output.get("auto_applied", [])
            print(f"\n[AM Workbench] Found {len(recs)} recommendations, auto-applied {len(applied)}")
            for rec in recs[:5]:
                priority = rec.get("priority", "?").upper()
                print(f"  [{priority}] {rec.get('action', '?')}")
                print(f"         Rationale: {rec.get('rationale', '')[:80]}")
        return 0
    except Exception as exc:
        print(f"[AM Workbench] Review failed: {exc}")
        logger.warning("Self-improvement review command failed: %s — CLI returns exit code 1", exc)
        return 1


def cmd_benchmark(args: Any) -> int:
    """Run agent benchmarks and report any performance regressions.

    Args:
        args: Parsed CLI arguments with optional agents filter and verbose.

    Returns:
        Exit code (0 if no regressions, 1 if regressions detected or on error).
    """
    from vetinari.cli_startup import _setup_logging

    _setup_logging(args.verbose)

    # Single-case mode: run one benchmark case by "suite:case_id" composite ID
    single_case = getattr(args, "case", None)
    if single_case:
        print(f"[AM Workbench] Running single benchmark case: {single_case}")
        try:
            from vetinari.benchmarks.runner import get_default_runner

            runner = get_default_runner()
            result = runner.run_single(single_case)
            status = "PASS" if result.passed else "FAIL"
            print(f"  [{status}] {result.case_id}  score={result.score:.3f}  latency={result.latency_ms:.0f}ms")
            if result.error:
                print(f"  Error: {result.error}")
            return 0 if result.passed else 1
        except Exception as exc:
            print(f"[AM Workbench] Single-case benchmark failed: {exc}")
            logger.warning("Single-case benchmark '%s' failed: %s", single_case, exc)
            return 1

    print("[AM Workbench] Running agent benchmarks...")
    try:
        from vetinari.benchmarks.suite import BenchmarkSuite

        suite = BenchmarkSuite()
        agent_filter = getattr(args, "agents", None)
        results = suite.run_all(agent_types=agent_filter)
        suite.print_report(results)
        regressions = suite.check_regression(results)
        if regressions:
            print("\nREGRESSIONS DETECTED:")
            for r in regressions:
                print(f"  {r}")
            return 1
        return 0
    except Exception as exc:
        print(f"[AM Workbench] Benchmark failed: {exc}")
        logger.warning("Benchmark suite failed to run: %s — no results available, CLI returns exit code 1", exc)
        return 1


def cmd_mcp(args: Any) -> int:
    """Start the MCP server for editor integration (stdio or http transport).

    For stdio transport, runs the JSON-RPC message loop on stdin/stdout.
    For http transport, the native Rust kernel owns the HTTP surface; this
    Python command only starts the stdio integration.

    Args:
        args: Parsed CLI arguments with transport, mcp_port, mcp_host,
              verbose.

    Returns:
        Exit code (0 on success, 1 on failure).
    """
    from vetinari.cli_startup import _setup_logging

    _setup_logging(args.verbose)
    transport = getattr(args, "transport", "stdio")

    try:
        from vetinari.mcp.server import get_mcp_server

        server = get_mcp_server()

        if transport == "http":
            logger.warning(
                "HTTP MCP transport is owned by the native Rust kernel; "
                "this Python command only starts the stdio MCP server"
            )
            return 0
        else:
            from vetinari.mcp.transport import StdioTransport

            print("[AM Workbench] MCP stdio server ready", file=sys.stderr)
            stdio = StdioTransport(server)
            stdio.run()

        return 0
    except KeyboardInterrupt:
        logger.warning("MCP server interrupted by user — shutting down cleanly")
        return 0
    except Exception as exc:
        print(f"[AM Workbench] MCP server failed: {exc}", file=sys.stderr)
        logger.warning("MCP server encountered a fatal error and will exit: %s — editor integration unavailable", exc)
        return 1


def _print_project_metadata(project_dir: Path, project_id: str) -> None:
    project_meta = project_dir / "project.yaml"
    if not project_meta.exists():
        print(f"  No project.yaml found in {project_dir}")
        return
    import yaml

    with project_meta.open(encoding="utf-8") as f:
        meta = yaml.safe_load(f) or {}
    print(f"  Project: {meta.get('name', project_id)}")
    print(f"  Category: {meta.get('category', 'unknown')}")
    print(f"  Status: {meta.get('status', 'unknown')}")
    print(f"  Created: {meta.get('created_at', 'unknown')}")


def _print_plan_state(project_dir: Path) -> None:
    plan_file = project_dir / "plan.json"
    if not plan_file.exists():
        print("\n  No plan.json found")
        return
    import json

    plan_data = json.loads(plan_file.read_text(encoding="utf-8"))
    print(f"\n  Plan: {plan_data.get('plan_id', 'unknown')}")
    print(f"  Goal: {plan_data.get('goal', 'N/A')[:80]}")
    print(f"  Phase: {plan_data.get('phase', 0)}")
    print(f"  Tasks: {len(plan_data.get('tasks', []))}")
    for task in plan_data.get("tasks", []):
        print(
            f"    [{task.get('status', 'unknown'):>10}] ({task.get('assigned_agent', '?')}) {task.get('description', '')[:60]}"
        )


def _print_execution_log(project_dir: Path) -> None:
    exec_log = project_dir / "execution.log"
    if not exec_log.exists():
        print("\n  No execution.log found")
        return
    log_lines = exec_log.read_text(encoding="utf-8").splitlines()
    print(f"\n  Execution log: {len(log_lines)} entries")
    for line in log_lines[-10:]:
        print(f"    {line}")


def _print_database_state(db_path: Path) -> None:
    if not db_path.exists():
        print("\n  No database found")
        return
    import sqlite3

    with sqlite3.connect(str(db_path)) as conn:
        cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = [row[0] for row in cursor.fetchall()]
        print(f"\n  Database: {len(tables)} tables")
        if "PlanHistory" in tables:
            cursor = conn.execute("SELECT COUNT(*) FROM PlanHistory")
            print(f"  Plan history entries: {cursor.fetchone()[0]}")
        if "SubtaskMemory" in tables:
            cursor = conn.execute("SELECT COUNT(*) FROM SubtaskMemory")
            print(f"  Subtask memory entries: {cursor.fetchone()[0]}")
        if "ModelPerformance" in tables:
            cursor = conn.execute("SELECT COUNT(*) FROM ModelPerformance")
            print(f"  Model performance records: {cursor.fetchone()[0]}")


def _print_output_artefacts(output_dir: Path) -> None:
    if not output_dir.exists():
        print("\n  No output artefacts found")
        return
    artefacts = list(output_dir.iterdir())
    print(f"\n  Output artefacts: {len(artefacts)}")
    for art in artefacts[:10]:
        size = art.stat().st_size if art.is_file() else 0
        print(f"    {art.name} ({size:,} bytes)")


def _print_training_batch_queue() -> None:
    try:
        from vetinari.adapters.batch_processor import get_batch_processor

        queue_stats = get_batch_processor().get_queue_stats()
        print("\n  Training batch queue:")
        print(f"    enabled: {queue_stats['enabled']}")
        print(f"    total_queued: {queue_stats['total_queued']}")
        print(f"    flush_thread_active: {queue_stats['flush_thread_active']}")
    except (ImportError, AttributeError):
        logger.debug("Batch processor unavailable - skipping queue stats in diagnosis")


def cmd_diagnose(args: Any) -> int:
    """Trace execution history for a project and show what happened.

    Reads the project state, event log, and SSE events to produce a
    diagnostic timeline showing the sequence of agent actions, model
    selections, quality gate results, and any errors or anomalies.

    Args:
        args: Parsed CLI arguments — requires ``args.project_id``.

    Returns:
        0 on success, 1 if the project cannot be found or on error.
    """
    from vetinari.cli_startup import _setup_logging

    _setup_logging(args.verbose)
    if getattr(args, "backends", False):
        from scripts.backend_health_check import main as backend_health_main

        return backend_health_main(["--dry-run", "--show-cache"])
    project_id = args.project_id
    print(f"[AM Workbench] Diagnosing project: {project_id}")
    print("=" * 60)

    try:
        from vetinari.constants import PROJECTS_DIR, VETINARI_STATE_DIR

        project_dir = PROJECTS_DIR / project_id
        if not project_dir.exists():
            print(f"  Project directory not found: {project_dir}")
            return 1

        from vetinari.constants import OUTPUTS_DIR

        _print_project_metadata(project_dir, project_id)
        _print_plan_state(project_dir)
        _print_execution_log(project_dir)
        _print_database_state(VETINARI_STATE_DIR / "vetinari.db")
        _print_output_artefacts(OUTPUTS_DIR / project_id)
        _print_training_batch_queue()

        print(f"\n{'=' * 60}")
        print("  Diagnosis complete.")
        return 0

    except Exception as exc:
        print(f"[AM Workbench] Diagnosis failed: {exc}")
        logger.exception("Diagnosis failed for project %s", project_id)
        return 1


def cmd_drift_check(args: Any) -> int:
    """Run a full drift audit using DriftMonitor.

    Uses the DriftMonitor to check contract fingerprints, capability
    coverage, and schema validation.  Reports all detected drifts and
    exits 1 if any issues are found.

    Args:
        args: Parsed CLI arguments.  Recognises ``args.update`` (bool)
            to regenerate the drift baseline instead of checking.

    Returns:
        Exit code (0 if no drift, 1 if drift detected or on error).
    """
    from vetinari.cli_startup import _setup_logging

    _setup_logging(args.verbose)

    # Handle 'drift update' subcommand
    if getattr(args, "update", False):
        return _drift_update_baseline()

    print("[AM Workbench] Running full drift audit...")
    try:
        from vetinari.drift.monitor import get_drift_monitor

        monitor = get_drift_monitor()
        report = monitor.run_full_audit()

        if report.is_clean:
            print(f"[AM Workbench] No drift detected. ({report.duration_ms:.0f}ms)")
            return 0

        print(f"[AM Workbench] Drift detected ({report.duration_ms:.0f}ms):")
        if report.contract_drifts:
            print(f"  Contract drifts: {len(report.contract_drifts)}")
            for name, info in report.contract_drifts.items():
                print(f"    - {name}: was {info.get('previous', '?')[:12]}.. now {info.get('current', '?')[:12]}..")
        if report.capability_drifts:
            print(f"  Capability drifts: {len(report.capability_drifts)}")
            for item in report.capability_drifts:
                print(f"    - {item}")
        if report.schema_errors:
            print(f"  Schema errors: {len(report.schema_errors)}")
            for schema_name, errors in report.schema_errors.items():
                for err in errors:
                    print(f"    - {schema_name}: {err}")
        for issue in report.issues:
            print(f"  - {issue}")
        return 1
    except Exception as exc:
        print(f"[AM Workbench] Drift check failed: {exc}")
        logger.exception("Drift check failed")
        return 1


def _drift_update_baseline() -> int:
    """Regenerate the drift baseline snapshot by re-registering core contracts.

    Returns:
        0 on success, 1 on failure.
    """
    print("[AM Workbench] Updating drift baseline...")
    try:
        from vetinari.drift.contract_registry import get_contract_registry

        registry = get_contract_registry()

        # Register core contracts to establish baseline
        from vetinari.agents.contracts import AgentResult, AgentSpec, ExecutionPlan, Task, VerificationResult

        for name, cls in [
            ("AgentSpec", AgentSpec),
            ("Task", Task),
            ("ExecutionPlan", ExecutionPlan),
            ("AgentResult", AgentResult),
            ("VerificationResult", VerificationResult),
        ]:
            # Register a default instance as the contract fingerprint
            try:
                instance = cls.__new__(cls)
                registry.register(name, instance)
            except Exception:
                logger.warning("Could not register %s for baseline", name)

        registry.snapshot()
        print("[AM Workbench] Drift baseline updated successfully.")
        return 0
    except Exception as exc:
        print(f"[AM Workbench] Baseline update failed: {exc}")
        logger.exception("Baseline update failed")
        return 1


def _register_devops_commands(subparsers: Any) -> None:
    """Register DevOps commands with the CLI argument parser.

    Args:
        subparsers: The argparse subparsers action group from the main parser.
    """
    subparsers.add_parser("upgrade", help="Check for model upgrades")
    subparsers.add_parser("review", help="Run self-improvement agent review")

    p_bench = subparsers.add_parser("benchmark", help="Run agent benchmarks")
    p_bench.add_argument("--agents", nargs="*", help="Specific agent types to benchmark")
    p_bench.add_argument(
        "--case", metavar="SUITE:CASE_ID", help="Run a single benchmark case (e.g. toolbench:tb-l1-001)"
    )

    p_mcp = subparsers.add_parser("mcp", help="Start MCP server for editor integration")
    p_mcp.add_argument(
        "--transport", default="stdio", choices=["stdio", "http"], help="Transport mode (default: stdio)"
    )
    p_mcp.add_argument(
        "--mcp-port", type=int, default=8765, help="HTTP transport port (default: 8765; http transport only)"
    )
    p_mcp.add_argument("--mcp-host", default="127.0.0.1", help="HTTP transport bind address (http transport only)")

    p_drift = subparsers.add_parser("drift-check", help="Check for contract drift across agents")
    p_drift.add_argument("--update", action="store_true", help="Regenerate drift baseline instead of checking")

    p_diagnose = subparsers.add_parser("diagnose", help="Trace execution history for a project")
    p_diagnose.add_argument("project_id", nargs="?", default="", help="The project ID to diagnose")
    p_diagnose.add_argument("--backends", action="store_true", help="Run backend health probes")


__all__ = [
    "_register_devops_commands",
    "cmd_benchmark",
    "cmd_diagnose",
    "cmd_drift_check",
    "cmd_mcp",
    "cmd_review",
    "cmd_upgrade",
]
