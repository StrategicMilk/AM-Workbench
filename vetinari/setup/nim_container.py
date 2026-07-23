"""DEPRECATED: NIM container tier removed per the 2026-06-09 AM Engine decision."""

from __future__ import annotations

import logging
import os
import shlex
import shutil
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any
from urllib.parse import urlparse

from vetinari.system.hardware_detect import GpuVendor, HardwareProfile

logger = logging.getLogger(__name__)


DEFAULT_NIM_ENDPOINT = os.environ.get("VETINARI_NIM_ENDPOINT", "http://127.0.0.1:8001")
DEFAULT_NIM_CONTAINER_NAME = "vetinari-nim"
DEFAULT_NIM_CONTAINER_PORT = 8000
DEFAULT_NIM_SETUP_MODE = "guided"
_SETUP_MODES = {"manual", "guided", "auto"}
_FALSE_VALUES = {"", "0", "false", "no", "off"}


@dataclass(frozen=True, slots=True)
class NIMContainerPlan:
    """Computed NIM container setup status for configuration and wizard output."""

    mode: str
    endpoint: str
    status: str
    hardware_eligible: bool
    endpoint_ready: bool
    docker_available: bool
    nvidia_runtime_available: bool
    image: str
    container_name: str
    host_port: int
    container_port: int
    api_key_env: str
    api_key_present: bool
    can_auto_start: bool
    missing: tuple[str, ...] = ()
    start_command: str = ""
    started: bool = False
    error: str = ""

    def __repr__(self) -> str:
        """Return a concise setup-plan identity for diagnostics."""
        return (
            f"NIMContainerPlan(mode={self.mode!r}, status={self.status!r}, "
            f"endpoint={self.endpoint!r}, can_auto_start={self.can_auto_start!r})"
        )

    def to_config(self) -> dict[str, Any]:
        """Return a secret-free config shape for ``~/.vetinari/config.yaml``."""
        return {
            "mode": self.mode,
            "status": self.status,
            "endpoint": self.endpoint,
            "hardware_eligible": self.hardware_eligible,
            "endpoint_ready": self.endpoint_ready,
            "docker_available": self.docker_available,
            "nvidia_runtime_available": self.nvidia_runtime_available,
            "image": self.image,
            "container_name": self.container_name,
            "host_port": self.host_port,
            "container_port": self.container_port,
            "api_key_env": self.api_key_env,
            "api_key_present": self.api_key_present,
            "can_auto_start": self.can_auto_start,
            "missing": list(self.missing),
            "start_command": self.start_command,
            "started": self.started,
            "error": self.error,
        }


def _coerce_env_bool(value: str | None, *, default: bool = False) -> bool:
    """Coerce a setup environment flag into a boolean."""
    if value is None:
        return default
    return value.strip().lower() not in _FALSE_VALUES


def _normalize_mode(value: str | None) -> str:
    """Normalize user setup-mode preference."""
    mode = (value or DEFAULT_NIM_SETUP_MODE).strip().lower()
    return mode if mode in _SETUP_MODES else DEFAULT_NIM_SETUP_MODE


def _endpoint_host_port(endpoint: str) -> int:
    """Extract the externally exposed endpoint port."""
    parsed = urlparse(endpoint)
    if parsed.port:
        return parsed.port
    if parsed.scheme == "https":
        return 443
    return 80 if parsed.scheme == "http" else 8001


def _api_key_env(_env: Mapping[str, str]) -> str:
    """Return the API-key environment variable expected by NIM containers."""
    return "NGC_API_KEY"


def _is_nvidia_container_hardware(hardware: HardwareProfile) -> bool:
    """Return True when local NIM container setup is viable on this machine."""
    return hardware.gpu_vendor == GpuVendor.NVIDIA and hardware.cuda_available


def is_openai_endpoint_ready(endpoint: str, *, timeout: float = 5.0) -> bool:
    """Check whether an OpenAI-compatible endpoint has a reachable model list.

    Returns:
        True when ``GET /v1/models`` returns HTTP 200.
    """
    try:
        import httpx

        response = httpx.get(f"{endpoint.rstrip('/')}/v1/models", timeout=timeout)
        return response.status_code == 200
    except Exception:
        logger.warning("NIM endpoint readiness probe failed for %s", endpoint, exc_info=True)
        return False


def _docker_has_nvidia_runtime(*, timeout: float = 5.0) -> bool:
    """Check Docker's configured runtimes for NVIDIA GPU support."""
    docker = shutil.which("docker")
    if not docker:
        return False
    try:
        result = subprocess.run(
            [docker, "info", "--format", "{{json .Runtimes}}"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        logger.warning("Docker NVIDIA runtime probe failed", exc_info=True)
        return False
    return result.returncode == 0 and "nvidia" in result.stdout.lower()


def _container_env_pairs(env: Mapping[str, str]) -> list[str]:
    """Return NIM cache-related container env assignments."""
    pairs = [
        f"NIM_ENABLE_KV_CACHE_REUSE={env.get('NIM_ENABLE_KV_CACHE_REUSE', '1')}",
    ]
    host_offload = env.get("NIM_ENABLE_KV_CACHE_HOST_OFFLOAD")
    if host_offload is not None:
        pairs.append(f"NIM_ENABLE_KV_CACHE_HOST_OFFLOAD={host_offload}")
    host_mem_fraction = env.get("NIM_KV_CACHE_HOST_MEM_FRACTION")
    if host_mem_fraction:
        pairs.append(f"NIM_KV_CACHE_HOST_MEM_FRACTION={host_mem_fraction}")
    kv_cache_percent = env.get("NIM_KVCACHE_PERCENT")
    if kv_cache_percent:
        pairs.append(f"NIM_KVCACHE_PERCENT={kv_cache_percent}")
    return pairs


def _build_start_command(
    *,
    image: str,
    container_name: str,
    host_port: int,
    container_port: int,
    api_key_env: str,
    env: Mapping[str, str],
) -> str:
    """Build a secret-free docker command for display/config."""
    args = [
        "docker",
        "run",
        "-d",
        "--name",
        container_name,
        "--gpus",
        "all",
        "-e",
        api_key_env,
    ]
    for pair in _container_env_pairs(env):
        args.extend(["-e", pair])
    cache_dir = env.get("VETINARI_NIM_CACHE_DIR")
    if cache_dir:
        args.extend(["-v", f"{cache_dir}:/opt/nim/.cache"])
    args.extend(["-p", f"{host_port}:{container_port}", image])
    return " ".join(shlex.quote(part) for part in args)


def _docker_run_args(plan: NIMContainerPlan, env: Mapping[str, str]) -> list[str]:
    """Build docker run args matching the display command."""
    args = [
        "docker",
        "run",
        "-d",
        "--name",
        plan.container_name,
        "--gpus",
        "all",
        "-e",
        plan.api_key_env,
    ]
    for pair in _container_env_pairs(env):
        args.extend(["-e", pair])
    cache_dir = env.get("VETINARI_NIM_CACHE_DIR")
    if cache_dir:
        args.extend(["-v", f"{cache_dir}:/opt/nim/.cache"])
    args.extend(["-p", f"{plan.host_port}:{plan.container_port}", plan.image])
    return args


def plan_nim_container_setup(
    hardware: HardwareProfile,
    *,
    endpoint: str | None = None,
    mode: str | None = None,
    env: Mapping[str, str] | None = None,
    endpoint_ready: bool | None = None,
    docker_available: bool | None = None,
    nvidia_runtime_available: bool | None = None,
) -> NIMContainerPlan:
    """Plan local NIM container setup without mutating the machine.

    Returns:
        Immutable container setup plan for wizard output and optional startup.
    """
    env_map = os.environ if env is None else env
    setup_mode = _normalize_mode(mode or env_map.get("VETINARI_NIM_SETUP_MODE"))
    nim_endpoint = endpoint or env_map.get("VETINARI_NIM_ENDPOINT") or DEFAULT_NIM_ENDPOINT
    ready = is_openai_endpoint_ready(nim_endpoint) if endpoint_ready is None else endpoint_ready
    has_docker = shutil.which("docker") is not None if docker_available is None else docker_available
    has_nvidia_runtime = (
        _docker_has_nvidia_runtime()
        if nvidia_runtime_available is None and has_docker
        else bool(nvidia_runtime_available)
    )
    hardware_eligible = _is_nvidia_container_hardware(hardware)
    image = env_map.get("VETINARI_NIM_IMAGE") or env_map.get("VETINARI_NIM_CONTAINER_IMAGE") or ""
    container_name = env_map.get("VETINARI_NIM_CONTAINER_NAME", DEFAULT_NIM_CONTAINER_NAME)
    host_port = int(env_map.get("VETINARI_NIM_HOST_PORT", _endpoint_host_port(nim_endpoint)))
    container_port = int(env_map.get("VETINARI_NIM_CONTAINER_PORT", DEFAULT_NIM_CONTAINER_PORT))
    key_env = _api_key_env(env_map)
    key_present = bool(env_map.get(key_env))

    missing: list[str] = []
    if not ready:
        if not hardware_eligible:
            missing.append("nvidia_cuda_gpu")
        if not has_docker:
            missing.append("docker")
        if has_docker and not has_nvidia_runtime:
            missing.append("nvidia_container_runtime")
        if not image:
            missing.append("VETINARI_NIM_IMAGE")
        if not key_present:
            missing.append(key_env)

    can_auto_start = setup_mode == "auto" and not ready and not missing
    status = _plan_status(ready, hardware_eligible, missing, setup_mode)

    start_command = ""
    if hardware_eligible and has_docker and image:
        start_command = _build_start_command(
            image=image,
            container_name=container_name,
            host_port=host_port,
            container_port=container_port,
            api_key_env=key_env,
            env=env_map,
        )

    return NIMContainerPlan(
        mode=setup_mode,
        endpoint=nim_endpoint,
        status=status,
        hardware_eligible=hardware_eligible,
        endpoint_ready=ready,
        docker_available=has_docker,
        nvidia_runtime_available=has_nvidia_runtime,
        image=image,
        container_name=container_name,
        host_port=host_port,
        container_port=container_port,
        api_key_env=key_env,
        api_key_present=key_present,
        can_auto_start=can_auto_start,
        missing=tuple(missing),
        start_command=start_command,
    )


def _plan_status(ready: bool, hardware_eligible: bool, missing: list[str], setup_mode: str) -> str:
    """Return NIM setup status from readiness and prerequisites."""
    if ready:
        return "ready"
    if not hardware_eligible:
        return "unsupported_hardware"
    if missing:
        return "missing_prerequisites"
    if setup_mode == "manual":
        return "manual_required"
    if setup_mode == "auto":
        return "auto_ready"
    return "guided_ready"


def start_nim_container(
    plan: NIMContainerPlan,
    *,
    env: Mapping[str, str] | None = None,
    timeout: float = 120.0,
) -> NIMContainerPlan:
    """Start a planned NIM container when the user explicitly selected auto mode.

    Returns:
        Updated plan reflecting started or failed container state.
    """
    if not plan.can_auto_start:
        return replace(plan, error="NIM container plan is not auto-startable")

    run_env = os.environ.copy()
    if env is not None:
        run_env.update(dict(env))

    docker = shutil.which("docker")
    if docker is None:
        return replace(plan, status="start_failed", error="docker executable not found")

    try:
        if _coerce_env_bool(run_env.get("VETINARI_NIM_AUTO_LOGIN"), default=True) and run_env.get("NGC_API_KEY"):
            subprocess.run(
                [docker, "login", "nvcr.io", "--username", "$oauthtoken", "--password-stdin"],
                input=run_env["NGC_API_KEY"],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=True,
                env=run_env,
            )
        docker_args = _docker_run_args(plan, run_env)
        docker_args[0] = docker
        subprocess.run(
            docker_args,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=True,
            env=run_env,
        )
    except subprocess.CalledProcessError as exc:
        message = (exc.stderr or exc.stdout or str(exc)).strip()
        logger.warning("NIM container startup command failed: %s", message)
        return replace(plan, status="start_failed", error=message)
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("NIM container startup failed", exc_info=True)
        return replace(plan, status="start_failed", error=str(exc))

    return replace(plan, status="started", started=True)
