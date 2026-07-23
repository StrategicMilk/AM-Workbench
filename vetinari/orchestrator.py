"""Backward-compatible Orchestrator facade.

Wraps TwoLayerOrchestrator and AdapterManager to provide the
``run_task`` / ``run_all`` / ``model_pool`` / ``adapter`` API used by
the web routes and CLI manifest-execution path.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from vetinari.adapter_manager import get_adapter_manager
from vetinari.adapters.base import InferenceRequest
from vetinari.boundary_guards import account_evidence_drop
from vetinari.exceptions import ExecutionError
from vetinari.orchestration.two_layer import get_two_layer_orchestrator
from vetinari.types import StatusEnum

logger = logging.getLogger(__name__)


@dataclass
class _ModelPool:
    """Wrapper exposing the model list from the adapter manager's discovery.

    Falls back to manifest placeholders only when no real models are discovered.

    Args:
        models: Initial list of model descriptor dicts (from manifest).
    """

    models: list[dict[str, Any]]

    def discover_models(self) -> None:
        """Refresh model list from adapter manager's discovered models.

        Replaces the manifest placeholders with real discovered models so that
        model IDs in projects match actually-loadable GGUF files.  Falls back
        to the original manifest list if discovery returns nothing.
        """
        try:
            mgr = get_adapter_manager()
            discovered = mgr.discover_models()
            # Flatten all provider results into a single model list
            real_models: list[dict[str, Any]] = []
            for _provider_models in discovered.values():
                real_models.extend(
                    {
                        "id": m.id,
                        "name": m.name or m.id,
                        "capabilities": m.capabilities,
                        "context_len": m.context_len,
                        "memory_gb": m.memory_gb,
                    }
                    for m in _provider_models
                )
            if real_models:
                self.models = real_models
                logger.info("ModelPool refreshed with %d discovered models", len(real_models))
                # Warm up primary model in background to avoid cold-start latency
                self._warm_up_primary()
        except Exception:
            logger.warning("ModelPool discovery failed, keeping existing models", exc_info=True)

    def _warm_up_primary(self) -> None:
        """Preload the first discovered model in a background thread.

        Delegates to ``ModelPool.warm_up_primary()`` which spawns a daemon
        thread internally. Best-effort: import or call failures are logged
        and swallowed so discovery is never blocked.
        """
        try:
            from vetinari.models.model_pool import ModelPool

            pool = ModelPool({"models": self.models})
            pool.warm_up_primary()
        except Exception as exc:
            logger.warning("Model warm-up setup failed: %s", exc)


class _AdapterBridge:
    """Bridge that exposes a ``chat()`` interface over AdapterManager.infer().

    Routes free-form chat calls through the adapter manager so callers
    don't need to construct InferenceRequest objects directly.
    """

    def chat(
        self,
        model: str,
        system_prompt: str,
        prompt: str,
    ) -> dict[str, Any]:
        """Run a chat inference via the adapter manager.

        Args:
            model: Model name (model_id) to target.
            system_prompt: System-level instruction prepended to the conversation.
            prompt: User message to send.

        Returns:
            Dict with ``output`` (str) and ``latency_ms`` (int) keys.

        Raises:
            RuntimeError: If the adapter manager is unavailable or inference fails.
        """
        mgr = get_adapter_manager()
        req = InferenceRequest(
            model_id=model,
            prompt=prompt,
            system_prompt=system_prompt or None,
        )
        resp = mgr.infer(req)
        return {"output": resp.output, "latency_ms": resp.latency_ms}


class Orchestrator:
    """Facade adapting TwoLayerOrchestrator to the manifest-execution API.

    Accepts the same constructor arguments used by web routes and CLI (config_path,
    execution_mode, api_token) and provides ``run_task``, ``run_all``,
    ``model_pool``, and ``adapter`` as a stable interface over TwoLayerOrchestrator.

    Args:
        config_path: Path to the manifest YAML file.
        execution_mode: Execution mode hint forwarded as context (e.g. "execution",
            "planning", "sandbox").
        api_token: Optional API token (unused by TwoLayerOrchestrator; accepted
            for API compatibility).
    """

    def __init__(
        self,
        config_path: str,
        execution_mode: str = "execution",
        api_token: str | None = None,
    ) -> None:
        self._config_path = Path(config_path)
        self._execution_mode = execution_mode
        self._manifest: dict[str, Any] = self._load_manifest()
        self._agent_context: dict[str, Any] = {
            "config_path": str(config_path),
            "execution_mode": execution_mode,
        }
        self.adapter = _AdapterBridge()
        self.model_pool = _ModelPool(self._manifest.get("models", []))

    def _load_manifest(self) -> dict[str, Any]:
        """Load and return the manifest YAML.

        Parses the YAML file at the configured config_path.  Returns an empty
        dict when the file is absent so callers receive empty collections rather
        than hard failures.

        Returns:
            Parsed manifest as a dict (may be empty if file not found).
        """
        if self._config_path.exists():
            with self._config_path.open(encoding="utf-8") as f:
                return yaml.safe_load(f) or {}
        logger.warning("Manifest not found: %s", self._config_path)
        return {}

    def run_task(self, task_id: str) -> dict[str, Any]:
        """Execute a single manifest task by its ID.

        Looks up the task in the manifest by ``id``, converts its
        ``description`` to a goal string, and delegates execution to
        TwoLayerOrchestrator.generate_and_execute().

        Args:
            task_id: The ``id`` field of the target task in the manifest YAML.

        Returns:
            Execution results dict from TwoLayerOrchestrator (includes
            ``completed``, ``plan``, and ``outputs`` keys).

        Raises:
            ValueError: If no task with the given task_id exists in the manifest.
        """
        tasks = self._manifest.get("tasks", [])
        task = next((t for t in tasks if t.get("id") == task_id), None)
        if task is None:
            raise ExecutionError(f"Task {task_id!r} not found in manifest")
        goal = task.get("description", task_id)
        return self._execute_goal(goal, {"task_id": task_id, "task": task})

    def run_all(self) -> dict[str, Any]:
        """Execute all tasks in the manifest sequentially.

        Iterates over every task in the manifest and calls
        _execute_goal for each one.  Individual task failures are logged
        and counted but do not abort the remaining tasks.

        Returns:
            Aggregated results with ``completed`` (int), ``total`` (int),
            and ``results`` (list) keys.
        """
        tasks = self._manifest.get("tasks", [])
        completed = 0
        results: list[dict[str, Any]] = []
        for task in tasks:
            task_id = task.get("id", "")
            goal = task.get("description", task_id)
            try:
                result = self._execute_goal(goal, {"task_id": task_id, "task": task})
                results.append(result)
                # Only count as completed when the result does not signal failure.
                # Checks both status-string payloads ({"status": "failed"}) and
                # count-based payloads ({"failed": 1}) so no failure format slips through.
                _is_failed = (
                    result.get("status") == StatusEnum.FAILED.value
                    or int(result.get(StatusEnum.FAILED.value, 0) or 0) > 0
                )
                if not _is_failed:
                    completed += 1
            except Exception as exc:
                logger.exception("Task %s failed during run_all", task_id)
                account_evidence_drop(task_id, "run_all_task_exception", logger=logger)
                results.append({
                    "task_id": task_id,
                    "status": StatusEnum.FAILED.value,
                    "error": str(exc),
                })
        return {StatusEnum.COMPLETED.value: completed, "total": len(tasks), "results": results}

    @staticmethod
    def _register_default_slos() -> None:
        """Register the default Service Level Objectives with the SLA tracker.

        Registers four baseline SLOs that define acceptable performance bounds
        for the orchestration system:

        - ``latency-p95``: p95 inference latency ≤ 2000 ms
        - ``success-rate``: minimum 95 % success rate
        - ``error-rate``: maximum 5 % error rate
        - ``approval-rate``: minimum 90 % plan approval rate
        """
        from vetinari.analytics.sla import SLOTarget, SLOType, get_sla_tracker
        from vetinari.constants import CACHE_TTL_ONE_DAY

        tracker = get_sla_tracker()
        defaults: list[SLOTarget] = [
            SLOTarget(
                name="latency-p95",
                slo_type=SLOType.LATENCY_P95,
                budget=2000.0,  # ms — p95 inference must be under 2 seconds
                window_seconds=float(CACHE_TTL_ONE_DAY),
                description="95th-percentile inference latency budget",
            ),
            SLOTarget(
                name="success-rate",
                slo_type=SLOType.SUCCESS_RATE,
                budget=95.0,  # minimum 95 % of tasks must succeed
                window_seconds=float(CACHE_TTL_ONE_DAY),
                description="Minimum fraction of tasks completing successfully",
            ),
            SLOTarget(
                name="error-rate",
                slo_type=SLOType.ERROR_RATE,
                budget=5.0,  # maximum 5 % error rate
                window_seconds=float(CACHE_TTL_ONE_DAY),
                description="Maximum fraction of tasks ending in error",
            ),
            SLOTarget(
                name="approval-rate",
                slo_type=SLOType.APPROVAL_RATE,
                budget=90.0,  # minimum 90 % plan approval rate
                window_seconds=float(CACHE_TTL_ONE_DAY),
                description="Minimum fraction of generated plans approved",
            ),
        ]
        for slo in defaults:
            tracker.register_slo(slo)
        logger.info("Registered %d default SLOs", len(defaults))

    def _execute_goal(self, goal: str, context: dict[str, Any]) -> dict[str, Any]:
        """Delegate goal execution to TwoLayerOrchestrator.

        Sets the agent context on the orchestrator before execution so it
        inherits the manifest path and execution mode.

        Args:
            goal: Natural-language goal description passed to the planner.
            context: Additional key-value context merged into the constraints.

        Returns:
            Execution results dict from TwoLayerOrchestrator.
        """
        orch = get_two_layer_orchestrator()
        orch.set_agent_context(self._agent_context)
        return orch.generate_and_execute(
            goal=goal,
            constraints={**context, "mode": self._execution_mode},
        )
