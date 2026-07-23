"""Interactive preflight package installation helpers."""

from __future__ import annotations

import importlib.util
import logging
import os
import re
import sys
from collections.abc import Callable
from pathlib import Path

from vetinari.preflight_models import PreflightReport
from vetinari.security.fail_closed import UntrustedInputError

logger = logging.getLogger(__name__)

_EXTRA_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


def _echo(message: str = "") -> None:
    sys.stdout.write(f"{message}\n")


def build_pip_environment() -> dict[str, str]:
    """Return a pip environment that avoids fragile default temp/cache paths.

    Returns:
    Environment mapping for pip subprocesses.

    Raises:
        OSError: Propagated when validation, persistence, or execution fails.
    """
    work_root: Path | None = None
    for candidate in (Path.cwd() / ".pip-work", Path.home() / ".vetinari" / "pip-work"):
        try:
            (candidate / "temp").mkdir(parents=True, exist_ok=True)
            (candidate / "cache").mkdir(parents=True, exist_ok=True)
            work_root = candidate
            break
        except OSError:
            logger.warning("pip work root not writable: %s", candidate)
    if work_root is None:
        raise OSError("No writable pip temp/cache root available")
    temp_dir = work_root / "temp"
    cache_dir = work_root / "cache"
    env = os.environ.copy()
    env["TEMP"] = str(temp_dir)
    env["TMP"] = str(temp_dir)
    env["TMPDIR"] = str(temp_dir)
    env["PIP_CACHE_DIR"] = str(cache_dir)
    env.setdefault("PIP_DISABLE_PIP_VERSION_CHECK", "1")
    return env


def prompt_and_install(
    report: PreflightReport,
    *,
    pip_runner: Callable[[list[str], dict[str, str]], None],
) -> bool:
    """Ask the user for permission, then install missing recommended groups.

    Args:
        report: The preflight report with missing groups.
        pip_runner: Callable that executes pip arguments with the prepared environment.

    Returns:
        True if installation was attempted, False if user declined or nothing to install.
    """
    missing_recommended = [g for g in report.groups if g.recommended and not g.is_complete]
    hw = report.hardware
    if not missing_recommended and not (hw.has_nvidia_gpu and not hw.torch_has_cuda):
        return False
    extras_to_install: list[str] = [_validate_extra_name(g.extra) for g in missing_recommended]
    install_actions: list[str] = []
    if extras_to_install:
        extras_str = ",".join(extras_to_install)
        install_actions.append(f'pip install "vetinari[{extras_str}]"')
    torch_cuda_needed = hw.has_nvidia_gpu and not hw.torch_has_cuda and importlib.util.find_spec("torch") is not None
    if torch_cuda_needed:
        install_actions.append(
            "Reinstall torch with CUDA support "
            "(pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu128)"
        )
    if not install_actions:
        return False
    _echo("\n  The following installations are available:")
    for idx, action in enumerate(install_actions, 1):
        _echo(f"    {idx}. {action}")
    try:
        answer = input("\n  Install missing Python packages? [y/N]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        logger.warning("Package installation prompt interrupted - skipping installation")
        _echo("\n  Skipping installation.")
        return False
    if answer != "y":
        _echo("  Skipping installation.")
        return False
    pip_env = build_pip_environment()
    _echo(f"  Using pip temp/cache root: {Path(pip_env['TEMP']).parent}")
    if extras_to_install:
        _install_extras(extras_to_install, pip_env, pip_runner)
    if torch_cuda_needed:
        _install_torch_cuda(pip_env, pip_runner)
    return True


def _install_extras(
    extras_to_install: list[str],
    pip_env: dict[str, str],
    pip_runner: Callable[[list[str], dict[str, str]], None],
) -> None:
    extras_str = ",".join(extras_to_install)
    pip_args = ["install", f"vetinari[{extras_str}]"]
    cmd = [sys.executable, "-m", "pip", *pip_args]
    _echo(f"\n  Running: {' '.join(cmd)}")
    try:
        pip_runner(pip_args, pip_env)
        _echo("  Installation complete.")
    except Exception as exc:
        return_code = getattr(exc, "returncode", 1)
        logger.warning("pip install failed with exit code %s", return_code)
        _echo(f"  Installation failed (exit code {return_code}).")
        _echo("  You can retry manually with:")
        _echo(f'    pip install "vetinari[{extras_str}]"')
        _echo(f"  Reuse temp/cache root: {Path(pip_env['TEMP']).parent}")


def _validate_extra_name(value: object) -> str:
    """Return a package extra name that is safe to embed in pip arguments."""
    if not isinstance(value, str):
        raise UntrustedInputError("package extra must be a string")
    extra = value.strip()
    if not _EXTRA_NAME_RE.fullmatch(extra):
        raise UntrustedInputError(f"unsafe package extra name: {value!r}")
    return extra


def _install_torch_cuda(
    pip_env: dict[str, str],
    pip_runner: Callable[[list[str], dict[str, str]], None],
) -> None:
    _echo("\n  Reinstalling PyTorch with CUDA support...")
    torch_args = [
        "install",
        "torch",
        "torchvision",
        "torchaudio",
        "--index-url",
        "https://download.pytorch.org/whl/cu128",
    ]
    torch_cmd = [sys.executable, "-m", "pip", *torch_args]
    _echo(f"  Running: {' '.join(torch_cmd)}")
    try:
        pip_runner(torch_args, pip_env)
        _echo("  PyTorch CUDA installation complete.")
    except Exception as exc:
        return_code = getattr(exc, "returncode", 1)
        logger.warning("torch CUDA install failed with exit code %s", return_code)
        _echo(f"  PyTorch CUDA installation failed (exit code {return_code}).")
        _echo("  Retry manually:")
        _echo("    pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu128")
        _echo(f"  Reuse temp/cache root: {Path(pip_env['TEMP']).parent}")


_build_pip_environment = build_pip_environment

__all__ = ["_build_pip_environment", "build_pip_environment", "prompt_and_install"]
