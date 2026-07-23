"""Docker-backed worker sandbox that fails closed when Docker is unavailable."""

from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from vetinari.learning.atomic_writers import _write_text_atomic
from vetinari.sandbox_types import ExecutionResult
from vetinari.security.fail_closed import require_sandbox_or_raise, sanitize_untrusted_text

logger = logging.getLogger(__name__)

DEVELOPER_WORKFLOW_CONTRACT_ID = "RCG-0014-P11"
DOCKER_SANDBOX_WORKFLOW_GUARDS: tuple[str, ...] = (
    "missing Docker returns a failed execution result",
    "worker code is never run unsandboxed as a fallback",
    "invalid tmpfs targets are rejected before docker execution",
    "timeouts return failed execution results with timeout metadata",
)


def developer_workflow_contract() -> dict[str, object]:
    """Return Docker sandbox workflow guarantees verified by pack RCG-0014-P11."""
    return {
        "pack": DEVELOPER_WORKFLOW_CONTRACT_ID,
        "surface": "vetinari/workers/sandbox/docker.py",
        "guards": DOCKER_SANDBOX_WORKFLOW_GUARDS,
    }


class _Runner(Protocol):
    def __call__(self, cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]: ...


@dataclass(frozen=True, slots=True)
class DockerSandboxConfig:
    """Configuration for Docker-based worker execution."""

    image: str = "python:3.12-slim"
    work_dir: Path | None = None
    timeout_s: int = 30
    memory: str = "512m"
    cpus: str = "1"
    network: str = "none"
    tmpfs_target: str = "tmp"

    def __repr__(self) -> str:
        return (
            "DockerSandboxConfig("
            f"image={self.image!r}, timeout_s={self.timeout_s}, memory={self.memory!r}, "
            f"cpus={self.cpus!r}, network={self.network!r})"
        )


class DockerSandbox:
    """Run worker code in an ephemeral Docker container."""

    def __init__(
        self,
        config: DockerSandboxConfig | None = None,
        *,
        docker_executable_resolver: Callable[[str], str | None] = shutil.which,
        runner: _Runner = subprocess.run,
    ) -> None:
        self.config = config or DockerSandboxConfig()
        self._docker_executable_resolver = docker_executable_resolver
        self._runner = runner

    def run_python(self, code: str) -> ExecutionResult:
        """Execute Python code in Docker without unsandboxed fallback.

        Returns:
            Value produced for the caller.
        """
        docker = self._docker_executable_resolver("docker")
        if docker is None:
            return ExecutionResult(
                success=False,
                output="",
                error="Docker is unavailable; refusing to run worker code unsandboxed.",
                return_code=-1,
                metadata={"sandbox": "docker", "available": False},
            )
        require_sandbox_or_raise(self.config, label="docker sandbox config")
        code = sanitize_untrusted_text(code, max_length=200_000)
        owns_root = self.config.work_dir is None
        root = self.config.work_dir or Path(tempfile.mkdtemp(prefix="vetinari_docker_sandbox_"))
        root.mkdir(parents=True, exist_ok=True)
        script_path = root / "worker.py"
        _write_text_atomic(script_path, code)
        tmpfs_target = self._tmpfs_mount_spec()
        start = time.perf_counter_ns()
        try:
            cmd = self._docker_run_command(docker, root, tmpfs_target)
            completed = self._runner(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.config.timeout_s,
            )
            elapsed_ms = int((time.perf_counter_ns() - start) / 1_000_000)
            return ExecutionResult(
                success=completed.returncode == 0,
                output=completed.stdout,
                error=completed.stderr,
                execution_time_ms=elapsed_ms,
                return_code=completed.returncode,
                stdout=completed.stdout,
                stderr=completed.stderr,
                metadata={"sandbox": "docker", "image": self.config.image},
            )
        except subprocess.TimeoutExpired:
            elapsed_ms = int((time.perf_counter_ns() - start) / 1_000_000)
            logger.warning(
                "Docker sandbox timed out",
                extra={
                    "action": "run_worker_python_in_docker",
                    "timeout_s": self.config.timeout_s,
                    "image": self.config.image,
                },
            )
            return ExecutionResult(
                success=False,
                output="",
                error=f"Docker sandbox timed out after {self.config.timeout_s} seconds",
                execution_time_ms=elapsed_ms,
                return_code=-1,
                metadata={"sandbox": "docker", "timeout": True},
            )
        finally:
            self._clean_work_dir(root, remove_root=owns_root)

    def _docker_run_command(self, docker: str, root: Path, tmpfs_target: str) -> list[str]:
        image = sanitize_untrusted_text(self.config.image, max_length=200)
        network = sanitize_untrusted_text(self.config.network, max_length=40)
        return [
            docker,
            "run",
            "--rm",
            "--network",
            network,
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--read-only",
            "--tmpfs",
            tmpfs_target,
            "--memory",
            self.config.memory,
            "--cpus",
            self.config.cpus,
            "-v",
            f"{root.resolve().as_posix()}:/workspace:rw",
            "-w",
            "/workspace",
            image,
            "python",
            "/workspace/worker.py",
        ]

    def _tmpfs_mount_spec(self) -> str:
        target = self.config.tmpfs_target.strip().strip("/")
        if target not in {"tmp", "var/tmp"}:
            raise ValueError("Docker sandbox tmpfs_target must be 'tmp' or 'var/tmp'")
        return f"/{target}:rw,noexec,nosuid,size=64m"

    @staticmethod
    def _clean_work_dir(root: Path, *, remove_root: bool = False) -> None:
        for child in root.iterdir():
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink(missing_ok=True)
        if remove_root:
            root.rmdir()
