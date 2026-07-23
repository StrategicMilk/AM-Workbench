"""Ponder module — scores available models against a task using benchmark data.

Combines four scoring dimensions: capability (benchmark-weighted or tag-based),
context window fit, memory efficiency, and name-based heuristics.  When a model
has stored benchmark scores the capability dimension is fully data-driven via
benchmarks.yaml.  Tag-based fallback is preserved for cold-start models.

Pipeline role: model selection input — called before every agent dispatch.
"""

from __future__ import annotations

import hashlib
import importlib
import logging
import math
import os
from datetime import datetime, timezone
from typing import Any

from vetinari.models.ponder_scoring import POLICY_SENSITIVE_KEYWORDS, ModelScore, PonderEngine, PonderRanking
from vetinari.safety.prompt_sanitizer import sanitize_task_description

logger = logging.getLogger(__name__)

__all__ = [
    "ENABLE_PONDER_MODEL_DISCOVERY",
    "POLICY_SENSITIVE_KEYWORDS",
    "PONDER_CLOUD_WEIGHT",
    "ModelScore",
    "PonderEngine",
    "PonderRanking",
    "get_all_models_with_cloud",
    "get_available_models",
    "get_cloud_models",
    "get_ponder_health",
    "get_ponder_results_for_plan",
    "ponder_engine",
    "ponder_project_for_plan",
    "rank_models",
    "score_models_with_cloud",
]


ENABLE_PONDER_MODEL_DISCOVERY = os.environ.get("ENABLE_PONDER_MODEL_DISCOVERY", "true").lower() in ("1", "true", "yes")


def _env_unit_float(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        logger.warning("Invalid ponder weight override %s; using %.2f", name, default)
        return default
    if not math.isfinite(value):
        return default
    return max(0.0, min(1.0, value))


PONDER_CLOUD_WEIGHT = _env_unit_float("PONDER_CLOUD_WEIGHT", 0.20)


def _normalize_task_description(task_description: object) -> str:
    if task_description is None:
        return ""
    if isinstance(task_description, str):
        return task_description
    return str(task_description)


def _task_description_for_persistence(task_description: object) -> str:
    """Return a redacted task description safe for result payloads."""
    return sanitize_task_description(_normalize_task_description(task_description))


def _task_description_log_id(task_description: object) -> str:
    normalized = _normalize_task_description(task_description)
    digest = hashlib.sha256(normalized.encode("utf-8", errors="replace")).hexdigest()
    return f"len={len(normalized)} sha256={digest[:12]}"


def get_available_models() -> list[dict]:
    """Get available models.

    Returns:
        List of results.
    """
    try:
        from vetinari.adapters.adapter_cache import get_local_inference_adapter

        adapter = get_local_inference_adapter("model-discovery")
        models = adapter.list_loaded_models()

        if not models:
            return _get_last_known_good_models()

        return [
            {
                "id": m.get("id", m.get("model", "unknown")),
                "name": m.get("id", m.get("model", "unknown")),
                "context_length": m.get("context_length", 8192),
                "quantization": m.get("quantization", "q4_k_m"),
                "tags": m.get("tags", []),
            }
            for m in models
        ]
    except Exception:
        logger.error(
            "Could not get available models from ModelPool — returning last-known-good list",
            exc_info=True,
        )
        return _get_last_known_good_models()


def _get_last_known_good_models() -> list[dict]:
    """Return the last successfully discovered models from ModelPool.

    Falls back to an empty list if no discovery has succeeded yet, rather
    than returning hardcoded model names that may not exist on this system.

    Returns:
        List of model dicts from ModelPool._last_known_good, or empty list.
    """
    try:
        from vetinari.web.shared import get_orchestrator

        orch = get_orchestrator()
        pool = getattr(orch, "model_pool", None)
        if pool is not None:
            last_good = getattr(pool, "_last_known_good", None)
            if last_good:
                return list(last_good)
    except Exception:
        logger.warning("Could not retrieve last-known-good models from orchestrator — returning empty list")
    return []


def rank_models(task_description: str, top_n: int = 3) -> dict[str, Any]:
    """Score available local models against a task and return ranked results.

    Args:
        task_description: Natural-language description of the task to route.
        top_n: Maximum number of top-scoring models to include in the ranking.

    Returns:
        A dict with keys ``task_id``, ``task_description``, ``rankings`` (list
        of per-model score breakdowns including capability, context, memory, and
        heuristic scores), ``timestamp``, and ``phase`` set to ``"result"``.
    """
    engine = PonderEngine()
    models = get_available_models()
    normalized_task_description = _normalize_task_description(task_description)
    safe_task_description = _task_description_for_persistence(normalized_task_description)

    ranking = engine.score_models(models, safe_task_description, top_n)

    return {
        "task_id": ranking.task_id,
        "task_description": safe_task_description,
        "rankings": [
            {
                "rank": i + 1,
                "model_id": r.model_id,
                "model_name": r.model_name,
                "total_score": round(r.total_score, 3),
                "capability_score": round(r.capability_score, 3),
                "context_score": round(r.context_score, 3),
                "memory_score": round(r.memory_score, 3),
                "heuristic_score": round(r.heuristic_score, 3),
                "policy_penalty": r.policy_penalty,
                "reasoning": r.reasoning,
            }
            for i, r in enumerate(ranking.rankings)
        ],
        "timestamp": ranking.timestamp,
        "phase": "result",
    }


def get_cloud_models() -> list[dict]:
    """Get available cloud models from ModelPool.

    Returns:
        List of results.
    """
    try:
        from .model_pool import ModelPool

        config = {}
        pool = ModelPool(config)
        return pool.get_cloud_models()
    except Exception as e:
        logger.error("Error getting cloud models: %s", e)
        return []


def get_all_models_with_cloud() -> list[dict]:
    """Get all available models (local + cloud).

    Returns:
        List of results.
    """
    local_models = get_available_models()
    cloud_models = get_cloud_models()
    return local_models + cloud_models


def _get_model_discovery_candidates(task_description: object, models: list[dict]) -> dict[str, float]:
    """Get model relevance scores from ModelSearchEngine."""
    if not ENABLE_PONDER_MODEL_DISCOVERY:
        return {}

    try:
        try:
            discovery_module = importlib.import_module("vetinari.models.model_discovery")
        except ModuleNotFoundError:
            discovery_module = importlib.import_module("vetinari.model_discovery")

        search_engine = discovery_module.ModelDiscovery()
        candidates = search_engine.search_for_task(_task_description_for_persistence(task_description), models)

        relevance = {}
        for candidate in candidates:
            model_id = candidate.id
            relevance[model_id] = candidate.final_score

        return relevance
    except Exception as e:
        logger.error("Error getting model search candidates: %s", e)
        return {}


def score_models_with_cloud(available_models: list[dict], task_description: object, top_n: int = 3) -> PonderRanking:
    """Score available models, optionally augmented by ModelDiscovery relevance scores.

    When the ``vetinari.models.model_discovery`` module is available and
    ``ENABLE_PONDER_MODEL_DISCOVERY`` is set, each model's score is boosted by a
    cloud-discovery relevance component (weighted by ``PONDER_CLOUD_WEIGHT``).
    When model discovery is unavailable or disabled, the function falls back to
    local-only scoring using capability, context, memory, and heuristic weights —
    no ``ImportError`` is raised and no false cloud-augmentation is claimed.

    Args:
        available_models: List of model dicts to rank.  Each dict must have at
            minimum an ``id`` key.
        task_description: Plain-English description of the task the selected
            model will execute.
        top_n: Maximum number of ranked results to return.

    Returns:
        A ``PonderRanking`` whose ``rankings`` list contains at most ``top_n``
        entries, sorted by descending total score.  The ``reasoning`` field on
        each entry indicates whether a cloud-discovery boost was applied.
    """
    engine = PonderEngine()
    normalized_task_description = _normalize_task_description(task_description)
    safe_task_description = _task_description_for_persistence(normalized_task_description)

    search_relevance = _get_model_discovery_candidates(normalized_task_description, available_models)
    using_cloud = bool(search_relevance)
    if not using_cloud:
        task_log_id = _task_description_log_id(normalized_task_description)
        logger.debug(
            "score_models_with_cloud: model discovery unavailable or disabled — using local-only scoring for task: %s",
            task_log_id,
        )

    requirements = engine._get_task_capability_requirements(safe_task_description)
    scored_models = [
        _score_cloud_model(engine, model, safe_task_description, requirements, search_relevance, using_cloud)
        for model in available_models
    ]
    scored_models.sort(key=lambda x: x.total_score, reverse=True)

    return PonderRanking(
        task_id=f"ponder_cloud_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}",
        task_description=safe_task_description,
        rankings=scored_models[:top_n],
    )


def _score_cloud_model(
    engine: PonderEngine,
    model: dict,
    task_description: str,
    requirements: dict[str, Any],
    search_relevance: dict[str, float],
    using_cloud: bool,
) -> ModelScore:
    cap_score = engine._calculate_capability_score(model, requirements)
    ctx_score = engine._calculate_context_score(model, requirements)
    mem_score = engine._calculate_memory_score(model)
    heur_score = engine._calculate_heuristic_score(model, task_description)
    policy = engine._check_policy_sensitivity(model, requirements)
    model_id = model.get("id", "")
    cloud_boost = search_relevance.get(model_id, 0.0) * PONDER_CLOUD_WEIGHT
    total = (
        cap_score * engine.weights["capability"]
        + ctx_score * engine.weights["context"]
        + mem_score * engine.weights["memory"]
        + heur_score * engine.weights["heuristic"]
        + policy
        + cloud_boost
    )
    return ModelScore(
        model_id=model_id,
        model_name=model.get("name", model_id),
        total_score=total,
        capability_score=cap_score,
        context_score=ctx_score,
        memory_score=mem_score,
        heuristic_score=heur_score,
        policy_penalty=policy,
        reasoning=_cloud_score_reasoning(using_cloud, cap_score, ctx_score, cloud_boost, policy),
    )


def _cloud_score_reasoning(
    using_cloud: bool,
    cap_score: float,
    ctx_score: float,
    cloud_boost: float,
    policy: float,
) -> str:
    reasoning = []
    if not using_cloud:
        reasoning.append("local-only scoring")
    if cap_score > 0.7:
        reasoning.append(f"capability: {cap_score:.2f}")
    if ctx_score > 0.8:
        reasoning.append(f"context: {ctx_score:.2f}")
    if cloud_boost > 0.1:
        reasoning.append(f"cloud boost: +{cloud_boost:.2f}")
    if policy < 0:
        reasoning.append(f"policy: {policy}")
    return ", ".join(reasoning) if reasoning else "balanced"


def ponder_project_for_plan(plan_id: str) -> dict[str, Any]:
    """Run project-wide ponder pass for all subtasks in a plan.

    Returns:
        A dict with keys ``plan_id``, ``total_subtasks``, ``updated_subtasks``
        (count of subtasks that were successfully scored and updated),
        ``errors`` (list of per-subtask error dicts), and ``success`` (False
        only when the plan is not found or has no subtasks).
    """
    import warnings

    from vetinari.planning.planning import get_plan_manager

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        plan_manager = get_plan_manager()
    from vetinari.planning.subtask_tree import subtask_tree

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        plan = plan_manager.get_plan(plan_id)
    if not plan:
        return {"error": f"Plan {plan_id} not found", "success": False}

    all_subtasks = subtask_tree.get_all_subtasks(plan_id)

    if not all_subtasks:
        return {"error": "No subtasks found", "success": False}

    available_models = get_all_models_with_cloud()

    results = {
        "plan_id": plan_id,
        "total_subtasks": len(all_subtasks),
        "updated_subtasks": 0,
        "errors": [],
        "success": True,
    }

    for subtask in all_subtasks:
        try:
            ranking = score_models_with_cloud(available_models, subtask.description, top_n=3)

            ranking_data = [
                {
                    "rank": i + 1,
                    "model_id": r.model_id,
                    "model_name": r.model_name,
                    "total_score": round(r.total_score, 3),
                    "capability_score": round(r.capability_score, 3),
                    "context_score": round(r.context_score, 3),
                    "memory_score": round(r.memory_score, 3),
                    "heuristic_score": round(r.heuristic_score, 3),
                    "policy_penalty": r.policy_penalty,
                    "reasoning": r.reasoning,
                }
                for i, r in enumerate(ranking.rankings)
            ]

            scores = {r.model_id: r.total_score for r in ranking.rankings}

            subtask_tree.update_subtask(
                plan_id,
                subtask.subtask_id,
                {"ponder_ranking": ranking_data, "ponder_scores": scores, "ponder_used": True},
            )

            results["updated_subtasks"] += 1

        except Exception as e:
            results["errors"].append({"subtask_id": subtask.subtask_id, "error": str(e)})

    return results


def get_ponder_results_for_plan(plan_id: str) -> dict[str, Any]:
    """Get ponder results for all subtasks in a plan.

    Returns:
        A dict with keys ``plan_id``, ``total_subtasks``, ``subtasks_with_ponder``
        (count of subtasks that have stored rankings), and ``subtasks`` (list of
        subtask dicts containing ``ponder_ranking``, ``ponder_scores``, and
        ``ponder_used`` for each subtask that has been scored).
    """
    from vetinari.planning.subtask_tree import subtask_tree

    all_subtasks = subtask_tree.get_all_subtasks(plan_id)

    subtask_results = [
        {
            "subtask_id": subtask.subtask_id,
            "description": _task_description_for_persistence(subtask.description),
            "agent_type": subtask.agent_type,
            "ponder_ranking": subtask.ponder_ranking,
            "ponder_scores": subtask.ponder_scores,
            "ponder_used": subtask.ponder_used,
        }
        for subtask in all_subtasks
        if subtask.ponder_ranking or subtask.ponder_scores
    ]

    return {
        "plan_id": plan_id,
        "total_subtasks": len(all_subtasks),
        "subtasks_with_ponder": len(subtask_results),
        "subtasks": subtask_results,
    }


def get_ponder_health() -> dict[str, Any]:
    """Check health/status of cloud providers.

    Returns:
        A dict with keys ``enable_model_discovery`` (whether ponder model
        discovery is active), ``cloud_weight`` (the configured weight given to
        cloud models during scoring), and ``providers`` (per-provider health
        data from ``ModelPool.get_cloud_provider_health()``).
    """
    from .model_pool import ModelPool

    cloud_health = ModelPool.get_cloud_provider_health()

    return {
        "enable_model_discovery": ENABLE_PONDER_MODEL_DISCOVERY,
        "cloud_weight": PONDER_CLOUD_WEIGHT,
        "providers": cloud_health,
    }


ponder_engine = PonderEngine()
