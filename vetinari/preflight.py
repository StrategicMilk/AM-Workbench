"""Startup preflight checker — dependency groups, CUDA readiness, and interactive installer.

Runs at startup (``vetinari start``) to detect hardware, report missing optional
dependency groups, check CUDA build status of installed packages, and offer to
install missing groups with explicit user permission.

This is the first thing users see after the banner: **Banner → Preflight →
Subsystem Wiring → Health Check → Ready**.
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
import multiprocessing
import queue
import shutil
import subprocess
import sys
from collections.abc import Callable
from importlib import metadata as importlib_metadata
from pathlib import Path
from typing import Any, cast

from vetinari import preflight_install
from vetinari.models.vram_manager import VRAMManager, VRAMPreflightResult, check_model_vram_capacity, get_vram_manager
from vetinari.preflight_install import prompt_and_install as _prompt_and_install
from vetinari.preflight_models import (
    DependencyGroupStatus,
    DependencyReadiness,
    DependencyReadinessSpec,
    HardwareInfo,
    PreflightReport,
)
from vetinari.preflight_specs import (
    _BASE_RECOMMENDED_GROUPS,
    _DEPENDENCY_GROUPS,
    _DEPENDENCY_READINESS_SPECS,
    _GPU_RECOMMENDED_GROUPS,
)

logger = logging.getLogger(__name__)
_NATIVE_PROBE_TIMEOUT_SECONDS = 20.0


def _build_pip_environment() -> dict[str, str]:
    """Build the environment passed to pip subprocesses.

    Returns:
        Environment mapping for pip commands.
    """
    return preflight_install._build_pip_environment()


def check_model_vram_preflight(
    model_id: str,
    *,
    manager: VRAMManager | None = None,
) -> VRAMPreflightResult:
    """Check whether ``model_id`` can be admitted under current VRAM state.

    Returns:
        VRAM preflight decision for the requested model.
    """
    active_manager = manager or get_vram_manager()
    try:
        required = active_manager.get_model_vram_requirement(model_id)
    except Exception:
        logger.warning("Could not resolve VRAM requirement for %s", model_id, exc_info=True)
        required = None
    try:
        available = active_manager.get_max_available_vram_gb()
    except Exception:
        logger.warning("Could not resolve available VRAM for %s", model_id, exc_info=True)
        available = None
    return check_model_vram_capacity(
        model_id,
        required_vram_gb=required,
        available_vram_gb=available,
    )


def detect_hardware() -> HardwareInfo:
    """Probe the system for GPU, CUDA toolkit, torch CUDA, and Docker.

    Uses pynvml/nvidia-smi for GPU detection, shutil.which for system tools,
    and crash-isolated child processes for torch/llama-cpp CUDA status.

    Returns:
        Populated HardwareInfo with all detected capabilities.
    """
    has_nvidia_gpu = False
    gpu_name = ""
    vram_gb = 0.0

    # NVIDIA GPU via pynvml
    try:
        pynvml = importlib.import_module("pynvml")

        pynvml.nvmlInit()
        handle = pynvml.nvmlDeviceGetHandleByIndex(0)
        name = pynvml.nvmlDeviceGetName(handle)
        if isinstance(name, bytes):
            name = name.decode("utf-8", errors="replace")
        mem = pynvml.nvmlDeviceGetMemoryInfo(handle)
        has_nvidia_gpu = True
        gpu_name = name
        vram_gb = round(mem.total / (1024**3), 1)
        pynvml.nvmlShutdown()
    except ImportError:
        logger.warning("pynvml not available — trying nvidia-smi fallback for GPU detection")
    except Exception as exc:
        logger.warning("pynvml GPU probe failed: %s — GPU detection may be incomplete", exc)

    # Fallback: nvidia-smi
    if not has_nvidia_gpu and shutil.which("nvidia-smi"):
        has_nvidia_gpu = True
        gpu_name = "NVIDIA GPU (detected via nvidia-smi)"

    # CUDA toolkit (nvcc on PATH or in standard install locations)
    has_cuda_toolkit = shutil.which("nvcc") is not None
    if not has_cuda_toolkit:
        # Check standard NVIDIA CUDA Toolkit install location (Windows)
        cuda_base = Path("C:/Program Files/NVIDIA GPU Computing Toolkit/CUDA")
        if cuda_base.exists():
            has_cuda_toolkit = any((cuda_base / d / "bin" / "nvcc.exe").exists() for d in cuda_base.iterdir())

    # torch CUDA support
    torch_has_cuda = False
    if importlib.util.find_spec("torch") is not None:
        torch_cuda_probe = _run_native_bool_probe(_torch_cuda_worker)
        if torch_cuda_probe is None:
            logger.warning("torch CUDA probe failed — CUDA support detection may be incomplete")
        else:
            torch_has_cuda = torch_cuda_probe

    # llama-cpp-python CUDA support
    llama_cpp_has_cuda = False
    if importlib.util.find_spec("llama_cpp") is not None:
        llama_cuda_probe = _run_native_bool_probe(_llama_cpp_cuda_worker)
        if llama_cuda_probe is None:
            logger.warning("llama-cpp CUDA probe failed — GPU offload support detection unavailable")
        else:
            llama_cpp_has_cuda = llama_cuda_probe

    # Docker
    has_docker = shutil.which("docker") is not None

    return HardwareInfo(
        has_nvidia_gpu=has_nvidia_gpu,
        gpu_name=gpu_name,
        vram_gb=vram_gb,
        has_cuda_toolkit=has_cuda_toolkit,
        torch_has_cuda=torch_has_cuda,
        llama_cpp_has_cuda=llama_cpp_has_cuda,
        has_docker=has_docker,
    )


# ── Dependency group checking ────────────────────────────────────────────────


def _run_native_bool_probe(worker: Callable[[Any], None]) -> bool | None:
    """Run a native-library probe in a spawned process."""
    ctx = multiprocessing.get_context("spawn")
    result_queue = ctx.Queue(maxsize=1)
    proc = ctx.Process(target=worker, args=(result_queue,))
    try:
        proc.start()
        proc.join(_NATIVE_PROBE_TIMEOUT_SECONDS)
        if proc.is_alive():
            proc.terminate()
            proc.join(2.0)
            return None
    except Exception:
        logger.warning("native preflight probe failed to start", exc_info=True)
        result_queue.close()
        result_queue.join_thread()
        return None
    if proc.exitcode != 0:
        result_queue.close()
        result_queue.join_thread()
        return None
    try:
        return bool(result_queue.get_nowait())
    except queue.Empty:
        logger.warning("native preflight probe returned no result")
        return None
    finally:
        result_queue.close()
        result_queue.join_thread()


def _torch_cuda_worker(result_queue: Any) -> None:
    torch = importlib.import_module("torch")
    result_queue.put(bool(torch.cuda.is_available()))


def _llama_cpp_cuda_worker(result_queue: Any) -> None:
    llama_cpp = importlib.import_module("llama_cpp")
    supports = getattr(llama_cpp, "llama_supports_gpu_offload", None)
    if callable(supports):
        result_queue.put(bool(supports()))
        return
    result_queue.put(bool(getattr(llama_cpp, "LLAMA_SUPPORTS_GPU_OFFLOAD", False)))


def check_dependency_groups(hardware: HardwareInfo) -> list[DependencyGroupStatus]:
    """Probe each optional dependency group for installed/missing modules.

    Uses ``importlib.util.find_spec()`` for fast probing without actually
    importing heavy packages like torch or transformers.

    Args:
        hardware: Detected hardware, used to set the ``recommended`` flag.

    Returns:
        List of DependencyGroupStatus, one per group.
    """
    recommended = _GPU_RECOMMENDED_GROUPS if hardware.has_nvidia_gpu else _BASE_RECOMMENDED_GROUPS
    results: list[DependencyGroupStatus] = []

    for group_name, (extra, import_names, description) in _DEPENDENCY_GROUPS.items():
        installed: list[str] = []
        missing: list[str] = []
        for mod_name in import_names:
            try:
                if importlib.util.find_spec(mod_name) is not None:
                    installed.append(mod_name)
                else:
                    missing.append(mod_name)
            except ValueError:
                # Some packages (e.g. trl) have __spec__ = None, causing find_spec
                # to raise ValueError. Treat as installed since they are importable.
                installed.append(mod_name)

        results.append(
            DependencyGroupStatus(
                name=group_name,
                extra=extra,
                description=description,
                import_names=import_names,
                installed=tuple(installed),
                missing=tuple(missing),
                recommended=group_name in recommended,
            )
        )

    return results


def _resolve_dependency_expectation(spec: DependencyReadinessSpec, hardware: HardwareInfo) -> str:
    """Resolve dynamic expectation levels for the current hardware."""
    if hardware.has_nvidia_gpu and spec.gpu_expected:
        return cast(str, spec.gpu_expected)
    return cast(str, spec.expected)


def _resolve_available_import(import_names: tuple[str, ...]) -> str | None:
    """Return the first import name that resolves in the current environment."""
    for mod_name in import_names:
        try:
            if importlib.util.find_spec(mod_name) is not None:
                return mod_name
        except ValueError as exc:
            logger.info(
                "importlib spec unavailable for %s — treating as importable: %s",
                mod_name,
                exc,
            )
            return mod_name
    return None


def _resolve_distribution_version(distribution: str | None) -> str | None:
    """Return the installed distribution version, if available."""
    if not distribution:
        return None
    try:
        return importlib_metadata.version(distribution)
    except importlib_metadata.PackageNotFoundError as exc:
        logger.info(
            "Distribution %s is not installed in this environment: %s",
            distribution,
            exc,
        )
        return None


def _evaluate_runtime_verification(
    spec: DependencyReadinessSpec,
    hardware: HardwareInfo,
    installed: bool,
) -> tuple[bool | None, str]:
    """Return a runtime-verification signal and a human-readable detail."""
    if not installed:
        return None, f"missing — install with {spec.install_command}"

    if spec.package == "torch" and hardware.has_nvidia_gpu:
        if hardware.torch_has_cuda:
            return True, "importable with CUDA available"
        return False, "importable but CUDA is not available"

    if spec.package == "llama_cpp" and hardware.has_nvidia_gpu:
        if hardware.llama_cpp_has_cuda:
            return True, "importable with GPU offload available"
        return False, "importable but GPU offload is not available"

    return True, "importable"


def _classify_dependency_status(expected: str, installed: bool, runtime_verified: bool | None) -> str:
    """Collapse install and verification state into a machine-readable status."""
    if not installed:
        return {
            "required": "missing-required",
            "recommended": "missing-recommended",
            "optional": "missing-optional",
            "dev": "missing-dev",
        }[expected]

    if runtime_verified is False:
        return "installed-unverified"

    if expected == "optional":
        return "installed-optional"
    if expected == "dev":
        return "installed-dev"
    return "ready"


def build_dependency_readiness_matrix(hardware: HardwareInfo) -> list[DependencyReadiness]:
    """Return a per-package dependency readiness matrix for the current environment.

    Args:
        hardware: Detected hardware capabilities that affect expectation levels.

    Returns:
        Dependency readiness entries for core, optional, and dev packages.
    """
    matrix: list[DependencyReadiness] = []

    for spec in _DEPENDENCY_READINESS_SPECS:
        expected = _resolve_dependency_expectation(spec, hardware)
        import_name = _resolve_available_import(spec.import_names)
        installed = import_name is not None
        version = _resolve_distribution_version(spec.distribution) if installed else None
        runtime_verified, detail = _evaluate_runtime_verification(spec, hardware, installed)
        status = _classify_dependency_status(expected, installed, runtime_verified)
        if version and installed:
            detail = f"{detail} (version {version})"

        matrix.append(
            DependencyReadiness(
                package=spec.package,
                import_name=import_name,
                channel=spec.channel,
                expected=expected,
                description=spec.description,
                install_command=spec.install_command,
                distribution=spec.distribution,
                installed=installed,
                version=version,
                runtime_verified=runtime_verified,
                status=status,
                detail=detail,
            )
        )

    return matrix


def summarize_dependency_readiness(matrix: list[DependencyReadiness]) -> dict[str, object]:
    """Summarize the dependency readiness matrix into stable counts and package lists.

    Args:
        matrix: Dependency readiness entries produced by
            ``build_dependency_readiness_matrix``.

    Returns:
        Stable summary counts and package-name lists grouped by readiness state.
    """
    summary: dict[str, object] = {
        "total": len(matrix),
        "installed": sum(1 for item in matrix if item.installed),
        "ready": sum(1 for item in matrix if item.status == "ready"),
        "installed_optional": [item.package for item in matrix if item.status == "installed-optional"],
        "installed_dev": [item.package for item in matrix if item.status == "installed-dev"],
        "installed_unverified": [item.package for item in matrix if item.status == "installed-unverified"],
        "missing_required": [item.package for item in matrix if item.status == "missing-required"],
        "missing_recommended": [item.package for item in matrix if item.status == "missing-recommended"],
        "missing_optional": [item.package for item in matrix if item.status == "missing-optional"],
        "missing_dev": [item.package for item in matrix if item.status == "missing-dev"],
    }
    return summary


def check_cuda_readiness(hardware: HardwareInfo) -> list[str]:
    """Return system-level actions needed for full CUDA/GPU support.

    Only returns actions relevant to the detected hardware — e.g., CUDA toolkit
    actions are only listed when an NVIDIA GPU is present.

    Args:
        hardware: Detected hardware info.

    Returns:
        List of human-readable action strings.  Empty if everything is ready.
    """
    actions: list[str] = []
    if not hardware.has_nvidia_gpu:
        return actions

    if not hardware.has_cuda_toolkit:
        actions.append("Install CUDA Toolkit: download from https://developer.nvidia.com/cuda-downloads")

    if importlib.util.find_spec("torch") is not None and not hardware.torch_has_cuda:
        actions.append(
            "Reinstall PyTorch with CUDA: "
            "pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu128"
        )

    if importlib.util.find_spec("llama_cpp") is not None and not hardware.llama_cpp_has_cuda:
        actions.append(
            "Rebuild llama-cpp-python with CUDA: "
            'set CMAKE_ARGS="-DGGML_CUDA=on" && pip install llama-cpp-python --force-reinstall --no-cache-dir'
        )

    if not hardware.has_docker:
        actions.append("Install Docker for NVIDIA NIM containers: https://docs.docker.com/get-docker/")

    return actions


# ── Report printing ──────────────────────────────────────────────────────────

# Status symbols for terminal output
_SYM_OK = "[OK]"
_SYM_PARTIAL = "[!!]"
_SYM_MISSING = "[--]"
_SYM_ACTION = "[>>]"


def print_preflight_report(report: PreflightReport) -> None:
    """Print the preflight report to stdout with optional rich formatting.

    Shows hardware summary, CUDA readiness actions, and dependency group status
    in a scannable format.  Falls back to plain text if rich is not installed.

    Args:
        report: The complete preflight report to display.
    """
    hw = report.hardware

    # Hardware summary — intentional stdout display for user-facing preflight report
    print("\n  Hardware:")
    if hw.has_nvidia_gpu:
        print(f"    GPU       : {hw.gpu_name}")
        if hw.vram_gb > 0:
            print(f"    VRAM      : {hw.vram_gb:.0f} GB")
        cuda_parts = [
            f"toolkit={'yes' if hw.has_cuda_toolkit else 'NO'}",
            f"torch={'yes' if hw.torch_has_cuda else 'CPU-only'}",
            f"llama-cpp={'yes' if hw.llama_cpp_has_cuda else 'CPU-only'}",
        ]
        print(f"    CUDA      : {', '.join(cuda_parts)}")
        print(f"    Docker    : {'yes' if hw.has_docker else 'no (needed for NIM)'}")
    else:
        print("    GPU       : not detected (CPU inference will be used)")

    # System-level actions
    if report.system_actions:
        print("\n  System actions needed (manual):")
        for action in report.system_actions:
            print(f"    {_SYM_ACTION} {action}")

    # Dependency groups
    print("\n  Dependency groups:")
    for group in report.groups:
        if group.is_complete:
            sym = _SYM_OK
        elif group.is_partial:
            sym = _SYM_PARTIAL
        else:
            sym = _SYM_MISSING

        rec = " (recommended)" if group.recommended and not group.is_complete else ""
        print(f"    {sym:<8} {group.name}{rec}")
        if not group.is_complete:
            print(f"             {group.description}")
            if group.missing:
                print(f"             missing: {', '.join(group.missing)}")
            print(f'             install: pip install "vetinari[{group.extra}]"')

    # Summary line
    complete = sum(1 for g in report.groups if g.is_complete)
    total = len(report.groups)
    missing_recommended = [g for g in report.groups if g.recommended and not g.is_complete]
    print(f"\n  {complete}/{total} groups installed.", end="")
    if missing_recommended:
        names = ", ".join(g.extra for g in missing_recommended)
        print(f"  Missing recommended: {names}")
    else:
        print("  All recommended groups installed.")


def _run_pip_command(args: list[str], pip_env: dict[str, str]) -> None:
    """Run pip via a Python wrapper that forces tempfile.tempdir early."""
    _validate_pip_command_args(args)
    temp_dir = pip_env["TEMP"]
    launcher = (
        "import runpy, sys, tempfile; "
        f"tempfile.tempdir = {temp_dir!r}; "
        f"sys.argv = {['pip', *args]!r}; "
        "runpy.run_module('pip', run_name='__main__')"
    )
    subprocess.check_call(
        [sys.executable, "-c", launcher],
        stdout=sys.stdout,
        stderr=sys.stderr,
        env=pip_env,
    )


def _validate_pip_command_args(args: list[str]) -> None:
    """Fail closed unless args match a preflight-generated install command."""
    if not args or args[0] != "install":
        raise ValueError("preflight pip command must be an install command")
    if len(args) == 2 and args[1].startswith("vetinari[") and args[1].endswith("]"):
        extras = args[1][len("vetinari[") : -1]
        if extras and all(extra.replace("-", "_").isidentifier() for extra in extras.split(",")):
            return
    if args == [
        "install",
        "torch",
        "torchvision",
        "torchaudio",
        "--index-url",
        "https://download.pytorch.org/whl/cu128",
    ]:
        return
    raise ValueError("preflight pip command is not an allowed preflight install")


def prompt_and_install(report: PreflightReport) -> bool:
    """Ask the user for permission, then install missing recommended groups.

    Returns:
        True when installation was attempted.
    """
    original_builder = preflight_install.build_pip_environment
    preflight_install.build_pip_environment = _build_pip_environment
    try:
        return _prompt_and_install(report, pip_runner=_run_pip_command)
    finally:
        preflight_install.build_pip_environment = original_builder


def run_preflight(interactive: bool = True) -> PreflightReport:
    """Run the full preflight check and optionally prompt for installation.

    Called by ``cmd_start`` during startup.  When ``interactive`` is False
    (non-TTY or --skip-preflight), prints the report but does not prompt.

    Args:
        interactive: Whether to prompt the user for installation.

    Returns:
        The complete PreflightReport.
    """
    hardware = detect_hardware()
    groups = check_dependency_groups(hardware)
    dependency_matrix = build_dependency_readiness_matrix(hardware)
    system_actions = check_cuda_readiness(hardware)

    report = PreflightReport(
        hardware=hardware,
        groups=groups,
        dependency_matrix=dependency_matrix,
        system_actions=system_actions,
    )

    print_preflight_report(report)

    if interactive:
        prompt_and_install(report)

    return report
