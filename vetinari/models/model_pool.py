"""Model Pool module."""

from __future__ import annotations

import logging
import os
import threading
import time
import weakref
from typing import Any

from vetinari.config_paths import resolve_config_path
from vetinari.constants import THREAD_JOIN_TIMEOUT
from vetinari.models.model_pool_discovery import ModelPoolDiscoveryMixin

logger = logging.getLogger(__name__)


_CLOUD_PROVIDERS_CACHE: dict | None = None
_CLOUD_PROVIDERS_LOCK = threading.Lock()
_CLOUD_PROVIDERS_CONFIG_PATH: str | None = None
_MODEL_POOLS: weakref.WeakSet[Any] | None = None
_MODEL_POOLS_LOCK = threading.Lock()


def _cloud_providers_config_path() -> str:
    global _CLOUD_PROVIDERS_CONFIG_PATH
    if _CLOUD_PROVIDERS_CONFIG_PATH is None:
        _CLOUD_PROVIDERS_CONFIG_PATH = resolve_config_path("cloud_providers.yaml")
    return _CLOUD_PROVIDERS_CONFIG_PATH


def _model_pools() -> weakref.WeakSet[Any]:
    global _MODEL_POOLS
    if _MODEL_POOLS is None:
        _MODEL_POOLS = weakref.WeakSet()
    return _MODEL_POOLS


def _thread_is_alive(thread: Any) -> bool:
    """Return whether a warm-up worker is alive, tolerating thread-like test doubles."""
    is_alive = getattr(thread, "is_alive", None)
    if not callable(is_alive):
        return False
    try:
        return bool(is_alive())
    except Exception:
        logger.warning("[Model Pool] Could not inspect warm-up thread liveness", exc_info=True)
        return False


def _load_cloud_providers() -> dict:
    """Load cloud provider definitions from config/cloud_providers.yaml.

    Cached after first load.  Falls back to empty dict if the file is missing
    (prevents startup failure if config is not yet present).

    Returns:
        Dict mapping provider_id to provider config dict.
    """
    global _CLOUD_PROVIDERS_CACHE
    if _CLOUD_PROVIDERS_CACHE is not None:
        return _CLOUD_PROVIDERS_CACHE
    with _CLOUD_PROVIDERS_LOCK:
        if _CLOUD_PROVIDERS_CACHE is not None:
            return _CLOUD_PROVIDERS_CACHE
        try:
            import yaml

            config_path = _cloud_providers_config_path()
            with open(config_path, encoding="utf-8") as fh:
                data = yaml.safe_load(fh)
            _CLOUD_PROVIDERS_CACHE = data.get("providers", {}) if data else {}
        except FileNotFoundError:
            logger.warning(
                "Cloud providers config not found at %s — no cloud providers available",
                _cloud_providers_config_path(),
            )
            _CLOUD_PROVIDERS_CACHE = {}
        except Exception:
            logger.warning(
                "Failed to load cloud providers from %s — no cloud providers available",
                _cloud_providers_config_path(),
                exc_info=True,
            )
            _CLOUD_PROVIDERS_CACHE = {}
    return _CLOUD_PROVIDERS_CACHE


def _estimate_latency_hint(model_id: str, memory_gb: float) -> str:
    """Estimate model latency category from model name and memory footprint.

    Uses memory_gb as the primary signal (correlates with parameter count):
    - <3 GB   -> fast   (<=3B params at Q4)
    - 3-14 GB -> medium (3B-14B params at Q4)
    - >14 GB  -> slow   (>14B params at Q4)

    Args:
        model_id: Model identifier string (used as fallback heuristic).
        memory_gb: Estimated model memory footprint in gigabytes.

    Returns:
        One of "fast", "medium", or "slow".
    """
    if memory_gb > 0:
        if memory_gb < 3.0:
            return "fast"
        elif memory_gb <= 14.0:
            return "medium"
        else:
            return "slow"

    # Fallback: parse param count from model_id string (e.g., "llama-3b", "qwen-14b")
    import re

    model_lower = model_id.lower()
    match = re.search(r"(\d+(?:\.\d+)?)\s*b\b", model_lower)
    if match:
        params_b = float(match.group(1))
        if params_b < 3:
            return "fast"
        elif params_b <= 14:
            return "medium"
        else:
            return "slow"

    return "medium"  # Default when unable to estimate


def _seed_discovered_models(models: list[dict[str, Any]]) -> None:
    """Seed the Thompson sampler with informed priors for each discovered model.

    Calls :meth:`~vetinari.learning.benchmark_seeder.BenchmarkSeeder.seed_model`
    for each model so the selector starts with capability-aware priors rather
    than an uninformative Beta(1,1).  Failures are non-fatal — seeding is
    best-effort and only affects cold-start behaviour.

    Args:
        models: List of model dicts as produced by ``discover_models``.
    """
    if not models:
        return
    try:
        from vetinari.learning.benchmark_seeder import get_benchmark_seeder

        seeder = get_benchmark_seeder()
        for model in models:
            model_id = model.get("id") or model.get("name")
            if model_id:
                seeder.seed_model(model_id, model_metadata=model)
    except Exception:
        logger.warning(
            "BenchmarkSeeder could not seed %d discovered models — Thompson sampler will use uninformed priors",
            len(models),
        )


class ModelPool(ModelPoolDiscoveryMixin):
    """Model pool for discovering, cataloging, and assigning local and cloud models."""

    def __init__(
        self,
        config: dict | None = None,
        memory_budget_gb: int | None = None,
    ):
        config = config or {}
        self.config = config
        # Use config memory_budget if not explicitly provided
        if memory_budget_gb is None:
            memory_budget_gb = config.get(
                "memory_budget_gb", config.get("discovery_filters", {}).get("max_model_memory_gb", 32)
            )
        self.memory_budget_gb = memory_budget_gb
        self.models = []
        self.discovered = []
        self._last_known_good: list = []  # Preserved across failed discoveries
        self._discovery_lock = threading.Lock()  # Prevents concurrent discover_models() races
        self._warmup_threads: list[threading.Thread] = []
        self._warmup_lock = threading.Lock()
        self._warmup_stop_event = threading.Event()

        # Retry policy — kept short so the UI never blocks for long
        self._discovery_failed = False
        self._fallback_active = False
        self._last_discovery_error = None
        self._discovery_retry_count = 0
        self._max_discovery_retries = int(os.environ.get("VETINARI_MODEL_DISCOVERY_RETRIES", "2"))
        self._discovery_retry_delay_base = float(os.environ.get("VETINARI_MODEL_DISCOVERY_RETRY_DELAY", "0.5"))
        # Total wall-clock cap for discovery retries (prevents startup hanging)
        self._max_discovery_wall_time = 10.0  # seconds

        # llama-swap delegation: when configured, defer model lifecycle to external router
        _routing = config.get("model_routing", {})
        self._model_router = _routing.get("model_router", "internal")
        self._router_url = _routing.get("router_url", "")
        self._llama_swap_enabled = self._model_router == "llama-swap" and bool(self._router_url)
        with _MODEL_POOLS_LOCK:
            _model_pools().add(self)

    def get_discovery_health(self) -> dict[str, Any]:
        """Get health information about model discovery."""
        return {
            "discovery_failed": self._discovery_failed,
            "fallback_active": self._fallback_active,
            "last_error": self._last_discovery_error,
            "retry_count": self._discovery_retry_count,
            "models_available": len(self.models),
            "max_retries": self._max_discovery_retries,
            "retry_delay_base": self._discovery_retry_delay_base,
        }

    def warm_up_primary(self) -> None:
        """Preload all discovered models to reduce first-request cold starts.

        Session 15 originally only warmed the first discovered model, which
        left secondary models cold even when the pool had already loaded them.
        The runtime still uses daemon background threads so startup stays
        non-blocking, but every discovered model now gets a best-effort warm-up.
        """
        if not self.models:
            logger.info("[Model Pool] No models available for warm-up")
            return
        self._warmup_stop_event.clear()

        try:
            from vetinari.shutdown import register_callback

            register_callback(f"ModelPool warmups {id(self)}", self.stop_warmups)
        except Exception:
            logger.warning("[Model Pool] Could not register warm-up shutdown callback", exc_info=True)

        def _warm_up_model(model_id: str) -> None:
            if self._warmup_stop_event.is_set():
                logger.info("[Model Pool] Model %s warm-up skipped during shutdown", model_id)
                return
            try:
                from vetinari.adapters.base import ProviderConfig, ProviderType
                from vetinari.adapters.llama_cpp_adapter import LlamaCppProviderAdapter

                adapter = LlamaCppProviderAdapter(
                    ProviderConfig(provider_type=ProviderType.LOCAL, name="local", endpoint="")
                )
                if self._warmup_stop_event.is_set():
                    logger.info("[Model Pool] Model %s warm-up cancelled before load", model_id)
                    return
                adapter.load_model(model_id)
                if self._warmup_stop_event.is_set():
                    if hasattr(adapter, "unload_model"):
                        adapter.unload_model(model_id)
                    elif hasattr(adapter, "unload_all"):
                        adapter.unload_all()
                    logger.info("[Model Pool] Model %s warm-up unloaded after shutdown request", model_id)
                else:
                    logger.info("[Model Pool] Model %s warm-up complete", model_id)
            except Exception as e:
                logger.warning("[Model Pool] Model %s warm-up failed: %s", model_id, e)

        for index, model in enumerate(self.models):
            model_id = model.get("id", model.get("name", "unknown"))
            logger.info("[Model Pool] Scheduling warm-up for model: %s", model_id)
            thread = threading.Thread(
                target=_warm_up_model,
                args=(model_id,),
                daemon=True,
                name=f"model-warmup-{index}",
            )
            with self._warmup_lock:
                self._warmup_threads = [t for t in self._warmup_threads if _thread_is_alive(t)]
                self._warmup_threads.append(thread)
            thread.start()

    def stop_warmups(self, timeout: float = THREAD_JOIN_TIMEOUT) -> dict[str, Any]:
        """Request model warm-up workers to stop and join them for a bounded time.

        Returns:
            Summary of requested and still-running warm-up workers.
        """
        self._warmup_stop_event.set()
        with self._warmup_lock:
            threads = list(self._warmup_threads)

        deadline = time.monotonic() + max(0.0, timeout)
        for thread in threads:
            remaining = max(0.0, deadline - time.monotonic())
            if _thread_is_alive(thread) and remaining > 0:
                thread.join(timeout=remaining)

        with self._warmup_lock:
            still_alive = [t for t in self._warmup_threads if _thread_is_alive(t)]
            self._warmup_threads = still_alive

        if still_alive:
            logger.warning(
                "[Model Pool] %d model warm-up thread(s) still running after %.1fs: %s",
                len(still_alive),
                timeout,
                [t.name for t in still_alive],
            )
        return {
            "requested_stop": len(threads),
            "still_running": [t.name for t in still_alive],
        }

    def get_warmup_status(self) -> dict[str, Any]:
        """Return observable status for model warm-up workers.

        Returns:
            Current warm-up activity and shutdown-request state.
        """
        with self._warmup_lock:
            threads = list(self._warmup_threads)
        alive = [t.name for t in threads if _thread_is_alive(t)]
        return {
            "active": bool(alive),
            "active_threads": alive,
            "shutdown_requested": self._warmup_stop_event.is_set(),
        }

    def assign_tasks_to_models(self, config: dict) -> None:
        """Assign tasks to models based on capability scoring.

        Args:
            config: Configuration dict containing a ``tasks`` list to assign.
        """
        tasks = config.get("tasks", [])
        for t in tasks:
            best = None
            best_score = -1.0
            for m in self.models:
                score = self._score_task_model(t, m)
                if score > best_score:
                    best = m
                    best_score = score
            t["assigned_model_id"] = best["id"] if best else None
        logger.info("Task-to-model assignments completed.")

    def _score_task_model(self, task: dict, model: dict) -> float:
        required = set(task.get("inputs", []))
        provided = set(model.get("capabilities", []))
        cap_match = len(required & provided) / max(len(required), 1)

        latency = model.get("latency_estimate", 1000)
        latency_norm = max(0.0, 1.0 - (latency / 2000.0))

        # Reliability sourced from cost tracker analytics, scoped to this task type
        task_type = task.get("type", "general")
        reliability = self._get_model_reliability(model.get("id", ""), task_type)

        ctx_len = model.get("context_len", 2048)
        data_size = sum(len(str(x)) for x in task.get("inputs", []))
        context_fit = 1.0 if data_size <= ctx_len else max(0.0, ctx_len / max(1, data_size))

        # Cost-efficiency score from analytics
        cost_efficiency = self._get_cost_efficiency(model.get("id", ""))

        w_cap = 0.30
        w_lat = 0.20
        w_rel = 0.15
        w_ctx = 0.10
        w_cost = 0.15
        w_res = 0.10

        # Estimate resource load from model context usage
        # Models using more of their context window are under heavier load
        resource_load = min(1.0, data_size / max(1, ctx_len)) if ctx_len > 0 else 0.5

        return (
            w_cap * cap_match
            + w_lat * latency_norm
            + w_rel * reliability
            + w_ctx * context_fit
            + w_cost * cost_efficiency
            + w_res * (1.0 - resource_load)
        )

    @staticmethod
    def _get_model_reliability(model_id: str, task_type: str = "general") -> float:
        """Get per-model reliability scoped to a specific task type.

        Looks up per-model performance data from the dynamic router using a
        task-scoped cache key, then falls back to global SLA data. Using the
        task type prevents a model's coding performance from being reported as
        its reasoning reliability.

        Args:
            model_id: The canonical model identifier.
            task_type: The task type for scoped reliability lookup (e.g. "coding",
                "reasoning"). Defaults to "general" when not specified.

        Returns:
            Reliability score in [0.0, 1.0]; 0.8 when no data is available.
        """
        # Try per-model, per-task-type performance data first
        try:
            from vetinari.models.dynamic_model_router import get_model_router

            router = get_model_router()
            cache_key = f"{model_id}:{task_type}"
            perf = router.get_performance_cache(cache_key)
            if perf and "success_rate" in perf:
                return min(1.0, perf["success_rate"])
        except Exception:
            logger.warning("Failed to get model reliability from dynamic router for %s", model_id, exc_info=True)
        # Fall back to global SLA data
        try:
            from vetinari.analytics.sla import get_sla_tracker

            report = get_sla_tracker().get_report("success-rate")
            if report and report.total_samples > 0:
                return min(1.0, report.current_value / 100.0)
        except Exception:
            logger.warning("Failed to get model reliability from SLA tracker for %s", model_id, exc_info=True)
        return 0.8  # conservative prior for unknown models

    @staticmethod
    def _get_cost_efficiency(model_id: str) -> float:
        """Get cost efficiency score (1.0 = free/cheapest, 0.0 = most expensive)."""
        try:
            from vetinari.analytics.cost import get_cost_tracker

            report = get_cost_tracker().get_report()
            if report.total_requests > 0 and report.by_model:
                model_costs = report.by_model
                max_cost = max(model_costs.values()) if model_costs else 1.0
                if max_cost > 0:
                    # Find this model's cost using exact id match; lower cost = higher score
                    for key, cost in model_costs.items():
                        if key == model_id:
                            return max(0.0, 1.0 - (cost / max_cost))
            return 1.0  # No cost data = assume free (local model)
        except Exception:
            logger.warning("Cost efficiency lookup failed for model %s — assuming free", model_id, exc_info=True)
            return 1.0

    def get_cloud_models(self) -> list[dict]:
        """Get available cloud models based on environment tokens.

        Returns:
            List of results.
        """
        cloud_models = []

        for provider_id, provider in _load_cloud_providers().items():
            token = os.environ.get(provider["env_token"]) or os.environ.get(
                provider.get("env_token_fallback", ""),
            )

            if token:
                model = {
                    "id": f"cloud:{provider_id}",
                    "name": provider["name"],
                    "provider": provider_id,
                    "endpoint": provider["endpoint"],
                    "capabilities": provider["tags"],
                    "context_len": provider["context_len"],
                    "memory_gb": provider["memory_gb"],
                    "free_tier": provider["free_tier"],
                    "version": "token-gated-2026-06-24",
                }
                cloud_models.append(model)
                logger.info("Cloud model available: %s", provider["name"])
            else:
                logger.debug("Cloud provider %s not configured (missing %s)", provider["name"], provider["env_token"])

        return cloud_models

    def get_all_available_models(self) -> list[dict]:
        """Get all available models (local + cloud).

        Returns:
            List of results.
        """
        local_models = self.models.copy()
        cloud_models = self.get_cloud_models()
        return local_models + cloud_models

    @staticmethod
    def get_cloud_provider_health() -> dict[str, bool]:
        """Check health/status of cloud providers based on token presence."""
        health = {}
        for provider_id, provider in _load_cloud_providers().items():
            token = os.environ.get(provider["env_token"]) or os.environ.get(
                provider.get("env_token_fallback", ""),
            )
            health[provider_id] = {"available": bool(token), "name": provider["name"], "has_token": bool(token)}
        return health

    def list_models(self) -> list[dict]:
        """Return list of discovered models (alias for discover_models)."""
        return self.models.copy()


def stop_all_model_warmups(timeout: float = THREAD_JOIN_TIMEOUT) -> list[dict[str, Any]]:
    """Stop warm-up workers for all live ModelPool instances.

    Returns:
        One stop summary per live model pool.
    """
    with _MODEL_POOLS_LOCK:
        pools = list(_model_pools())
    return [pool.stop_warmups(timeout=timeout) for pool in pools]
