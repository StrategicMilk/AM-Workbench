"""Worker code-execution sandbox selection."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from vetinari.agents.contracts import AgentResult, AgentTask
    from vetinari.workers.sandbox.docker import DockerSandboxConfig

_DOCKER_BACKENDS = frozenset({"docker", "docker_sandbox", "dockersandbox"})
_REQUIRED_POLICIES = frozenset({"sandbox_required", "require_sandbox", "docker", "docker_sandbox"})


def execute_worker_code_in_configured_sandbox(task: AgentTask) -> AgentResult | None:
    """Run Worker code through DockerSandbox when task policy requires it.

    Returning ``None`` means the task did not opt into sandboxed code execution
    and the normal Worker build delegate should continue. Any explicit sandbox
    requirement returns an AgentResult, including fail-closed errors.

    Returns:
        Value produced for the caller.
    """
    context = task.context if isinstance(task.context, dict) else {}
    sandbox_config = _sandbox_config(context)
    backend = _selected_backend(context, sandbox_config)
    required = _is_sandbox_required(context, sandbox_config, backend)
    if required and not backend:
        backend = "docker"

    if not required and backend not in _DOCKER_BACKENDS:
        return None
    if backend not in _DOCKER_BACKENDS:
        return _failure(
            "Sandbox-required Worker code execution requested an unsupported backend.",
            backend=backend or "unknown",
        )

    code = _extract_code(task, context)
    if not code.strip():
        return _failure(
            "Sandbox-required Worker code execution did not provide code to execute.",
            backend=backend,
        )

    from vetinari.sandbox.guardrails import CodeExecutionGuardrail
    from vetinari.workers.sandbox.docker import DockerSandbox

    guardrail = CodeExecutionGuardrail().check(code)
    if not guardrail.passed:
        return _failure(
            f"Sandbox guardrail blocked Worker code execution: {guardrail.reason}",
            backend=backend,
        )

    docker_result = DockerSandbox(_docker_config(sandbox_config)).run_python(code)
    output = {
        "stdout": docker_result.output or docker_result.stdout,
        "stderr": docker_result.error or docker_result.stderr,
        "returncode": docker_result.return_code,
    }
    metadata = {
        "sandbox_required": True,
        "sandbox_backend": "docker",
        "docker_metadata": docker_result.metadata,
    }
    errors = [] if docker_result.success else [docker_result.error or "Docker sandbox execution failed"]
    return _agent_result(
        success=docker_result.success,
        output=output,
        errors=errors,
        metadata=metadata,
        output_type="code_execution",
    )


def _sandbox_config(context: Mapping[str, Any]) -> Mapping[str, Any]:
    raw = context.get("sandbox")
    return raw if isinstance(raw, Mapping) else {}


def _selected_backend(context: Mapping[str, Any], sandbox_config: Mapping[str, Any]) -> str:
    raw = sandbox_config.get("backend") or sandbox_config.get("type") or context.get("sandbox_backend")
    return str(raw or "").strip().lower()


def _is_sandbox_required(
    context: Mapping[str, Any],
    sandbox_config: Mapping[str, Any],
    backend: str,
) -> bool:
    if backend in _DOCKER_BACKENDS:
        return True
    if context.get("sandbox_required") is True or context.get("requires_sandbox") is True:
        return True
    if sandbox_config.get("required") is True:
        return True
    policy = str(context.get("execution_policy") or sandbox_config.get("policy") or "").strip().lower()
    return policy in _REQUIRED_POLICIES


def _extract_code(task: AgentTask, context: Mapping[str, Any]) -> str:
    for key in ("code", "source", "python"):
        value = context.get(key)
        if isinstance(value, str):
            return value
    if task.prompt and str(context.get("prompt_is_code", "")).strip().lower() == "true":
        return task.prompt
    return ""


def _docker_config(sandbox_config: Mapping[str, Any]) -> DockerSandboxConfig:
    from vetinari.workers.sandbox.docker import DockerSandboxConfig

    kwargs: dict[str, Any] = {}
    for key in ("image", "timeout_s", "memory", "cpus", "network", "tmpfs_target"):
        if key in sandbox_config:
            kwargs[key] = sandbox_config[key]
    work_dir = sandbox_config.get("work_dir")
    if work_dir:
        kwargs["work_dir"] = Path(str(work_dir))
    return DockerSandboxConfig(**kwargs)


def _failure(message: str, *, backend: str) -> AgentResult:
    return _agent_result(
        success=False,
        output={"stdout": "", "stderr": message, "returncode": -1},
        errors=[message],
        metadata={
            "sandbox_required": True,
            "sandbox_backend": backend,
            "sandbox_failed_closed": True,
        },
        output_type="code_execution",
    )


def _agent_result(**kwargs: Any) -> AgentResult:
    from vetinari.agents.contracts import AgentResult

    return AgentResult(**kwargs)
