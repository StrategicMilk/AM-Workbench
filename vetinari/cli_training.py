"""Training, Kaizen, and Watch CLI commands for Vetinari.

Handles the continuous-improvement (Kaizen), idle-time training, and
file-watcher subcommands. These are domain-specific commands that
coordinate the self-learning and monitoring subsystems.

This module is part of the CLI pipeline:
argument parsing (cli.py) -> **domain commands** (cli_training.py).
"""

from __future__ import annotations

import json
import logging
import sys
import time
from pathlib import Path
from typing import Any

from vetinari.constants import (
    KAIZEN_DB_PATH,
    MAIN_LOOP_POLL_INTERVAL,
)
from vetinari.ux import display_label_or_humanize

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Kaizen commands
# ---------------------------------------------------------------------------


def cmd_kaizen(args: Any) -> int:
    """Kaizen Office - continuous improvement commands.

    Args:
        args: Parsed CLI arguments with kaizen_action.

    Returns:
        Exit code (0 for success, 1 for error).
    """
    from vetinari.kaizen.improvement_log import ImprovementLog

    log = ImprovementLog(KAIZEN_DB_PATH)

    action = getattr(args, "kaizen_action", None)

    if action == "report":
        report = log.get_weekly_report()
        print("=== Weekly Kaizen Report ===")
        print(f"  Proposed:  {report.total_proposed}")
        print(f"  Active:    {report.total_active}")
        print(f"  Confirmed: {report.total_confirmed}")
        print(f"  Failed:    {report.total_failed}")
        print(f"  Reverted:  {report.total_reverted}")
        print(f"  Avg Effect: {report.avg_improvement_effect:+.3f}")
        print(f"  Generated: {report.generated_at.isoformat()}")
        print("Next actions:")
        print("  vetinari kaizen gemba")
        print("  docs/getting-started/kaizen-workflow.md")
        return 0

    if action == "gemba":
        from vetinari.kaizen.gemba import AutoGembaWalk

        gemba = AutoGembaWalk(log)
        weekly = log.get_weekly_report()
        defect_counts = log.get_weekly_defect_counts()
        top_defect = log.get_top_defect_pattern()
        total_events = (
            weekly.total_proposed
            + weekly.total_active
            + weekly.total_confirmed
            + weekly.total_failed
            + weekly.total_reverted
        )
        if total_events == 0 and not defect_counts and top_defect is None:
            print("Gemba walk requires execution history; no kaizen or defect records are available.")
            return 1
        failure_patterns = []
        if weekly.total_failed:
            failure_patterns.append({
                "model": "kaizen-log",
                "task_type": "failed_improvement",
                "frequency": weekly.total_failed,
                "avg_score": 0.0,
            })
        rework_stats = {
            "rework_count": weekly.total_failed + weekly.total_reverted,
            "total_tasks": max(total_events, 1),
        }
        value_stream_stats = {
            "value_add_ratio": weekly.total_confirmed / max(total_events, 1),
            "avg_lead_time": 0.0,
            "bottleneck_station": "kaizen",
        }
        defect_distribution = {}
        if top_defect:
            category = str(top_defect.get("category") or top_defect.get("defect_type") or "unknown")
            defect_distribution[category] = 1.0
        try:
            report = gemba.run(
                failure_patterns=failure_patterns,
                rework_stats=rework_stats,
                value_stream_stats=value_stream_stats,
                defect_distribution=defect_distribution,
            )
        except Exception:
            logger.exception(
                "Gemba walk failed - AutoGembaWalk.run() raised an unexpected error; check logs for details"
            )
            return 1
        print("=== Gemba Walk Complete ===")
        print(f"  Findings: {len(report.findings)}")
        print(f"  Improvements Proposed: {report.improvements_proposed}")
        for finding in report.findings:
            print(f"  [{display_label_or_humanize(finding.type)}] {finding.detail}")
            print(f"    -> {finding.proposed_improvement}")
        if not report.findings:
            print("  No issues found - the line is running clean.")
        return 0

    print("Usage: vetinari kaizen {report|gemba}")
    return 1


def _register_kaizen_commands(subparsers: Any) -> None:
    """Register kaizen commands with the CLI argument parser.

    Args:
        subparsers: The argparse subparsers action group from the main parser.
    """
    p_kaizen = subparsers.add_parser("kaizen", help="Kaizen Office - continuous improvement")
    p_kaizen.add_argument(
        "kaizen_action",
        choices=["report", "gemba"],
        help="report: weekly summary | gemba: on-demand execution review",
    )


# ---------------------------------------------------------------------------
# Training commands
# ---------------------------------------------------------------------------


def _cmd_train_status() -> int:
    from vetinari.training.control import get_training_control_service

    receipt = get_training_control_service().status()
    print(receipt.message)
    if receipt.job_id:
        print(f"Job: {receipt.job_id}")
    return 0


def _cmd_train_start(args: Any) -> int:
    from vetinari.training.control import get_training_control_service

    skill = getattr(args, "skill", None)
    receipt = get_training_control_service().start(skill=skill)
    print(receipt.message)
    if receipt.job_id:
        print(f"Job: {receipt.job_id}")
    if not receipt.passed:
        return 1
    return 0


def _cmd_train_pause(args: Any) -> int:
    from vetinari.training.control import get_training_control_service

    receipt = get_training_control_service().pause(job_id=getattr(args, "job_id", None))
    print(receipt.message)
    return 0 if receipt.passed else 1


def _cmd_train_resume(args: Any) -> int:
    from vetinari.training.control import get_training_control_service

    receipt = get_training_control_service().resume(job_id=getattr(args, "job_id", None))
    print(receipt.message)
    return 0 if receipt.passed else 1


def _cmd_train_stop(args: Any) -> int:
    from vetinari.training.control import get_training_control_service

    receipt = get_training_control_service().stop(job_id=getattr(args, "job_id", None))
    print(receipt.message)
    return 0 if receipt.passed else 1


def _cmd_train_cancel(args: Any) -> int:
    from vetinari.training.control import get_training_control_service

    receipt = get_training_control_service().cancel(job_id=getattr(args, "job_id", None))
    print(receipt.message)
    return 0 if receipt.passed else 1


def _cmd_train_checkpoint(args: Any) -> int:
    from vetinari.training.control import get_training_control_service

    receipt = get_training_control_service().checkpoint(job_id=getattr(args, "job_id", None))
    print(receipt.message)
    return 0 if receipt.passed else 1


def _cmd_train_jobs() -> int:
    from vetinari.training.control import get_training_control_service

    jobs = get_training_control_service().jobs()
    if not jobs:
        print("No training jobs recorded.")
        return 0
    for job in jobs:
        print(
            f"{job.job_id} {display_label_or_humanize(job.status)} "
            f"progress={job.progress:.2f} activity={job.activity_description}"
        )
    return 0


def _cmd_train_data() -> int:
    from vetinari.training.data_seeder import TrainingDataSeeder

    status = TrainingDataSeeder().get_seed_status()
    print(f"Seed datasets: {status.get('downloaded', 0)}/{status.get('total', 0)} downloaded")
    try:
        from vetinari.learning.training_data import get_training_collector

        stats = get_training_collector().get_stats()
        print(f"Execution records: {stats.get('total_records', 0)}")
        print(f"Average quality: {stats.get('avg_score', 0):.2f}")
    except Exception as exc:
        print(f"Training data collector unavailable: {exc}")
    return 0


def _cmd_train_run(args: Any) -> int:
    from vetinari.training.pipeline import TrainingPipeline

    pipeline = TrainingPipeline()
    reqs = pipeline.check_requirements()
    if not reqs.get("ready_for_training"):
        print("Training libraries not installed. Run: pip install trl peft bitsandbytes transformers")
        return 1

    skill = getattr(args, "skill", None)
    backend = getattr(args, "backend", "vllm")
    model_format = getattr(args, "model_format", None)
    base_model = getattr(args, "base_model", "auto")
    format_label = model_format or ("gguf" if backend == "llama_cpp" else "safetensors")
    print(
        "Running training pipeline "
        f"(task_type={display_label_or_humanize(skill or 'all')}, "
        f"backend={display_label_or_humanize(backend)}, "
        f"format={display_label_or_humanize(format_label)}, base_model={base_model})..."
    )
    run = pipeline.run(
        base_model=base_model,
        task_type=skill,
        backend=backend,
        model_format=model_format,
        model_revision=getattr(args, "model_revision", None),
    )
    print(f"Run {run.run_id}: success={run.success}")
    if run.output_model_path:
        print(f"  Model: {run.output_model_path}")
    if getattr(run, "model_manifest_path", ""):
        print(f"  Manifest: {run.model_manifest_path}")
    if run.error:
        print(f"  Error: {run.error}")
    return 0 if run.success else 1


def _cmd_train_seed() -> int:
    from vetinari.training.data_seeder import TrainingDataSeeder

    count = TrainingDataSeeder().seed_if_empty(authorized=True)
    print(f"Seeded {count} datasets.")
    return 0


def _cmd_train_curriculum() -> int:
    from vetinari.training.curriculum import TrainingCurriculum

    curriculum = TrainingCurriculum()
    status = curriculum.get_status()
    print(f"Phase: {display_label_or_humanize(status['phase'])}")
    print(f"Candidates: {status.get('candidate_count', 0)}")
    activity = curriculum.next_activity()
    print("\nNext activity:")
    print(f"  Type: {display_label_or_humanize(activity.type)}")
    print(f"  Description: {activity.description}")
    print(f"  Priority: {activity.priority:.2f}")
    print(f"  Est. duration: {activity.estimated_duration_minutes}m")
    print(f"  Est. VRAM: {activity.estimated_vram_gb:.1f} GB")
    return 0


def _cmd_train_history() -> int:
    from vetinari.training.agent_trainer import AgentTrainer

    stats = AgentTrainer().get_stats()
    if not stats.get("agents"):
        print("No training history yet.")
        return 0
    for agent, info in stats["agents"].items():
        print(f"  {agent}: last trained {info.get('last_trained', 'never')}, runs: {info.get('run_count', 0)}")
    return 0


def cmd_train(args: Any) -> int:
    """Manage the idle-time training system.

    Dispatches to the appropriate training subsystem based on
    ``args.train_action``.

    Args:
        args: Parsed CLI arguments with train_action and optional skill.

    Returns:
        Exit code (0 for success, 1 for error).
    """
    action = args.train_action
    if action == "status":
        return _cmd_train_status()
    if action == "start":
        return _cmd_train_start(args)
    if action == "pause":
        return _cmd_train_pause(args)
    if action == "resume":
        return _cmd_train_resume(args)
    if action == "stop":
        return _cmd_train_stop(args)
    if action == "cancel":
        return _cmd_train_cancel(args)
    if action == "checkpoint":
        return _cmd_train_checkpoint(args)
    if action == "jobs":
        return _cmd_train_jobs()
    if action == "data":
        return _cmd_train_data()
    if action == "run":
        return _cmd_train_run(args)
    if action == "seed":
        return _cmd_train_seed()
    if action == "curriculum":
        return _cmd_train_curriculum()
    if action == "history":
        return _cmd_train_history()
    print(f"Unknown train action: {display_label_or_humanize(action)}")
    return 1


def _register_training_commands(subparsers: Any) -> None:
    """Register training commands with the CLI argument parser.

    Args:
        subparsers: The argparse subparsers action group from the main parser.
    """
    p_train = subparsers.add_parser("train", help="Manage idle-time training system")
    p_train.add_argument(
        "train_action",
        choices=[
            "status",
            "start",
            "run",
            "pause",
            "resume",
            "stop",
            "cancel",
            "checkpoint",
            "jobs",
            "data",
            "seed",
            "curriculum",
            "history",
        ],
        help=(
            "status: show state | start: manual trigger | run: run pipeline now"
            " | pause/resume/stop/cancel/checkpoint/jobs: server-side job controls"
            " | data: show stats | seed: download datasets | curriculum: show plan"
            " | history: past runs"
        ),
    )
    p_train.add_argument("--skill", default=None, help="Train specific skill (with 'start' action)")
    p_train.add_argument("--job-id", default=None, help="Target job id for pause/resume/stop/cancel/checkpoint")
    p_train.add_argument("--base-model", default="auto", help="Base model ID/path for 'run' (default: auto)")
    p_train.add_argument(
        "--backend",
        choices=["llama_cpp", "vllm", "nim"],
        default="vllm",
        help="Deployment backend for 'run' (default: vllm; use llama_cpp for GGUF)",
    )
    p_train.add_argument(
        "--format",
        dest="model_format",
        choices=["gguf", "safetensors", "awq", "gptq"],
        default=None,
        help="Output model format for 'run'",
    )
    p_train.add_argument(
        "--revision",
        dest="model_revision",
        default=None,
        help="Immutable Hugging Face revision for 'run'",
    )


# ---------------------------------------------------------------------------
# Watch commands
# ---------------------------------------------------------------------------


def cmd_watch(args: Any) -> int:
    """Manage the WatchService file-monitoring daemon.

    Dispatches to start, report, or scan based on ``args.watch_action``.

    Args:
        args: Parsed CLI arguments with ``watch_action`` and optional flags.

    Returns:
        Exit code (0 for success, non-zero for error).
    """
    action = getattr(args, "watch_action", "start")

    if action == "start":
        return _cmd_watch_start(args)
    if action == "report":
        return _cmd_watch_report(args)
    if action == "scan":
        return _cmd_watch_scan(args)

    print(f"Unknown watch action: {action}")
    return 1


def _cmd_watch_start(args: Any) -> int:
    """Start the file watcher and block until Ctrl-C.

    Args:
        args: Parsed CLI arguments providing ``dir``, ``interval``, and
              ``no_directives`` fields.

    Returns:
        Exit code (0 on clean shutdown, 1 on initialisation error).
    """
    from vetinari.watch import WatchConfig, WatchMode

    watch_dir = getattr(args, "dir", ".") or "."
    interval = getattr(args, "interval", 2.0) or 2.0
    scan_directives = not getattr(args, "no_directives", False)

    config = WatchConfig(
        watch_dir=watch_dir,
        poll_interval=float(interval),
        scan_directives=scan_directives,
    )

    try:
        watcher = WatchMode(config)
        watcher.start()
    except Exception as exc:
        logger.exception("Failed to start watch mode")
        print(f"[Vetinari Watch] Error: {exc}", file=sys.stderr)
        return 1

    print(f"[Vetinari Watch] Monitoring {Path(watch_dir).resolve()}")
    print(f"[Vetinari Watch] Poll interval: {interval}s | Directives: {scan_directives}")
    print("[Vetinari Watch] Press Ctrl-C to stop")

    try:
        while watcher.is_running:
            time.sleep(MAIN_LOOP_POLL_INTERVAL)
    except KeyboardInterrupt:
        logger.warning("Watch mode interrupted by user")
    finally:
        watcher.stop()
        print("\n[Vetinari Watch] Stopped.")

    return 0


def _cmd_watch_report(args: Any) -> int:
    """Print all entries from the on-disk directive report file.

    Args:
        args: Parsed CLI arguments providing the ``dir`` field.

    Returns:
        Exit code (0 for success, 1 if the report file cannot be read).
    """
    watch_dir = getattr(args, "dir", ".") or "."
    report_path = Path(watch_dir) / ".vetinari" / "watch_report.jsonl"

    if not report_path.exists():
        print(f"[Vetinari Watch] No report file found at {report_path}")
        print("[Vetinari Watch] Run 'vetinari watch start' to generate one.")
        return 0

    try:
        entries: list[dict[str, Any]] = []
        with Path(report_path).open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    entries.append(json.loads(line))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[Vetinari Watch] Could not read report: {exc}", file=sys.stderr)
        logger.warning("Could not read watch report at %s - file may be corrupted: %s", report_path, exc)
        return 1

    if not entries:
        print("[Vetinari Watch] Report file is empty - no directives recorded.")
        return 0

    print(f"[Vetinari Watch] {len(entries)} directive(s) in report\n")

    high = [e for e in entries if e.get("priority") == "high"]
    normal = [e for e in entries if e.get("priority") != "high"]

    def _print_entry(entry: dict[str, Any]) -> None:
        """Print a single report entry in a compact, readable format."""
        action_str = entry.get("action", "?").upper()
        path = entry.get("file_path", "?")
        line_num = entry.get("line_number", "?")
        target = entry.get("target") or entry.get("full_line", "")
        ts = entry.get("timestamp", "")[:19]
        print(f"  [{display_label_or_humanize(action_str)}] {path}:{line_num}  - {target}  ({ts})")

    if high:
        print("  HIGH PRIORITY:")
        for entry in high:
            _print_entry(entry)
        print()

    if normal:
        print("  NORMAL:")
        for entry in normal:
            _print_entry(entry)

    return 0


def _cmd_watch_scan(args: Any) -> int:
    """One-shot scan of all tracked files for @vetinari directives.

    Args:
        args: Parsed CLI arguments providing the ``dir`` field.

    Returns:
        Exit code (0 for success, 1 on error).
    """
    from vetinari.watch import DirectiveScanner, FileWatcher, WatchConfig

    watch_dir = getattr(args, "dir", ".") or "."
    config = WatchConfig(watch_dir=watch_dir)

    try:
        watcher = FileWatcher(config)
        watcher.scan()
        all_files = list(watcher._file_states.keys())

        scanner = DirectiveScanner(config.max_file_size)
        all_directives = []
        for file_path in all_files:
            all_directives.extend(scanner.scan_file(file_path))
    except Exception as exc:
        logger.exception("Scan failed")
        print(f"[Vetinari Watch] Scan error: {exc}", file=sys.stderr)
        return 1

    if not all_directives:
        print(f"[Vetinari Watch] No @vetinari directives found in {watch_dir}")
        return 0

    print(f"[Vetinari Watch] Found {len(all_directives)} directive(s):\n")
    for d in all_directives:
        print(f"  [{display_label_or_humanize(d.action)}] {d.file_path}:{d.line_number}  - {d.target or d.full_line}")

    return 0


def _register_watch_commands(subparsers: Any) -> None:
    """Register the ``watch`` command group with the CLI argument parser.

    Args:
        subparsers: The argparse subparsers action group from the main parser.
    """
    p_watch = subparsers.add_parser(
        "watch",
        help="File watcher - monitor source files for @vetinari directives",
    )
    p_watch.add_argument(
        "watch_action",
        choices=["start", "report", "scan"],
        help="start: run watcher | report: print report | scan: one-shot scan",
    )
    p_watch.add_argument("--dir", default=".", help="Directory to monitor (default: current directory)")
    p_watch.add_argument("--interval", type=float, default=2.0, help="Poll interval in seconds (default: 2.0)")
    p_watch.add_argument(
        "--no-directives",
        action="store_true",
        help="Disable @vetinari directive scanning (track file changes only)",
    )


__all__ = [
    "_register_kaizen_commands",
    "_register_training_commands",
    "_register_watch_commands",
    "cmd_kaizen",
    "cmd_train",
    "cmd_watch",
]
