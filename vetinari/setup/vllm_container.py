"""vLLM container setup planning for the first-run wizard."""

from __future__ import annotations

import logging
import os
import shlex
import shutil
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, replace
from ipaddress import IPv4Address
from typing import Any
from urllib.parse import urlparse

from vetinari.system.hardware_detect import GpuVendor, HardwareProfile

logger = logging.getLogger(__name__)


DEFAULT_VLLM_ENDPOINT = os.environ.get("VETINARI_VLLM_ENDPOINT", "http://127.0.0.1:8000")
DEFAULT_VLLM_CONTAINER_NAME = "vetinari-vllm"
DEFAULT_VLLM_CONTAINER_PORT = 8000
DEFAULT_VLLM_CUDA_IMAGE = "vllm/vllm-openai@sha256:9f759c8e47d0b4a0c63c7d86da4d89fe1873782d4b915a1c2d824969dbf11a31"
DEFAULT_VLLM_ROCM_IMAGE = (
    "vllm/vllm-openai-rocm@sha256:58c5cd489c62bfc3e5987d24fd0bcb8b2d4e0d9e8f4d6738ea24c7855967de4f"
)
# Image digests for reproducible deployment. The defaults are pinned image refs
# so the container layer cannot drift between runs. Operators may override via
# ``VETINARI_VLLM_IMAGE`` during development, but production should keep a
# digest-pinned reference and document validated upgrades in CHANGELOG.md.
VLLM_IMAGE_DIGEST = os.environ.get(
    "VETINARI_VLLM_IMAGE_DIGEST",
    # Placeholder zero-digest reminds operators to pin via env var. The runtime
    # uses the env-var image string directly; this constant is the documented
    # default for tooling that wants to surface the pinned digest.
    "sha256:0000000000000000000000000000000000000000000000000000000000000000",
)


def validate_image_digest(image_ref: str) -> bool:
    """Return True when ``image_ref`` is digest-pinned (image@sha256:...).

    Used by container health checks to log a WARNING when the running container
    image is not digest-pinned. The function does not block startup — operators
    may still run with mutable tags in development — but the warning surfaces
    the supply-chain risk so deployment scripts can enforce pinning explicitly.
    """
    return "@sha256:" in image_ref


DEFAULT_VLLM_SETUP_MODE = "guided"
DEFAULT_VLLM_CONTAINER_BIND_HOST = str(IPv4Address(0))
DEFAULT_VLLM_DOCKER_PUBLISH_HOST = "127.0.0.1"
_SETUP_MODES = {"manual", "guided", "auto"}
_FALSE_VALUES = {"", "0", "false", "no", "off"}


@dataclass(frozen=True, slots=True)
class VLLMContainerPlan:
    """Computed vLLM container setup status for configuration and wizard output."""

    mode: str
    endpoint: str
    status: str
    hardware_eligible: bool
    endpoint_ready: bool
    docker_available: bool
    nvidia_runtime_available: bool
    device_backend: str
    image: str
    model: str
    container_name: str
    host_port: int
    container_port: int
    hf_token_present: bool
    can_auto_start: bool
    missing: tuple[str, ...] = ()
    start_command: str = ""
    started: bool = False
    error: str = ""

    def __repr__(self) -> str:
        """Return a concise setup-plan identity for diagnostics."""
        return (
            f"VLLMContainerPlan(mode={self.mode!r}, status={self.status!r}, "
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
            "device_backend": self.device_backend,
            "image": self.image,
            "model": self.model,
            "container_name": self.container_name,
            "host_port": self.host_port,
            "container_port": self.container_port,
            "hf_token_present": self.hf_token_present,
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
    mode = (value or DEFAULT_VLLM_SETUP_MODE).strip().lower()
    return mode if mode in _SETUP_MODES else DEFAULT_VLLM_SETUP_MODE


def _endpoint_host_port(endpoint: str) -> int:
    """Extract the externally exposed endpoint port."""
    parsed = urlparse(endpoint)
    if parsed.port:
        return parsed.port
    if parsed.scheme == "https":
        return 443
    return 80 if parsed.scheme == "http" else 8000


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
        logger.warning("vLLM endpoint readiness probe failed for %s", endpoint, exc_info=True)
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


def _device_backend(hardware: HardwareProfile) -> str:
    """Return the vLLM container device backend supported by this hardware."""
    if hardware.gpu_vendor == GpuVendor.NVIDIA and hardware.cuda_available:
        return "cuda"
    if hardware.gpu_vendor == GpuVendor.AMD:
        return "rocm"
    return "unsupported"


def _default_image(device_backend: str) -> str:
    """Return the default official vLLM image for the selected backend."""
    if device_backend == "rocm":
        return DEFAULT_VLLM_ROCM_IMAGE
    return DEFAULT_VLLM_CUDA_IMAGE


def _container_args_prefix(plan: VLLMContainerPlan) -> list[str]:
    """Build Docker hardware flags for the selected vLLM device backend."""
    if plan.device_backend == "cuda":
        return ["--runtime", "nvidia", "--gpus", "all"]
    if plan.device_backend == "rocm":
        return [
            "--group-add=video",
            "--cap-add=SYS_PTRACE",
            "--security-opt",
            "seccomp=unconfined",
            "--device",
            "/dev/kfd",
            "--device",
            "/dev/dri",
        ]
    return []


def _engine_args(env: Mapping[str, str], *, container_port: int) -> list[str]:
    """Return vLLM server args derived from setup environment variables."""
    bind_host = env.get("VETINARI_VLLM_CONTAINER_BIND_HOST", DEFAULT_VLLM_CONTAINER_BIND_HOST)
    args = ["--host", bind_host, "--port", str(container_port)]
    prefix_caching = _coerce_env_bool(env.get("VETINARI_VLLM_PREFIX_CACHING_ENABLED"), default=True)
    if prefix_caching:
        args.append("--enable-prefix-caching")
        hash_algo = env.get("VETINARI_VLLM_PREFIX_CACHING_HASH_ALGO", "sha256")
        if hash_algo:
            args.extend(["--prefix-caching-hash-algo", hash_algo])
    else:
        args.append("--no-enable-prefix-caching")
    served_name = env.get("VETINARI_VLLM_SERVED_MODEL_NAME")
    if served_name:
        args.extend(["--served-model-name", served_name])
    extra_args = env.get("VETINARI_VLLM_EXTRA_ARGS", "")
    if extra_args:
        args.extend(shlex.split(extra_args))
    return args


def _docker_run_args(plan: VLLMContainerPlan, env: Mapping[str, str]) -> list[str]:
    """Build docker run args for starting vLLM."""
    publish_host = env.get("VETINARI_VLLM_DOCKER_PUBLISH_HOST", DEFAULT_VLLM_DOCKER_PUBLISH_HOST)
    args = [
        "docker",
        "run",
        "-d",
        "--name",
        plan.container_name,
        *_container_args_prefix(plan),
        "-p",
        f"{publish_host}:{plan.host_port}:{plan.container_port}",
        "--ipc=host",
    ]
    cache_dir = env.get("VETINARI_VLLM_CACHE_DIR")
    if cache_dir:
        args.extend(["-v", f"{cache_dir}:/root/.cache/huggingface"])
    if env.get("HF_TOKEN"):
        args.extend(["-e", "HF_TOKEN"])
    args.extend([plan.image, "--model", plan.model])
    args.extend(_engine_args(env, container_port=plan.container_port))
    return args


def _build_start_command(plan: VLLMContainerPlan, env: Mapping[str, str]) -> str:
    """Build a secret-free Docker command for display/config."""
    return " ".join(shlex.quote(part) for part in _docker_run_args(plan, env))


def plan_vllm_container_setup(
    hardware: HardwareProfile,
    *,
    endpoint: str | None = None,
    mode: str | None = None,
    env: Mapping[str, str] | None = None,
    endpoint_ready: bool | None = None,
    docker_available: bool | None = None,
    nvidia_runtime_available: bool | None = None,
) -> VLLMContainerPlan:
    """Plan local vLLM container setup without mutating the machine.

    Returns:
        Immutable container setup plan for wizard output and optional startup.
    """
    env_map = os.environ if env is None else env
    setup_mode = _normalize_mode(mode or env_map.get("VETINARI_VLLM_SETUP_MODE"))
    vllm_endpoint = endpoint or env_map.get("VETINARI_VLLM_ENDPOINT") or DEFAULT_VLLM_ENDPOINT
    ready = is_openai_endpoint_ready(vllm_endpoint) if endpoint_ready is None else endpoint_ready
    has_docker = shutil.which("docker") is not None if docker_available is None else docker_available
    device_backend = _device_backend(hardware)
    has_nvidia_runtime = (
        _docker_has_nvidia_runtime()
        if nvidia_runtime_available is None and has_docker and device_backend == "cuda"
        else bool(nvidia_runtime_available)
    )
    hardware_eligible = device_backend in {"cuda", "rocm"}
    image = env_map.get("VETINARI_VLLM_IMAGE") or _default_image(device_backend)
    if not validate_image_digest(image):
        logger.warning(
            "vLLM container image %r is not digest-pinned (@sha256:) — "
            "supply-chain integrity cannot be verified; set VETINARI_VLLM_IMAGE "
            "to a digest-pinned reference for production deployments",
            image,
        )
    model = env_map.get("VETINARI_VLLM_MODEL") or env_map.get("VETINARI_VLLM_MODEL_PATH") or ""
    container_name = env_map.get("VETINARI_VLLM_CONTAINER_NAME", DEFAULT_VLLM_CONTAINER_NAME)
    host_port = int(env_map.get("VETINARI_VLLM_HOST_PORT", _endpoint_host_port(vllm_endpoint)))
    container_port = int(env_map.get("VETINARI_VLLM_CONTAINER_PORT", DEFAULT_VLLM_CONTAINER_PORT))
    hf_token_present = bool(env_map.get("HF_TOKEN"))

    missing: list[str] = []
    if not ready:
        if not hardware_eligible:
            missing.append("gpu_server_hardware")
        if not has_docker:
            missing.append("docker")
        if device_backend == "cuda" and has_docker and not has_nvidia_runtime:
            missing.append("nvidia_container_runtime")
        if not image:
            missing.append("VETINARI_VLLM_IMAGE")
        if not model:
            missing.append("VETINARI_VLLM_MODEL")

    can_auto_start = setup_mode == "auto" and not ready and not missing
    status = _plan_status(ready, hardware_eligible, missing, setup_mode)

    plan = VLLMContainerPlan(
        mode=setup_mode,
        endpoint=vllm_endpoint,
        status=status,
        hardware_eligible=hardware_eligible,
        endpoint_ready=ready,
        docker_available=has_docker,
        nvidia_runtime_available=has_nvidia_runtime,
        device_backend=device_backend,
        image=image,
        model=model,
        container_name=container_name,
        host_port=host_port,
        container_port=container_port,
        hf_token_present=hf_token_present,
        can_auto_start=can_auto_start,
        missing=tuple(missing),
    )

    if hardware_eligible and has_docker and model:
        plan = replace(plan, start_command=_build_start_command(plan, env_map))
    return plan


def _plan_status(ready: bool, hardware_eligible: bool, missing: list[str], setup_mode: str) -> str:
    """Return vLLM setup status from readiness and prerequisites."""
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


def start_vllm_container(
    plan: VLLMContainerPlan,
    *,
    env: Mapping[str, str] | None = None,
    timeout: float = 120.0,
) -> VLLMContainerPlan:
    """Start a planned vLLM container when the user explicitly selected auto mode.

    Returns:
        Updated plan reflecting started or failed container state.
    """
    if not plan.can_auto_start:
        return replace(plan, error="vLLM container plan is not auto-startable")

    run_env = os.environ.copy()
    if env is not None:
        run_env.update(dict(env))

    docker = shutil.which("docker")
    if docker is None:
        return replace(plan, status="start_failed", error="docker executable not found")

    docker_args = _docker_run_args(plan, run_env)
    docker_args[0] = docker
    try:
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
        logger.warning("vLLM container startup command failed: %s", message)
        return replace(plan, status="start_failed", error=message)
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("vLLM container startup failed", exc_info=True)
        return replace(plan, status="start_failed", error=str(exc))

    return replace(plan, status="started", started=True)
