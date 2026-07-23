"""Training stage execution functions — SFT, SimPO, and DAPO-reward/DPO-loss stages.

These module-level functions accept the TrainingStageOrchestrator instance
(unused) for consistency, but they are pure functions that do not need
instance state. Extracted here to hold dapo.py under the 550-line limit.

Naming honesty (2026-04-25, SHARD-01-revised): the "DAPO" stage in this
module computes DAPO rewards via ``compute_dapo_reward`` but feeds them into
a ``trl.DPOTrainer`` (DPO loss). The reward is DAPO; the loss is DPO. The
inner training-execution helper ``_execute_dapo_reward_dpo_training`` and
the script builder ``build_dapo_reward_dpo_script`` reflect this hybrid in
their names. ``run_dapo_stage`` and the stage-name string ``"dapo"`` keep
their identity from the reward function (the algorithmic surface) and are
not renamed.

This module is part of the training layer:
  TrainingStageOrchestrator (dapo.py) -> stage execution (dapo_stages.py)
  -> reward computation (dapo_rewards.py) -> training pipeline (pipeline.py)
"""

from __future__ import annotations

import importlib.util
import logging
from pathlib import Path
from typing import Any

from vetinari.exceptions import ExecutionError
from vetinari.learning.atomic_writers import _write_text_atomic, write_jsonl_atomic
from vetinari.security.fail_closed import assert_closed_schema, confine_to_root, sanitize_untrusted_text
from vetinari.training.dapo_rewards import DapoExecutionResult, StageResult, compute_dapo_reward
from vetinari.training.dapo_scripts import build_dapo_reward_dpo_script, build_simpo_script
from vetinari.training.loss import SIMPO_BETA_DEFAULT, SIMPO_GAMMA_DEFAULT, run_training_loss
from vetinari.types import StatusEnum, TrainingAlgorithm

logger = logging.getLogger(__name__)


def _is_trl_available() -> bool:
    """Return True when TRL is discoverable without importing its native-adjacent stack."""
    try:
        return importlib.util.find_spec("trl") is not None
    except (ImportError, AttributeError, ValueError):
        logger.warning("TRL availability probe failed", exc_info=True)
        return False


def run_sft_stage(
    model: str,
    dataset_path: Path,
    config: dict[str, Any],
) -> StageResult:
    """Run Supervised Fine-Tuning stage via the existing training pipeline.

    Delegates to TrainingPipeline.run() with config overrides. If the
    pipeline is unavailable (import error, no data), returns a successful
    skipped result so the orchestrator can continue to SimPO/DAPO.

    Args:
        model: Base model path or identifier.
        dataset_path: Path to the training dataset.
        config: Full stage config dict; reads the ``sft`` sub-key.

    Returns:
        StageResult with output_model pointing to the SFT checkpoint, or
        the unchanged ``model`` if the stage was skipped.
    """
    safe_model = sanitize_untrusted_text(model, max_length=2048)
    _safe_dataset_path = Path(sanitize_untrusted_text(str(dataset_path), max_length=4096)).resolve()
    assert_closed_schema(
        config, allowed_keys={"sft", "backend", "model_format", "model_revision", "revision", "dapo", "simpo"}
    )
    try:
        from vetinari.training.pipeline import TrainingPipeline

        pipeline = TrainingPipeline()
        sft_config = config.get("sft", {})
        run = pipeline.run(
            base_model=safe_model,
            task_type=sft_config.get("task_type", "general"),
            min_score=sft_config.get("min_score", 0.7),
            epochs=sft_config.get("epochs", 3),
            backend=sft_config.get("backend", config.get("backend", "llama_cpp")),
            model_format=sft_config.get("model_format", sft_config.get("format", config.get("model_format"))),
            model_revision=sft_config.get("model_revision", sft_config.get("revision", config.get("model_revision"))),
        )
        return StageResult(
            success=True,
            stage_name="sft",
            output_model=str(run.output_model_path) if hasattr(run, "output_model_path") else safe_model,
        )
    except (ImportError, ModuleNotFoundError) as exc:
        # Training libraries not installed — skip gracefully (not a pipeline failure).
        logger.info("SFT stage skipped: training libraries unavailable (%s)", exc)
        return StageResult(
            success=True,
            stage_name="sft",
            output_model=safe_model,
            metrics={StatusEnum.SKIPPED.value: True, "reason": "pipeline_unavailable"},
        )
    except Exception as exc:
        # Real pipeline failure — propagate as failure so orchestrator knows (defect 15 fix).
        logger.warning(
            "SFT stage failed: %s — downstream stages will use the un-fine-tuned base model",
            exc,
        )
        return StageResult(
            success=False,
            stage_name="sft",
            output_model=safe_model,
            error=f"SFT pipeline failed: {exc}",
        )


def run_simpo_stage(
    model: str,
    dataset_path: Path,
    config: dict[str, Any],
) -> StageResult:
    """Run SimPO (reference-free alignment) stage via DPO with simpo loss.

    Curates preference pairs from collected execution data, then trains
    using trl's DPOTrainer with ``loss_type="simpo"``, which does not
    require a reference model. Runs the training in a subprocess to avoid
    GPU memory conflicts with the inference server.

    Args:
        model: Input model path (SFT output).
        dataset_path: Base training data path (used to derive output dir).
        config: Full stage config dict; reads the ``simpo`` sub-key.

    Returns:
        StageResult with the aligned model path or a skipped result when
        trl is not installed or preference data is insufficient.
    """
    safe_model = sanitize_untrusted_text(model, max_length=2048)
    safe_dataset_path = Path(sanitize_untrusted_text(str(dataset_path), max_length=4096)).resolve()
    assert_closed_schema(
        config, allowed_keys={"simpo", "sft", "dapo", "backend", "model_format", "model_revision", "revision"}
    )
    if not _is_trl_available():
        logger.info("trl unavailable - skipping SimPO stage")
        return StageResult(
            success=True,
            stage_name="simpo",
            output_model=safe_model,
            metrics={StatusEnum.SKIPPED.value: True, "reason": "trl_unavailable"},
        )

    # Curate DPO preference pairs
    try:
        from vetinari.training.pipeline import DataCurator

        curator = DataCurator()
        simpo_config = config.get("simpo", {})
        dpo_path = curator.curate_dpo(
            task_type=simpo_config.get("task_type"),
            output_dir=str(safe_dataset_path.parent),
        )
    except (ImportError, ValueError, ExecutionError) as exc:
        logger.info("SimPO: insufficient preference data or curator unavailable: %s", exc)
        return StageResult(
            success=True,
            stage_name="simpo",
            output_model=safe_model,
            metrics={StatusEnum.SKIPPED.value: True, "reason": "insufficient_dpo_pairs"},
        )

    if not dpo_path or not Path(dpo_path).exists():
        logger.info("SimPO: no DPO dataset produced; skipping")
        return StageResult(
            success=True,
            stage_name="simpo",
            output_model=safe_model,
            metrics={StatusEnum.SKIPPED.value: True, "reason": "no_dpo_dataset"},
        )

    return _execute_simpo_training(safe_model, dpo_path, safe_dataset_path, simpo_config)


def _execute_simpo_training(
    model: str,
    dpo_path: str,
    dataset_path: Path,
    simpo_config: dict[str, Any],
) -> StageResult:
    """Run the SimPO training subprocess after data is ready.

    Args:
        model: Input model path.
        dpo_path: Path to the curated DPO dataset.
        dataset_path: Base dataset path (for output dir derivation).
        simpo_config: SimPO-specific config sub-dict.

    Returns:
        StageResult with the adapter path or an error result.
    """
    import subprocess
    import sys

    safe_model = sanitize_untrusted_text(model, max_length=2048)
    safe_dpo_path = Path(sanitize_untrusted_text(str(dpo_path), max_length=4096)).resolve()
    safe_dataset_path = Path(sanitize_untrusted_text(str(dataset_path), max_length=4096)).resolve()
    output_dir = safe_dpo_path.parent / "simpo_output"
    output_dir.mkdir(parents=True, exist_ok=True)

    script = build_simpo_script(
        model=safe_model,
        dpo_path=str(safe_dpo_path),
        output_dir=str(output_dir),
        epochs=int(simpo_config.get("epochs", 1)),
        model_revision=simpo_config.get("model_revision", simpo_config.get("revision")),
        beta=float(simpo_config.get("beta", SIMPO_BETA_DEFAULT)),
        simpo_gamma=float(
            simpo_config.get(
                "simpo_gamma",
                simpo_config.get("gamma", simpo_config.get("gamma_beta_ratio", SIMPO_GAMMA_DEFAULT)),
            )
        ),
    )

    run_training_loss(TrainingAlgorithm.SIMPO, model=model, dataset_path=dpo_path)
    script_path = output_dir / "train_simpo_script.py"
    _write_text_atomic(script_path, script)
    logger.info("SimPO: running training script at %s", script_path)

    proc = subprocess.run(
        [sys.executable, str(script_path)],
        capture_output=True,
        text=True,
    )

    if proc.returncode != 0:
        logger.warning("SimPO training failed: %s", proc.stderr[-2000:])
        return StageResult(
            success=False,
            stage_name="simpo",
            output_model=safe_model,
            error=f"SimPO training failed: {proc.stderr[-500:]}",
        )

    adapter_path = str(output_dir / "lora_adapter")
    logger.info("SimPO: training complete — adapter at %s", adapter_path)
    return StageResult(
        success=True,
        stage_name="simpo",
        output_model=adapter_path,
        metrics={"method": "simpo", "dpo_dataset": str(safe_dpo_path), "dataset_root": str(safe_dataset_path.parent)},
    )


def run_dapo_stage(
    model: str,
    dataset_path: Path,
    config: dict[str, Any],
) -> StageResult:
    """Run the DAPO-reward-ranked DPO training stage.

    Collects response groups from training data, computes DAPO rewards via
    ``compute_dapo_reward`` (tiered: execution > correctness > quality >
    composite), sorts each group into chosen/rejected preference pairs, then
    trains via ``trl.DPOTrainer`` (DPO loss) using the DAPO-ranked pairs.

    The reward signal is DAPO; the training loss is DPO. This is intentional
    and documented at the module top — see also
    ``_execute_dapo_reward_dpo_training`` and ``build_dapo_reward_dpo_script``
    for the inner names that reflect the hybrid.

    Falls back gracefully when trl is not installed or when there are
    insufficient response groups for meaningful preference-pair training.

    Args:
        model: Input model path (SimPO output or SFT output).
        dataset_path: Base training data path.
        config: Full stage config dict; reads the ``dapo`` sub-key.

    Returns:
        StageResult with the trained adapter path, or a skipped result when
        prerequisites are not met.
    """
    safe_model = sanitize_untrusted_text(model, max_length=2048)
    safe_dataset_path = Path(sanitize_untrusted_text(str(dataset_path), max_length=4096)).resolve()
    assert_closed_schema(
        config, allowed_keys={"dapo", "simpo", "sft", "backend", "model_format", "model_revision", "revision"}
    )
    dapo_config = config.get("dapo", {})
    min_groups = dapo_config.get("min_groups", 5)

    # Collect response groups from training data
    try:
        from vetinari.learning.training_data import get_training_collector

        collector = get_training_collector()
        ranking_data = collector.export_ranking_dataset()
    except (ImportError, AttributeError):
        logger.info("DAPO: ranking data export not available; skipping")
        return StageResult(
            success=True,
            stage_name="dapo",
            output_model=safe_model,
            metrics={StatusEnum.SKIPPED.value: True, "reason": "ranking_export_unavailable"},
        )

    if len(ranking_data) < min_groups:
        logger.info(
            "DAPO: only %d groups (need %d); skipping",
            len(ranking_data),
            min_groups,
        )
        return StageResult(
            success=True,
            stage_name="dapo",
            output_model=safe_model,
            metrics={StatusEnum.SKIPPED.value: True, "reason": "insufficient_groups", "groups": len(ranking_data)},
        )

    preference_pairs = _build_dapo_preference_pairs(ranking_data)

    if len(preference_pairs) < 3:
        logger.info("DAPO: only %d preference pairs generated; skipping", len(preference_pairs))
        return StageResult(
            success=True,
            stage_name="dapo",
            output_model=safe_model,
            metrics={StatusEnum.SKIPPED.value: True, "reason": "insufficient_preference_pairs"},
        )

    return _execute_dapo_reward_dpo_training(safe_model, safe_dataset_path, preference_pairs, ranking_data, dapo_config)


def _build_dapo_preference_pairs(ranking_data: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Score each response group with DAPO rewards and build preference pairs.

    Args:
        ranking_data: List of response groups from the training collector.

    Returns:
        List of preference pair dicts with prompt, chosen, and rejected keys.
    """
    preference_pairs = []
    for group in ranking_data:
        responses = group.get("responses", [])
        if len(responses) < 2:
            continue

        scored = []
        for resp in responses:
            quality_metrics = {
                "score": resp.get("score", 0.0),
                "test_pass_rate": resp.get("test_pass_rate", 0.0),
                "static_analysis_score": resp.get("static_analysis_score", 0.5),
            }
            # Defect 18 fix: pass execution status through the typed ExecutionResult
            # parameter rather than embedding it in quality_metrics (which compute_dapo_reward
            # never reads for the execution tier).
            executes = resp.get("success")
            execution_result = DapoExecutionResult(success=bool(executes)) if executes is not None else None
            reward = compute_dapo_reward(quality_metrics, execution_result=execution_result)
            # executes_rank: True→1, None→0, False→-1. Execution truth takes absolute
            # priority over reward magnitude (defect 8 fix: a response that executed
            # successfully MUST rank above one that failed, regardless of reward score).
            executes_rank = 1 if executes is True else (-1 if executes is False else 0)
            scored.append((resp, executes_rank, reward.total_reward))

        # Sort by (executes_rank, reward) descending — defect 8 fix.
        scored.sort(key=lambda x: (x[1], x[2]), reverse=True)
        best = scored[0][0]
        worst = scored[-1][0]
        best_key = (scored[0][1], scored[0][2])
        worst_key = (scored[-1][1], scored[-1][2])
        if best_key > worst_key:
            # Defect 19 fix: include computed reward scores in the pair so the
            # training subprocess can use them for reward-weighted loss rather
            # than treating all chosen/rejected pairs as equally weighted.
            preference_pairs.append({
                "prompt": sanitize_untrusted_text(group.get("prompt", ""), max_length=20_000),
                "chosen": sanitize_untrusted_text(best.get("response", ""), max_length=20_000),
                "rejected": sanitize_untrusted_text(worst.get("response", ""), max_length=20_000),
                "chosen_reward": scored[0][2],
                "rejected_reward": scored[-1][2],
            })

    return preference_pairs


def _execute_dapo_reward_dpo_training(
    model: str,
    dataset_path: Path,
    preference_pairs: list[dict[str, Any]],
    ranking_data: list[dict[str, Any]],
    dapo_config: dict[str, Any],
) -> StageResult:
    """Write the DAPO-ranked preference dataset and run a DPO training subprocess.

    The function writes ``dapo_preferences.jsonl`` (chosen/rejected pairs ranked
    by ``compute_dapo_reward``) and then generates and executes a training
    script via ``build_dapo_reward_dpo_script``. The generated script trains
    with ``trl.DPOTrainer`` (DPO loss), so the *training algorithm* is DPO; the
    DAPO part is the reward-derived ranking signal that produced the pairs.

    Renamed 2026-04-25 from ``_execute_dapo_training`` (SHARD-01-revised) so the
    name carries the DAPO-reward / DPO-loss hybrid instead of overstating the
    method as full DAPO RL training.

    Args:
        model: Input model path.
        dataset_path: Base path for output directory derivation.
        preference_pairs: Pre-built preference pairs to train on.
        ranking_data: Original ranking groups (for metrics).
        dapo_config: DAPO-specific config sub-dict.

    Returns:
        StageResult with the adapter path or an error/skipped result.
    """
    safe_model = sanitize_untrusted_text(model, max_length=2048)
    safe_dataset_path = Path(sanitize_untrusted_text(str(dataset_path), max_length=4096)).resolve()
    output_dir = safe_dataset_path.parent / "dapo_output"
    output_dir.mkdir(parents=True, exist_ok=True)
    dapo_dataset_path = output_dir / "dapo_preferences.jsonl"

    write_jsonl_atomic(dapo_dataset_path, list(preference_pairs))

    logger.info("DAPO: wrote %d preference pairs to %s", len(preference_pairs), dapo_dataset_path)

    if not _is_trl_available():
        logger.info("DAPO: trl unavailable; skipping training after saving data")
        return StageResult(
            success=True,
            stage_name="dapo",
            output_model=model,
            metrics={
                StatusEnum.SKIPPED.value: True,
                "reason": "trl_unavailable",
                "pairs_saved": len(preference_pairs),
            },
        )

    proc = _run_dapo_reward_dpo_script(safe_model, dapo_dataset_path, output_dir, dapo_config)
    if proc.returncode != 0:
        logger.warning("DAPO training failed: %s", proc.stderr[-2000:])
        return StageResult(
            success=False,
            stage_name="dapo",
            output_model=safe_model,
            error=f"DAPO training failed: {proc.stderr[-500:]}",
        )

    adapter_path = str(output_dir / "lora_adapter")
    logger.info("DAPO: training complete — adapter at %s", adapter_path)
    return StageResult(
        success=True,
        stage_name="dapo",
        output_model=adapter_path,
        metrics={
            "method": "dapo",
            "preference_pairs": len(preference_pairs),
            "groups_used": len(ranking_data),
        },
    )


def _run_dapo_reward_dpo_script(model: str, dataset_path: Path, output_dir: Path, dapo_config: dict[str, Any]) -> Any:
    import subprocess
    import sys

    safe_model = sanitize_untrusted_text(model, max_length=2048)
    safe_dataset_path = Path(sanitize_untrusted_text(str(dataset_path), max_length=4096)).resolve()
    output_candidate = Path(output_dir)
    if output_candidate.is_absolute():
        try:
            output_candidate = output_candidate.resolve().relative_to(safe_dataset_path.parent)
        except ValueError as exc:
            raise ValueError("output_dir must stay beside the DAPO dataset") from exc
    safe_output_dir = confine_to_root(safe_dataset_path.parent, output_candidate)
    script = build_dapo_reward_dpo_script(
        model=safe_model,
        dapo_dataset_path=str(safe_dataset_path),
        output_dir=str(safe_output_dir),
        epochs=int(dapo_config.get("epochs", 1)),
        model_revision=dapo_config.get("model_revision", dapo_config.get("revision")),
    )
    script_path = safe_output_dir / "train_dapo_reward_dpo_script.py"
    _write_text_atomic(script_path, script)
    logger.info("DAPO: running training script at %s", script_path)
    return subprocess.run([sys.executable, str(script_path)], capture_output=True, text=True)
