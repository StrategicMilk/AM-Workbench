"""Packaging CLI — ``vetinari models`` command and all model management helpers.

Provides the model management subcommand group for Vetinari's CLI:
- List local GGUF files with rich table output
- Download models from HuggingFace Hub with optional progress bar
- Remove models with confirmation prompt
- Show per-file metadata (size, quantization, family, GGUF header validity)
- Recommend models based on detected VRAM
- Scan common directories for GGUF/AWQ model files
- ``cmd_forget``, ``cmd_config_reload``, ``cmd_resume``, ``cmd_quick_action``

Imported by ``cli_packaging.py`` which re-exports these for the CLI dispatch table.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import vetinari.cli_packaging_models_local as _local_models
from vetinari.cli_packaging_data import _RICH_AVAILABLE, _console, _detect_hardware
from vetinari.cli_packaging_models_local import (
    _find_models_dir,
    _find_native_models_dir,
    _guess_family,
    _guess_quantization,
    _iter_model_files,
    _local_file_matches_filters,
    _models_check,
    _models_info,
    _models_recommend,
    _models_remove,
)
from vetinari.cli_packaging_models_local import (
    _models_list as _local_models_list,
)
from vetinari.cli_packaging_models_remote import (
    _NATIVE_BACKENDS,
    _download_with_progress,
    _infer_cli_backend,
    _models_download,
    _models_files,
    _models_status,
    _verify_sha256,
)
from vetinari.security.redaction import REDACTED_PATH, redact_text

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _ModelCommandOptions:
    action: str
    backend: str
    model_format: str | None
    filename: str | None
    revision: str | None
    objective: str | None
    family: str | None
    quantization: str | None
    file_type: str | None
    min_size_gb: float | None
    max_size_gb: float | None
    vram_gb: int

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}("
            f"action={self.action!r}, "
            f"backend={self.backend!r}, "
            f"model_format={self.model_format!r}, "
            f"filename={self.filename!r}"
            ")"
        )


def _model_command_options(args: Any) -> _ModelCommandOptions:
    return _ModelCommandOptions(
        action=getattr(args, "models_action", "list"),
        backend=getattr(args, "backend", "auto"),
        model_format=getattr(args, "model_format", None),
        filename=getattr(args, "filename", None),
        revision=getattr(args, "revision", None),
        objective=getattr(args, "objective", None),
        family=getattr(args, "family", None),
        quantization=getattr(args, "quantization", None),
        file_type=getattr(args, "file_type", None),
        min_size_gb=getattr(args, "min_size_gb", None),
        max_size_gb=getattr(args, "max_size_gb", None),
        vram_gb=getattr(args, "vram_gb", 32),
    )


def _models_list_from_options(models_dir: Path, options: _ModelCommandOptions) -> int:
    return _models_list(
        models_dir,
        objective=options.objective,
        family=options.family,
        quantization=options.quantization,
        file_type=options.file_type,
        min_size_gb=options.min_size_gb,
        max_size_gb=options.max_size_gb,
    )


def _privacy_safe_cli_path(value: str | Path | None) -> str:
    """Return a CLI/display path that avoids exposing host-specific directories."""
    if value is None:
        return ""
    path = Path(str(value)).expanduser()
    try:
        resolved_path = path.resolve()
        cwd = Path.cwd().resolve()
    except OSError:
        logger.warning("Could not resolve CLI path for privacy-safe display", exc_info=True)
        name = path.name or "path"
        return f"{REDACTED_PATH}/{name}"
    if resolved_path.is_relative_to(cwd):
        return resolved_path.relative_to(cwd).as_posix()
    name = path.name or "path"
    logger.debug("Redacting non-workspace CLI path for display: %s", redact_text(str(value)))
    return f"{REDACTED_PATH}/{name}"


def _models_files_from_options(args: Any, backend_normalized: str, options: _ModelCommandOptions) -> int:
    return _models_files(
        getattr(args, "repo", None),
        backend=backend_normalized,
        model_format=options.model_format,
        revision=options.revision,
        vram_gb=options.vram_gb,
        objective=options.objective,
        family=options.family,
        quantization=options.quantization,
        file_type=options.file_type,
        min_size_gb=options.min_size_gb,
        max_size_gb=options.max_size_gb,
    )


def _models_recommend_modality(modality: str, hardware_key: str | None = None) -> int:
    """Print catalog-backed modality recommendations."""
    from vetinari.setup.model_recommender import ModelRecommender
    from vetinari.setup.model_recommender_types import Modality
    from vetinari.system.hardware_detect import GpuInfo, GpuVendor, HardwareProfile

    # The recommender applies a reserve factor to detected VRAM; use a synthetic
    # detected value that leaves 32 GB-class local recommendations visible.
    vram = 36.0 if hardware_key in {None, "rtx_5090_32gb", "rtx_5090_32gb_blackwell"} else 16.0
    hardware = HardwareProfile(
        cpu_count=32,
        ram_gb=64.0,
        gpu=GpuInfo(
            name=str(hardware_key or "rtx_5090_32gb"),
            vendor=GpuVendor.NVIDIA,
            vram_gb=vram,
            cuda_available=True,
        ),
        os_name="Windows",
        arch="x86_64",
    )
    modality_aliases = {"audio": "audio_asr", "image_gen": "image_generation", "video_gen": "video_generation"}
    recs = ModelRecommender().recommend_for_modality(Modality(modality_aliases.get(modality, modality)), hardware)
    print("model_id backend quant vram_gb verified_on")
    for rec in recs:
        print(
            f"{rec.model_id} {rec.recommended_backend or rec.backend} "
            f"{rec.recommended_quant or rec.quantization} {rec.vram_gb_loaded} {rec.verified_on.isoformat()}"
        )
    return 0


__all__ = [
    "_NATIVE_BACKENDS",
    "_RICH_AVAILABLE",
    "_console",
    "_download_with_progress",
    "_find_models_dir",
    "_find_native_models_dir",
    "_guess_family",
    "_guess_quantization",
    "_infer_cli_backend",
    "_iter_model_files",
    "_local_file_matches_filters",
    "_models_check",
    "_models_download",
    "_models_files",
    "_models_info",
    "_models_list",
    "_models_recommend",
    "_models_remove",
    "_models_scan",
    "_models_status",
    "_verify_sha256",
    "cmd_config_reload",
    "cmd_forget",
    "cmd_models",
    "cmd_quick_action",
    "cmd_resume",
]


def _models_list(
    models_dir: Path,
    *,
    objective: str | None = None,
    family: str | None = None,
    quantization: str | None = None,
    file_type: str | None = None,
    min_size_gb: float | None = None,
    max_size_gb: float | None = None,
) -> int:
    """Print local model files through the historical patchable facade."""
    _local_models._RICH_AVAILABLE = _RICH_AVAILABLE
    _local_models._console = _console
    return _local_models_list(
        models_dir,
        objective=objective,
        family=family,
        quantization=quantization,
        file_type=file_type,
        min_size_gb=min_size_gb,
        max_size_gb=max_size_gb,
    )


def _models_scan() -> int:
    """Scan for model files without printing host-specific parent directories."""
    from vetinari.setup.init_wizard import _scan_for_models

    print("[AM Workbench] Scanning for model files...")
    found = _scan_for_models()
    if not found:
        print("  No .gguf or .awq files found.")
        print("  Run 'vetinari init' to download a recommended model.")
        return 0
    print(f"  Found {len(found)} model file(s):")
    for model_path in found:
        size_mb = model_path.stat().st_size / (1024 * 1024) if model_path.exists() else 0
        print(f"    {model_path.name:40s} {size_mb:>8.1f} MB  location={REDACTED_PATH}")
    return 0


def cmd_models(args: Any) -> int:
    """Manage local and Hugging Face model artifacts.

    Supports model-management sub-actions selected via ``args.models_action``:

    * ``list``       — scan models directory and print a summary table.
    * ``download``   — download a model from HuggingFace Hub.
    * ``remove``     — delete a model file after confirmation.
    * ``info``       — print detailed metadata for a single model file.
    * ``recommend``  — suggest optimal models based on detected VRAM.
    * ``scan``       — discover .gguf/.awq files across common directories.
    * ``check``      — check for newer, better models via benchmarks and sentiment.

    Args:
        args: Parsed CLI arguments.  Recognises ``args.models_action``,
            ``args.repo``, ``args.filename``, and ``args.name``.

    Returns:
        0 on success, 1 on failure.
    """
    options = _model_command_options(args)
    backend_normalized = _infer_cli_backend(
        options.backend,
        filename=options.filename,
        model_format=options.model_format,
        action=options.action,
    )
    models_dir = _find_native_models_dir() if backend_normalized in _NATIVE_BACKENDS else _find_models_dir()

    if options.action == "list":
        return _models_list_from_options(models_dir, options)

    if options.action == "download":
        repo = getattr(args, "repo", None)
        return _models_download(
            repo,
            options.filename,
            models_dir,
            backend=backend_normalized,
            model_format=options.model_format,
            revision=options.revision,
        )

    if options.action == "files":
        return _models_files_from_options(args, backend_normalized, options)

    if options.action == "status":
        return _models_status(getattr(args, "download_id", None))

    if options.action == "remove":
        name = getattr(args, "name", None)
        return _models_remove(name, models_dir)

    if options.action == "info":
        name = getattr(args, "name", None)
        return _models_info(name, models_dir)

    if options.action == "recommend":
        modality = getattr(args, "modality", None)
        if modality:
            return _models_recommend_modality(modality, getattr(args, "hardware", None))
        hw = _detect_hardware()
        return _models_recommend(hw["vram_gb"])

    if options.action == "scan":
        return _models_scan()

    if options.action == "check":
        return _models_check()

    print(f"Unknown models action: {options.action}")
    print("Usage: vetinari models {{list|files|download|status|remove|info|recommend|scan|check}}")
    return 1


# ── Remaining packaging commands ───────────────────────────────────────────────


def cmd_forget(args: Any) -> int:
    """Purge all learned data for a specific project.

    Args:
        args: Parsed CLI arguments with ``project`` name.

    Returns:
        0 on success, 1 on error.
    """
    project = getattr(args, "project", None)
    if not project:
        print("Error: --project is required. Usage: vetinari forget --project <name>")
        return 1
    print(f"[AM Workbench] Forgetting all learned data for project: {project}")
    try:
        from vetinari.database import get_connection

        with get_connection() as conn:
            # Delete plan, episode-memory, and training rows matching the project
            # marker.  All deletes are committed atomically in this transaction.
            conn.execute(
                """
                DELETE FROM SubtaskMemory
                WHERE plan_id IN (
                    SELECT plan_id FROM PlanHistory WHERE goal LIKE ?
                )
                """,
                (f"%{project}%",),
            )
            conn.execute("DELETE FROM PlanHistory WHERE goal LIKE ?", (f"%{project}%",))
            conn.execute("DELETE FROM memory_episodes WHERE task_summary LIKE ?", (f"%{project}%",))
            conn.execute("DELETE FROM training_data WHERE task_type LIKE ?", (f"%{project}%",))
            conn.commit()
        print(f"  Cleared plan history and subtask memory for project: {project}")
    except Exception as exc:
        logger.warning("Could not clear project data from database for %s — data not purged", project, exc_info=True)
        print(f"  Database cleanup failed: {exc}")
        return 1
    print(f"  Done. Project '{project}' data has been forgotten.")
    return 0


def cmd_config_reload(_args: Any) -> int:
    """Hot-reload the VetinariSettings singleton without restarting.

    Returns:
        0 always.
    """
    from vetinari.config.settings import reset_settings

    reset_settings()
    print("[AM Workbench] Settings reloaded from environment and config files.")
    return 0


def cmd_resume(args: Any) -> int:
    """Resume a previously interrupted plan execution from checkpoint.

    Args:
        args: Parsed CLI arguments with ``plan_id``.

    Returns:
        0 on success, 1 on error.
    """
    plan_id = getattr(args, "plan_id", None)
    if not plan_id:
        print("Error: plan_id is required. Usage: vetinari resume <plan_id>")
        return 1
    print(f"[AM Workbench] Resuming plan: {plan_id}")
    try:
        from vetinari.orchestration.durable_execution import DurableExecutionEngine

        engine = DurableExecutionEngine()
        # Verify the checkpoint exists before attempting recovery.
        checkpoint = engine.load_checkpoint(plan_id)
        if checkpoint is None:
            print(f"  No checkpoint found for plan {plan_id}")
            return 1
        print("  Checkpoint found — attempting recovery via DurableExecutionEngine")
        result = engine.recover_execution(plan_id)
        status = result.get("status", "unknown")
        completed_count = result.get("completed_tasks", 0)
        failed_count = result.get("failed_tasks", 0)
        print(f"  Recovery complete — status: {status}")
        print(f"  Completed: {completed_count}  Failed: {failed_count}")
        return 0 if status not in ("failed", "error") else 1
    except Exception as exc:
        logger.warning(
            "Could not resume plan %s — checkpoint may be corrupt or execution context missing",
            plan_id,
            exc_info=True,
        )
        print(f"  Resume failed: {exc}")
        return 1


def cmd_quick_action(args: Any) -> int:
    """Execute a quick action (explain/test/review/fix) on a file.

    Args:
        args: Parsed CLI arguments with ``quick_action`` and ``file``.

    Returns:
        0 on success, 1 on error.
    """
    action = getattr(args, "quick_action", "explain")
    file_path = getattr(args, "file", None)
    if not file_path:
        print(f"Error: file path required. Usage: vetinari {action} <file>")
        return 1
    safe_file_path = _privacy_safe_cli_path(file_path)
    goal_map = {
        "explain": f"Explain what the file {safe_file_path} does, its role in the codebase, and key functions/classes.",
        "test": f"Generate comprehensive tests for the file {safe_file_path}.",
        "review": f"Review the file {safe_file_path} for bugs, security issues, and code quality problems.",
        "fix": f"Fix any issues found in the file {safe_file_path}.",
    }
    goal = redact_text(goal_map.get(action, f"{action} the file {safe_file_path}"))
    print(f"[AM Workbench] {action.capitalize()}: {safe_file_path}")
    try:
        from vetinari.orchestration.two_layer import get_two_layer_orchestrator

        orch = get_two_layer_orchestrator()
        results = orch.generate_and_execute(goal=goal)
        if results.get("final_output"):
            print("\n--- Output ---")
            print(str(results["final_output"])[:3000])
        return 0
    except Exception as exc:
        print(f"[AM Workbench] Error: {exc}")
        logger.warning("Quick action '%s' failed for %s: %s", action, file_path, exc)
        return 1
