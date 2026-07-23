"""Backend dependency provisioning for setup wizard and CLI."""

from __future__ import annotations

import importlib.util
import logging
import os
import shlex
import shutil
import subprocess
import sys
from collections.abc import Callable, Iterable, Sequence
from datetime import datetime, timezone
from pathlib import Path

from vetinari.agents.contracts import OutcomeSignal
from vetinari.agents.evidence_contracts import AttestedArtifact, ToolEvidence
from vetinari.boundary_guards import assert_dependency_success
from vetinari.security.redaction import redact_text
from vetinari.setup.backend_install_catalog import (
    ALL_INSTALLABLE_PROVIDERS as _ALL_INSTALLABLE_PROVIDERS,
)
from vetinari.setup.backend_install_catalog import (
    CURRENT_ENVIRONMENT_SAFE_GROUPS as _CURRENT_ENVIRONMENT_SAFE_GROUPS,
)
from vetinari.setup.backend_install_catalog import (
    DEFAULT_VLLM_MODEL as _DEFAULT_VLLM_MODEL,
)
from vetinari.setup.backend_install_catalog import (
    ISOLATED_ENVIRONMENT_GROUPS as _ISOLATED_ENVIRONMENT_GROUPS,
)
from vetinari.setup.backend_install_catalog import (
    PROVIDER_ALIASES as _PROVIDER_ALIASES,
)
from vetinari.setup.backend_install_catalog import (
    PROVIDER_ENV as _PROVIDER_ENV,
)
from vetinari.setup.backend_install_catalog import (
    PROVIDER_ENVIRONMENT_GROUP as _PROVIDER_ENVIRONMENT_GROUP,
)
from vetinari.setup.backend_install_catalog import (
    PROVIDER_EXTRAS as _PROVIDER_EXTRAS,
)
from vetinari.setup.backend_install_catalog import (
    PROVIDER_IMPORTS as _PROVIDER_IMPORTS,
)
from vetinari.setup.backend_install_catalog import (
    TRAINING_ISOLATED_PROVIDERS as _TRAINING_ISOLATED_PROVIDERS,
)
from vetinari.setup.backend_install_models import BackendInstallPlan
from vetinari.system.hardware_detect import GpuVendor, HardwareProfile, detect_hardware
from vetinari.types import ArtifactKind, EvidenceBasis, ModelProvider

OutputSink = Callable[[str], None]
logger = logging.getLogger(__name__)


def normalize_provider(provider: ModelProvider | str) -> ModelProvider:
    """Normalize setup-wizard/backend aliases to the canonical provider enum.

    Returns:
        Canonical model provider enum value.
    """
    if isinstance(provider, ModelProvider):
        return provider
    value = str(provider).strip().lower().replace("-", "_")
    if value in _PROVIDER_ALIASES:
        return _PROVIDER_ALIASES[value]
    return ModelProvider(value)


def project_root_from_here() -> Path:
    """Return the repository root that owns ``pyproject.toml``.

    Returns:
        Repository root path.

    Raises:
        FileNotFoundError: If no owning ``pyproject.toml`` is found.
    """
    root = Path(__file__).resolve().parent
    for _ in range(6):
        if (root / "pyproject.toml").exists():
            return root
        root = root.parent
    raise FileNotFoundError("Could not locate pyproject.toml from backend installer")


def detect_install_hardware() -> HardwareProfile:
    """Detect the hardware profile used for backend install planning."""
    return detect_hardware()


def _is_windows_host(hardware: HardwareProfile) -> bool:
    return hardware.os_name.lower().startswith("windows")


def _provider_hardware_reasons(provider: ModelProvider, hardware: HardwareProfile) -> tuple[str, ...]:
    reasons: list[str] = []
    if provider in {ModelProvider.VLLM, ModelProvider.SGLANG}:
        if hardware.gpu_vendor is not GpuVendor.NVIDIA or not hardware.cuda_available:
            reasons.append("requires NVIDIA CUDA hardware for local GPU serving")
        if _is_windows_host(hardware) and not shutil.which("wsl.exe"):
            reasons.append("Windows native install requires WSL or a configured OpenAI-compatible endpoint")
    if provider is ModelProvider.NIM:
        if hardware.gpu_vendor is not GpuVendor.NVIDIA:
            reasons.append("NVIDIA NIM requires NVIDIA hardware for local containers")
        if not shutil.which("docker"):
            reasons.append("local NIM container setup requires Docker")
    if provider is ModelProvider.COMFYUI and not hardware.has_gpu and hardware.ram_gb < 16:
        reasons.append("ComfyUI is practical only with a GPU or at least 16 GB RAM")
    if provider is ModelProvider.LOCAL and hardware.ram_gb and hardware.ram_gb < 8:
        reasons.append("llama.cpp local models need at least 8 GB RAM for the smallest recommended models")
    return tuple(reasons)


def _provider_priority(provider: ModelProvider, hardware: HardwareProfile) -> int:
    if provider is ModelProvider.LOCAL:
        return 10 if not hardware.has_gpu else 30
    if hardware.gpu_vendor is GpuVendor.NVIDIA and hardware.cuda_available:
        order = {
            ModelProvider.VLLM: 10,
            ModelProvider.SGLANG: 15,
            ModelProvider.NIM: 20,
            ModelProvider.COMFYUI: 25,
        }
        if provider in order:
            return order[provider]
    if provider is ModelProvider.FASTER_WHISPER:
        return 35
    if provider in {ModelProvider.OPENAI, ModelProvider.ANTHROPIC, ModelProvider.GEMINI}:
        return 60
    return 80


def recommended_providers_for_hardware(
    hardware: HardwareProfile | None = None,
    *,
    providers: Sequence[ModelProvider] = _ALL_INSTALLABLE_PROVIDERS,
) -> tuple[ModelProvider, ...]:
    """Return all installable providers ordered by hardware fit.

    Returns:
        Providers ordered from best to worst fit for the detected hardware.
    """
    profile = hardware or detect_install_hardware()
    return tuple(
        sorted(
            providers,
            key=lambda provider: (
                bool(_provider_hardware_reasons(provider, profile)),
                _provider_priority(provider, profile),
                provider.value,
            ),
        )
    )


def dependency_extras_for_provider(
    provider: ModelProvider | str,
    *,
    include_core: bool = True,
    include_training: bool = False,
    extra_extras: Iterable[str] = (),
) -> tuple[str, ...]:
    """Return pyproject extras needed for a backend provisioning target.

    Returns:
        Deduplicated pyproject extra names.
    """
    provider_type = normalize_provider(provider)
    extras: list[str] = []
    if include_core:
        extras.append("core")
    extras.extend(_PROVIDER_EXTRAS.get(provider_type, ()))
    if include_training:
        extras.append("training")
    extras.extend(str(extra).strip() for extra in extra_extras if str(extra).strip())
    return tuple(dict.fromkeys(extras))


def _current_interpreter_extras(
    provider: ModelProvider, extras: tuple[str, ...], hardware: HardwareProfile
) -> tuple[str, ...]:
    if (
        provider in {ModelProvider.VLLM, ModelProvider.SGLANG}
        and _is_windows_host(hardware)
        and not shutil.which("wsl.exe")
    ):
        return tuple(extra for extra in extras if extra not in {"vllm", "sglang"})
    return extras


def _pip_install_command(python_executable: str, extras: tuple[str, ...]) -> tuple[str, ...]:
    target = ".[{}]".format(",".join(extras)) if extras else "."
    return (python_executable, "-m", "pip", "install", "-e", target)


def _wsl_project_root(project_root: Path) -> str:
    """Translate a Windows checkout path to the equivalent WSL mount path."""
    resolved = project_root.resolve()
    drive = resolved.drive.rstrip(":").lower()
    if drive:
        relative = resolved.relative_to(resolved.anchor).as_posix()
        return f"/mnt/{drive}/{relative}"
    return resolved.as_posix()


def _wsl_isolated_install_command(environment_key: str, extras: tuple[str, ...], project_root: Path) -> tuple[str, ...]:
    target = ".[{}]".format(",".join(extras)) if extras else "."
    env_name = f"amw-{environment_key}"
    checkout = _wsl_project_root(project_root)
    script = (
        f"python3 -m venv ~/.vetinari/{env_name} && "
        f"~/.vetinari/{env_name}/bin/python -m pip install --upgrade pip setuptools wheel && "
        f'cd {shlex.quote(checkout)} && ~/.vetinari/{env_name}/bin/python -m pip install -e "{target}"'
    )
    return ("wsl.exe", "-d", "Ubuntu", "--", "bash", "-lc", script)


def _wsl_vllm_launch_command(environment_key: str) -> str:
    env_name = f"~/.vetinari/amw-{environment_key}"
    return (
        f".\\start-vllm-wsl.ps1 -VenvPath {env_name} -Model {_DEFAULT_VLLM_MODEL} -Port 8000 -StartupTimeoutSeconds 300"
    )


def _environment_key(provider: ModelProvider) -> str:
    return _PROVIDER_ENVIRONMENT_GROUP.get(provider, provider.value)


def _shared_environment_safe(provider: ModelProvider) -> bool:
    return _environment_key(provider) in _CURRENT_ENVIRONMENT_SAFE_GROUPS


def _isolation_reasons(provider: ModelProvider) -> tuple[str, ...]:
    if _environment_key(provider) not in _ISOLATED_ENVIRONMENT_GROUPS:
        return ()
    return (
        f"{provider.value} carries a backend-specific GPU server stack that can pin torch, flashinfer, "
        "xgrammar, and related CUDA packages differently from other local server backends",
    )


def training_must_be_isolated(provider: ModelProvider | str, *, include_training: bool) -> bool:
    """Return whether training deps must be split from this backend stack."""
    return include_training and normalize_provider(provider) in _TRAINING_ISOLATED_PROVIDERS


def _training_isolation_reasons(provider: ModelProvider, *, include_training: bool) -> tuple[str, ...]:
    if not training_must_be_isolated(provider, include_training=include_training):
        return ()
    return (
        "training extras are installed in amw-training because the current Unsloth training stack "
        "requires torch <2.11 while local GPU server backends can require torch 2.11",
    )


def isolated_environment_commands(plan: BackendInstallPlan) -> tuple[str, ...]:
    """Return commands for installing an isolated backend environment.

    Returns:
        Shell commands for creating and populating the backend environment.
    """
    extras_spec = ",".join(plan.extras)
    target = f".[{extras_spec}]" if extras_spec else "."
    env_name = f"amw-{plan.environment_key}"
    if os.name == "nt" and shutil.which("wsl.exe"):
        checkout = _wsl_project_root(plan.project_root)
        return (
            "wsl -d Ubuntu -- bash -lc "
            f"'python3 -m venv ~/.vetinari/{env_name} && "
            f"~/.vetinari/{env_name}/bin/python -m pip install --upgrade pip setuptools wheel && "
            f'cd {shlex.quote(checkout)} && ~/.vetinari/{env_name}/bin/python -m pip install -e "{target}"\'',
        )
    return (
        f"python -m venv ~/.vetinari/{env_name}",
        f"~/.vetinari/{env_name}/bin/python -m pip install --upgrade pip setuptools wheel",
        f'cd {plan.project_root} && ~/.vetinari/{env_name}/bin/python -m pip install -e "{target}"',
    )


def isolated_training_environment_commands(
    *,
    project_root: Path | None = None,
    include_core: bool = True,
) -> tuple[str, ...]:
    """Return commands for installing the dedicated training environment.

    Returns:
        Shell commands for creating and populating the training environment.
    """
    root = (project_root or project_root_from_here()).resolve()
    extras = ("core", "training") if include_core else ("training",)
    target = ".[{}]".format(",".join(extras))
    env_name = "amw-training"
    if os.name == "nt" and shutil.which("wsl.exe"):
        checkout = _wsl_project_root(root)
        return (
            "wsl -d Ubuntu -- bash -lc "
            f"'python3 -m venv ~/.vetinari/{env_name} && "
            f"~/.vetinari/{env_name}/bin/python -m pip install --upgrade pip setuptools wheel && "
            f'cd {shlex.quote(checkout)} && ~/.vetinari/{env_name}/bin/python -m pip install -e "{target}"\'',
        )
    return (
        f"python -m venv ~/.vetinari/{env_name}",
        f"~/.vetinari/{env_name}/bin/python -m pip install --upgrade pip setuptools wheel",
        f'cd {root} && ~/.vetinari/{env_name}/bin/python -m pip install -e "{target}"',
    )


def current_environment_install_plans(
    plans: Sequence[BackendInstallPlan],
) -> tuple[tuple[BackendInstallPlan, ...], tuple[BackendInstallPlan, ...]]:
    """Split plans into same-interpreter installs and isolated-env followups.

    Returns:
        Tuple of selected current-environment plans and deferred isolated plans.
    """
    selected: list[BackendInstallPlan] = []
    isolated: list[BackendInstallPlan] = []
    claimed_isolated_group = False
    for plan in plans:
        if not plan.hardware_supported:
            continue
        if plan.shared_environment_safe:
            selected.append(plan)
            continue
        if not claimed_isolated_group:
            selected.append(plan)
            claimed_isolated_group = True
        else:
            isolated.append(plan)
    return tuple(selected), tuple(isolated)


def build_backend_install_plan(
    provider: ModelProvider | str,
    *,
    include_core: bool = True,
    include_training: bool = False,
    extra_extras: Iterable[str] = (),
    python_executable: str | None = None,
    project_root: Path | None = None,
    hardware: HardwareProfile | None = None,
) -> BackendInstallPlan:
    """Build a runnable backend dependency plan from the selected provider.

    Returns:
        Backend installation plan for the requested provider.
    """
    provider_type = normalize_provider(provider)
    hardware_profile = hardware or detect_install_hardware()
    root = (project_root or project_root_from_here()).resolve()
    py = python_executable or sys.executable
    isolate_training = training_must_be_isolated(provider_type, include_training=include_training)
    extras = dependency_extras_for_provider(
        provider_type,
        include_core=include_core,
        include_training=include_training and not isolate_training,
        extra_extras=extra_extras,
    )
    extras = _current_interpreter_extras(provider_type, extras, hardware_profile)
    notes: list[str] = []
    system_commands: list[str] = []
    environment_key = _environment_key(provider_type)
    shared_environment_safe = _shared_environment_safe(provider_type)
    isolation_reasons = (
        *_isolation_reasons(provider_type),
        *_training_isolation_reasons(provider_type, include_training=include_training),
    )
    skip_reasons = _provider_hardware_reasons(provider_type, hardware_profile)
    pip_command = _pip_install_command(py, extras)
    if isolate_training:
        notes.append(
            "Local training dependencies are planned for a dedicated amw-training environment, "
            "separate from this backend server environment."
        )
        system_commands.extend(isolated_training_environment_commands(project_root=root, include_core=include_core))
    if provider_type in {ModelProvider.VLLM, ModelProvider.SGLANG} and os.name == "nt":
        notes.append(
            f"{provider_type.value} is GPU/Linux-oriented on this host; use WSL, Docker, or a configured endpoint."
        )
        if shutil.which("wsl.exe"):
            notes.append(f"The install command will create/update ~/.vetinari/amw-{environment_key} inside Ubuntu WSL.")
            pip_command = _wsl_isolated_install_command(environment_key, extras, root)
            if provider_type is ModelProvider.VLLM:
                system_commands.append(_wsl_vllm_launch_command(environment_key))
    if provider_type is ModelProvider.NIM:
        if os.name == "nt":
            notes.append(
                "nim is served through NVIDIA containers or a configured endpoint; install Docker before local setup."
            )
        system_commands.append("docker pull nvcr.io/nim/<org>/<model>:<tag>")
    return BackendInstallPlan(
        provider=provider_type,
        project_root=root,
        python_executable=py,
        extras=extras,
        pip_command=pip_command,
        verification_modules=_PROVIDER_IMPORTS.get(provider_type, ()),
        endpoint_env_vars=_PROVIDER_ENV.get(provider_type, ()),
        environment_key=environment_key,
        shared_environment_safe=shared_environment_safe,
        isolation_reasons=isolation_reasons,
        system_commands=tuple(system_commands),
        notes=tuple(notes),
        hardware_supported=not skip_reasons,
        priority=_provider_priority(provider_type, hardware_profile),
        skip_reasons=skip_reasons,
    )


def build_backend_install_plans(
    *,
    hardware: HardwareProfile | None = None,
    providers: Sequence[ModelProvider] = _ALL_INSTALLABLE_PROVIDERS,
    include_core: bool = True,
    include_training: bool = False,
    python_executable: str | None = None,
    project_root: Path | None = None,
) -> tuple[BackendInstallPlan, ...]:
    """Build hardware-ranked install plans for every known installable backend.

    Returns:
        Hardware-ranked backend installation plans.
    """
    profile = hardware or detect_install_hardware()
    ordered = recommended_providers_for_hardware(profile, providers=providers)
    return tuple(
        build_backend_install_plan(
            provider,
            include_core=include_core,
            include_training=include_training,
            python_executable=python_executable,
            project_root=project_root,
            hardware=profile,
        )
        for provider in ordered
    )


def run_install_plan(
    plan: BackendInstallPlan,
    *,
    dry_run: bool = False,
    output: OutputSink | None = None,
    timeout_s: float | None = None,
) -> OutcomeSignal:
    """Execute a backend install plan and return an evidence-backed outcome.

    Returns:
        Outcome signal containing command evidence and remediation suggestions.
    """
    output = output or (lambda _line: None)
    command_text = plan.command_text()
    evidence_command = redact_text(command_text)
    if dry_run:
        output(f"DRY-RUN {evidence_command}")
        return OutcomeSignal(
            passed=True,
            score=1.0,
            basis=EvidenceBasis.HUMAN_ATTESTED,
            attested_artifacts=(
                AttestedArtifact(
                    kind=ArtifactKind.COMMAND_INVOCATION,
                    attested_by="backend_installer.dry_run",
                    attested_at_utc=datetime.now(timezone.utc).isoformat(),
                    payload={"command": evidence_command, "executed": False},
                ),
            ),
            tool_evidence=(
                ToolEvidence(
                    tool_name="pip",
                    command=evidence_command,
                    exit_code=0,
                    stdout_snippet="dry-run",
                    passed=True,
                ),
            ),
            suggestions=plan.notes + plan.system_commands,
        )
    output(f"Running: {evidence_command}")
    output(f"Working directory: {plan.project_root}")
    lines: list[str] = []
    try:
        completed = subprocess.run(
            list(plan.pip_command),
            cwd=str(plan.project_root),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            timeout=timeout_s,
            check=False,
        )
        for line in (completed.stdout or "").splitlines():
            stripped = redact_text(line.rstrip())
            lines.append(stripped)
            output(stripped)
        exit_code = int(completed.returncode or 0)
        try:
            assert_dependency_success(exit_code == 0, dependency_id="backend install subprocess")
        except RuntimeError:
            logger.warning("Backend install command failed", exc_info=True)
    except subprocess.TimeoutExpired:
        logger.warning("Backend install command timed out", exc_info=True)
        return OutcomeSignal(
            passed=False,
            basis=EvidenceBasis.TOOL_EVIDENCE,
            issues=(f"pip subprocess timed out after {timeout_s} seconds",),
            suggestions=plan.notes + plan.system_commands,
        )
    except OSError as exc:
        logger.warning("Could not launch backend install command", exc_info=True)
        return OutcomeSignal(
            passed=False,
            basis=EvidenceBasis.TOOL_EVIDENCE,
            issues=(f"could not launch pip subprocess: {exc}",),
            suggestions=plan.notes + plan.system_commands,
        )
    tail = "\n".join(lines[-20:])
    return OutcomeSignal(
        passed=exit_code == 0,
        score=1.0 if exit_code == 0 else 0.0,
        basis=EvidenceBasis.TOOL_EVIDENCE,
        tool_evidence=(
            ToolEvidence(
                tool_name="pip",
                command=evidence_command,
                exit_code=exit_code,
                stdout_snippet=tail[-1000:],
                passed=exit_code == 0,
            ),
        ),
        issues=() if exit_code == 0 else (f"pip exited with code {exit_code}",),
        suggestions=plan.notes + plan.system_commands,
    )


def _module_available(module_names: tuple[str, ...]) -> bool:
    return all(importlib.util.find_spec(module_name) is not None for module_name in module_names)


def _provider_configured(provider: ModelProvider) -> bool:
    return any(os.environ.get(name) for name in _PROVIDER_ENV.get(provider, ()))


def ensure_backend(
    provider: ModelProvider | str,
    recommendation: object | None = None,
    *,
    install: bool = False,
    dry_run: bool = False,
    include_core: bool = True,
    include_training: bool = False,
) -> OutcomeSignal:
    """Check or provision a backend, failing closed when readiness is unproven.

    Args:
        provider: Provider enum or provider alias.
        recommendation: Optional recommendation object retained for API compatibility.
        install: Run the generated install plan instead of checking readiness only.
        dry_run: Print/prove the install plan without mutating the environment.
        include_core: Include core runtime dependencies in the install plan.
        include_training: Include training dependencies in the install plan.

    Returns:
        Outcome signal proving readiness or explaining missing setup.
    """
    _ = recommendation
    provider_type = normalize_provider(provider)
    plan = build_backend_install_plan(provider_type, include_core=include_core, include_training=include_training)
    if install or dry_run:
        return run_install_plan(plan, dry_run=dry_run)
    modules = _PROVIDER_IMPORTS.get(provider_type, ())
    if modules and _module_available(modules):
        return OutcomeSignal(passed=True, score=1.0, basis=EvidenceBasis.TOOL_EVIDENCE)
    if not modules and _provider_configured(provider_type):
        return OutcomeSignal(passed=True, score=1.0, basis=EvidenceBasis.TOOL_EVIDENCE)
    missing = tuple(module for module in modules if importlib.util.find_spec(module) is None)
    issues = missing or (f"{provider_type.value} endpoint or binary is not configured",)
    return OutcomeSignal(
        passed=False,
        basis=EvidenceBasis.TOOL_EVIDENCE,
        issues=tuple(str(issue) for issue in issues),
        suggestions=(plan.command_text(), *plan.notes, *plan.system_commands),
    )


def available_install_scripts() -> dict[ModelProvider, Path]:
    """Return legacy script mappings.

    Backend installs are now plan-based; returning an empty mapping prevents
    callers from advertising non-existent ``scripts/install_backends`` files.
    """
    return {}


def run_install_script(script_path: Path, dry_run: bool = False) -> tuple[int, str]:
    """Reject legacy script execution in favor of backend install plans.

    Args:
        script_path: Legacy script path requested by the caller.
        dry_run: Retained compatibility flag; legacy scripts are never run.

    Returns:
        Non-zero exit code and explanatory message.
    """
    _ = dry_run
    return 1, f"legacy backend install scripts are not supported: {script_path}"
