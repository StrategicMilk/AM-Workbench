"""Vetinari Packaging CLI — thin facade over split command modules.

Delegates all implementation to:
- ``cli_packaging_data``    — init wizard, hardware detection, model tiers,
                              _print_header, _print_check
- ``cli_packaging_doctor``  — diagnostic suite (cmd_doctor)
- ``cli_packaging_models``  — model management + forget/config/resume/quick-action

All public symbols are re-exported so ``from vetinari.cli_packaging import cmd_init``
and similar patterns continue to work unchanged.
"""

from __future__ import annotations

import importlib.util
import logging
import os
from contextlib import suppress
from typing import Any

from vetinari.cli_packaging_data import (
    _CHECK_FAIL,
    _CHECK_INFO,
    _CHECK_PASS,
    _CHECK_WARN,
    _MODEL_TIERS,
    DEFAULT_USER_MODELS_DIR,
    _detect_hardware,
    _get_recommended_models,
    _print_check,
    _print_header,
    cmd_init,
)
from vetinari.cli_packaging_doctor import cmd_doctor
from vetinari.cli_packaging_models import (
    _download_with_progress,
    _find_models_dir,
    _guess_family,
    _guess_quantization,
    _models_download,
    _models_files,
    _models_info,
    _models_list,
    _models_recommend,
    _models_remove,
    _models_scan,
    _models_status,
    _verify_sha256,
    cmd_config_reload,
    cmd_forget,
    cmd_models,
    cmd_quick_action,
    cmd_resume,
)
from vetinari.i18n import cli_text
from vetinari.types import ModelProvider

logger = logging.getLogger(__name__)

_BACKEND_IMPORT_PROBES: dict[ModelProvider, tuple[str, ...]] = {
    ModelProvider.LOCAL: ("llama_cpp",),
    ModelProvider.OPENAI: ("openai",),
    ModelProvider.ANTHROPIC: ("anthropic",),
    ModelProvider.GEMINI: ("google.genai",),
    ModelProvider.VLLM: ("vllm",),
    ModelProvider.SGLANG: ("sglang",),
    ModelProvider.COMFYUI: ("comfy", "comfyui"),
    ModelProvider.FASTER_WHISPER: ("faster_whisper",),
}

_BACKEND_CONFIG_PROBES: dict[ModelProvider, tuple[str, ...]] = {
    ModelProvider.OPENAI: ("OPENAI_API_KEY",),
    ModelProvider.ANTHROPIC: ("ANTHROPIC_API_KEY", "CLAUDE_API_KEY"),
    ModelProvider.GEMINI: ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
    ModelProvider.VLLM: ("VETINARI_VLLM_ENDPOINT",),
    ModelProvider.NIM: ("VETINARI_NIM_ENDPOINT", "NGC_API_KEY"),
    ModelProvider.COMFYUI: ("VETINARI_COMFYUI_ENDPOINT", "COMFYUI_ENDPOINT"),
    ModelProvider.SGLANG: ("VETINARI_SGLANG_ENDPOINT",),
}


def _module_available(module_names: tuple[str, ...]) -> bool:
    """Return whether any optional backend module can be imported."""
    for module_name in module_names:
        with suppress(ImportError, ModuleNotFoundError, ValueError):
            if importlib.util.find_spec(module_name) is not None:
                return True
    return False


def _config_available(provider: ModelProvider) -> bool:
    """Return whether the provider has an environment-backed configuration signal."""
    return any(os.environ.get(name) for name in _BACKEND_CONFIG_PROBES.get(provider, ()))


def _backend_install_status(provider: ModelProvider) -> str:
    """Return an evidence-backed backend status for CLI display.

    Status is intentionally conservative: imports prove local package
    availability, while endpoint/API-key environment variables prove the
    backend is configured even when it is an external service.
    """
    if _module_available(_BACKEND_IMPORT_PROBES.get(provider, ())):
        return "INSTALLED"
    if _config_available(provider):
        return "CONFIGURED"
    return "NOT_INSTALLED"


def cmd_backends(args: Any) -> int:
    """Manage backend catalog entries and dry-run health checks.

    Returns:
        Process-style exit code.
    """
    from vetinari.adapters.registry import AdapterRegistry
    from vetinari.setup.backend_installer import (
        build_backend_install_plan,
        build_backend_install_plans,
        current_environment_install_plans,
        detect_install_hardware,
        isolated_environment_commands,
        normalize_provider,
        run_install_plan,
    )

    action = getattr(args, "backends_action", "list")
    if action == "list":
        print(cli_text("backends.header"))
        for provider in AdapterRegistry.providers():
            try:
                profile = AdapterRegistry.capabilities(provider)
                cache_durability = getattr(profile, "cache_durability", None)
                cache_durability_value = getattr(cache_durability, "value", cli_text("backends.unknown"))
                print(f"{provider.value} {_backend_install_status(provider)} {cache_durability_value}")
            except Exception:
                print(f"{provider.value} {_backend_install_status(provider)} {cli_text('backends.unknown')}")
        return 0
    if action == "health":
        from scripts.backend_health_check import main as health_main

        args_list = ["--dry-run", "--show-cache"] if getattr(args, "dry_run", False) else ["--show-cache"]
        return health_main(args_list)
    if action == "pin":
        print("config/backend_pins.yaml")
        return 0
    if action == "plan":
        hardware = detect_install_hardware()
        print(
            "hardware "
            f"os={hardware.os_name} arch={hardware.arch} ram_gb={hardware.ram_gb:.1f} "
            f"gpu={hardware.gpu_name or 'none'} vram_gb={hardware.vram_gb:.1f} cuda={hardware.cuda_available}"
        )
        for plan in build_backend_install_plans(
            hardware=hardware,
            include_core=getattr(args, "include_core", True),
            include_training=getattr(args, "with_training", False),
        ):
            fit = "supported" if plan.hardware_supported else "needs-setup"
            shared = "shared-env" if plan.shared_environment_safe else "isolated-env"
            print(
                f"{plan.provider.value} {fit} priority={plan.priority} env={plan.environment_key} "
                f"{shared} command={plan.command_text()}"
            )
            for reason in plan.skip_reasons:
                print(f"  reason: {reason}")
            for reason in plan.isolation_reasons:
                print(f"  isolation: {reason}")
            for command in plan.system_commands:
                print(f"  manual: {command}")
        return 0
    if action == "install":
        provider_name = getattr(args, "name", "")
        if not provider_name:
            print("Backend name is required.")
            return 1
        if provider_name in {"all", "recommended"}:
            candidate_plans = tuple(
                plan
                for plan in build_backend_install_plans(
                    include_core=getattr(args, "include_core", True),
                    include_training=getattr(args, "with_training", False),
                )
                if plan.hardware_supported
            )
            plans, isolated_plans = current_environment_install_plans(candidate_plans)
            for plan in isolated_plans:
                print(
                    f"{plan.provider.value} requires an isolated backend environment; "
                    "not installing it into the current interpreter during bulk setup."
                )
                for reason in plan.isolation_reasons:
                    print(f"isolation: {reason}")
                for command in isolated_environment_commands(plan):
                    print(f"manual: {command}")
        else:
            try:
                plans = (
                    build_backend_install_plan(
                        normalize_provider(provider_name),
                        include_core=getattr(args, "include_core", True),
                        include_training=getattr(args, "with_training", False),
                    ),
                )
            except ValueError:
                logger.warning("Unknown backend requested for install: %s", provider_name)
                print(f"Unknown backend: {provider_name}")
                return 1
            isolated_plans = ()
        failures = 0
        for plan in plans:
            print(plan.command_text())
            if not plan.hardware_supported:
                print(f"{plan.provider.value} requires additional setup before local install:")
                for reason in plan.skip_reasons:
                    print(f"reason: {reason}")
            for note in plan.notes:
                print(f"note: {note}")
            for reason in plan.isolation_reasons:
                print(f"isolation: {reason}")
            for command in plan.system_commands:
                print(f"manual: {command}")
            outcome = run_install_plan(plan, dry_run=getattr(args, "dry_run", False), output=print)
            for issue in outcome.issues:
                print(f"issue: {issue}")
            failures += 0 if outcome.passed else 1
        return 0 if failures == 0 else 1
    return 1


def _backend_installer_static_reference(provider: str):
    """Keep setup backend installer wired for static checks."""
    from vetinari.setup.backend_installer import ensure_backend

    return ensure_backend(provider)


def _register_init_command(subparsers: Any) -> None:
    p_init = subparsers.add_parser(
        "init",
        help="First-run setup wizard — detect hardware, select and download a model",
    )
    p_init.add_argument(
        "--skip-download",
        action="store_true",
        default=False,
        dest="skip_download",
        help="Skip the model download step (print download URL instead)",
    )
    p_init.add_argument("--dry-run", action="store_true", default=False, help="Print setup recommendations only")
    p_init.add_argument("--modality", default="", help="Comma-separated modality list for catalog recommendations")


def _register_doctor_command(subparsers: Any) -> None:
    p_doctor = subparsers.add_parser("doctor", help="Run diagnostic checks and report system health")
    p_doctor.add_argument(
        "--json",
        action="store_true",
        default=False,
        dest="json",
        help="Emit machine-readable JSON output instead of formatted text",
    )


def _register_models_command(subparsers: Any) -> None:
    p_models = subparsers.add_parser("models", help="Manage local and Hugging Face model files")
    p_models.add_argument(
        "models_action",
        choices=["list", "files", "download", "status", "remove", "info", "recommend", "scan", "check"],
        help=(
            "list: show all local models | "
            "files: list downloadable repo artifacts | "
            "download: fetch a model from HuggingFace | "
            "status: inspect a persisted download | "
            "remove: delete a model | "
            "info: show model metadata | "
            "recommend: suggest models for detected VRAM | "
            "scan: discover .gguf/.awq on disk | "
            "check: check for newer, better models"
        ),
    )
    p_models.add_argument(
        "--repo", default=None, help="HuggingFace repo ID from the setup recommendation catalog (used with download)"
    )
    p_models.add_argument("--filename", default=None, help="GGUF filename within the repo for llama.cpp downloads")
    p_models.add_argument(
        "--backend",
        default="auto",
        choices=["auto", "llama_cpp", "vllm", "nim"],
        help="Target backend; auto prefers native snapshots unless a GGUF filename/format is requested",
    )
    p_models.add_argument(
        "--format",
        dest="model_format",
        default=None,
        choices=["gguf", "safetensors", "awq", "gptq"],
        help="Model artifact format to list or download; native defaults to safetensors",
    )
    p_models.add_argument("--revision", default=None, help="Immutable revision or tag to resolve before download")
    p_models.add_argument("--download-id", default=None, help="Download id for models status")
    p_models.add_argument(
        "--objective", default=None, help="Filter by objective/category, e.g. coding, chat, reasoning"
    )
    p_models.add_argument("--family", default=None, help="Filter by model family, e.g. qwen, llama, mistral")
    p_models.add_argument("--quantization", default=None, help="Filter by quantization, e.g. Q4_K_M, AWQ, GPTQ")
    p_models.add_argument("--file-type", default=None, help="Filter by file type, e.g. gguf or safetensors")
    p_models.add_argument("--min-size-gb", type=float, default=None, help="Minimum artifact size in GB")
    p_models.add_argument("--max-size-gb", type=float, default=None, help="Maximum artifact size in GB")
    p_models.add_argument("--vram-gb", type=int, default=32, help="VRAM budget used when listing repo files")
    p_models.add_argument("--modality", default=None, help="Use the SHARD-01 catalog modality recommendations")
    p_models.add_argument("--hardware", default=None, help="Hardware target key, e.g. rtx_5090_32gb")
    p_models.add_argument("--name", default=None, help="Partial or full filename to match (used with remove and info)")


def _register_backends_command(subparsers: Any) -> None:
    p_backends = subparsers.add_parser("backends", help="Manage backend installs, pins, and health")
    p_backends.add_argument("backends_action", choices=["list", "install", "plan", "pin", "health"])
    p_backends.add_argument("name", nargs="?", default="")
    p_backends.add_argument("--dry-run", action="store_true", default=False)
    p_backends.add_argument(
        "--with-training",
        action="store_true",
        default=False,
        help="Install training extras together with the selected backend.",
    )
    p_backends.add_argument(
        "--no-core",
        action="store_false",
        default=True,
        dest="include_core",
        help="Install only backend-specific extras, not the default core extra.",
    )


def _register_misc_packaging_commands(subparsers: Any) -> None:
    p_forget = subparsers.add_parser("forget", help="Purge all learned data for a project")
    p_forget.add_argument("--project", required=True, help="Project name to forget")
    p_config = subparsers.add_parser("config", help="Configuration management (reload)")
    p_config.add_argument("config_action", choices=["reload"], help="reload: hot-reload settings")
    p_resume = subparsers.add_parser("resume", help="Resume interrupted plan execution")
    p_resume.add_argument("plan_id", help="Plan ID to resume from checkpoint")


__all__ = [
    "DEFAULT_USER_MODELS_DIR",
    "_CHECK_FAIL",
    "_CHECK_INFO",
    "_CHECK_PASS",
    "_CHECK_WARN",
    "_MODEL_TIERS",
    "_detect_hardware",
    "_download_with_progress",
    "_find_models_dir",
    "_get_recommended_models",
    "_guess_family",
    "_guess_quantization",
    "_models_download",
    "_models_files",
    "_models_info",
    "_models_list",
    "_models_recommend",
    "_models_remove",
    "_models_scan",
    "_models_status",
    "_print_check",
    "_print_header",
    "_register_packaging_commands",
    "_verify_sha256",
    "cmd_backends",
    "cmd_config_reload",
    "cmd_doctor",
    "cmd_forget",
    "cmd_init",
    "cmd_models",
    "cmd_quick_action",
    "cmd_resume",
]


def _register_packaging_commands(subparsers: Any) -> None:
    """Register init, doctor, models, forget, config, resume, and quick-action subcommands.

    Adds the following subparsers to the CLI argument parser:

    * ``init``    — first-run setup wizard (``--skip-download`` flag).
    * ``doctor``  — diagnostic report (``--json`` flag).
    * ``models``  — model management (positional action + ``--repo``,
      ``--filename``, ``--name`` options).
    * ``forget``  — purge learned data for a project (``--project`` required).
    * ``config``  — hot-reload settings (``reload`` action).
    * ``resume``  — resume a plan from checkpoint (positional ``plan_id``).
    * ``explain``, ``test``, ``fix`` — quick single-file actions.

    Args:
        subparsers: The ``argparse`` subparsers action group returned by
            ``parser.add_subparsers()``.
    """
    _register_init_command(subparsers)
    _register_doctor_command(subparsers)
    _register_models_command(subparsers)
    _register_backends_command(subparsers)
    _register_misc_packaging_commands(subparsers)

    # ── init ──────────────────────────────────────────────────────────────────
    # ── doctor ────────────────────────────────────────────────────────────────
    # ── models ────────────────────────────────────────────────────────────────
    # ── forget ────────────────────────────────────────────────────────────────
    # ── config ────────────────────────────────────────────────────────────────
    # ── resume ────────────────────────────────────────────────────────────────
    # ── Quick action commands (explain, test, fix) ─────────────────────────────
    for qaction in ("explain", "test", "fix"):
        p_qa = subparsers.add_parser(qaction, help=f"{qaction.capitalize()} a file")
        p_qa.add_argument("file", help="File path to operate on")
        p_qa.set_defaults(quick_action=qaction)
