"""Vetinari Training Pipeline - orchestration layer."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from vetinari.constants import OPERATOR_MODELS_CACHE_DIR
from vetinari.learning import atomic_writers

from .pipeline_core import (
    BenchmarkTracker,
    CloudOutputStore,
    DataCurator,
    TrainingRun,
    _ensure_packages,
    _set_training_run_field,
)
from .pipeline_distillation import ContextDistillationDatasetBuilder, DistillationDatasetInfo
from .pipeline_eval import (
    _configure_training_eval_holdout,
    _mark_training_eval_unavailable,
    _prepare_training_eval_split,
    _write_training_eval_evidence,
)
from .pipeline_models import (
    _DEFAULT_TRAINING_BASE_MODEL,
    _TRAINING_NATIVE_BACKENDS,
    _normalize_training_backend,
    _normalize_training_format,
    _read_model_revision_manifest,
    _record_unsupported_cloud_training,
)
from .pipeline_models import (
    _native_model_roots as _native_model_roots_for_training,
)
from .pipeline_models import (
    _resolve_base_model as _resolve_base_model_for_training,
)
from .pipeline_models import (
    _resolve_model_revision as _resolve_model_revision_for_training,
)
from .pipeline_run_support import (
    _count_jsonl_records,
    _emit_training_step_receipt,
    _persist_run_record,
    _record_improvement_archive,
    _update_replay_buffer,
)
from .pipeline_trainers import GGUFConverter, LocalTrainer, ModelDeployer, _validate_training_schedule

logger = logging.getLogger(__name__)


BOUNDARY_ADR = "ADR-0132"
CANONICAL_BOUNDARY = "training.execution"
_MODELS_DIR = Path(OPERATOR_MODELS_CACHE_DIR)
_NATIVE_MODELS_DIR = Path(os.environ.get("VETINARI_NATIVE_MODELS_DIR", str(_MODELS_DIR / "native")))

__all__ = [
    "BenchmarkTracker",
    "CloudOutputStore",
    "ContextDistillationDatasetBuilder",
    "DataCurator",
    "DistillationDatasetInfo",
    "GGUFConverter",
    "LocalTrainer",
    "ModelDeployer",
    "TrainingPipeline",
    "TrainingRun",
    "_ensure_packages",
]

_COMPAT_PRIVATE_EXPORTS = (
    _DEFAULT_TRAINING_BASE_MODEL,
    _prepare_training_eval_split,
    _read_model_revision_manifest,
    _write_training_eval_evidence,
)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _ensure_replay_dataset_provenance(
    dataset_path: str,
    *,
    source_dataset_path: str,
    run_id: str,
) -> str:
    """Add deterministic provenance to replay-bound rows that only have privacy evidence."""
    path = Path(dataset_path)
    source_path = Path(source_dataset_path)
    source_revision = _sha256_file(source_path)
    rows: list[dict[str, Any]] = []
    changed = False

    with path.open(encoding="utf-8") as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            stripped = raw_line.strip()
            if not stripped:
                continue
            row = json.loads(stripped)
            if not isinstance(row, dict):
                raise ValueError(f"{path}:{line_number} must contain a JSON object")
            metadata = dict(row.get("metadata") or {})
            if not (metadata.get("dataset_revision") or metadata.get("source_dataset") or metadata.get("provenance")):
                metadata["source_dataset"] = str(source_path)
                metadata["dataset_revision"] = source_revision
                metadata["provenance"] = "training.pipeline.eval_holdout"
                metadata["training_run_id"] = run_id
                row["metadata"] = metadata
                changed = True
            rows.append(row)

    if changed:
        atomic_writers.write_jsonl_atomic(path, rows)
    return str(path)


class TrainingPipeline:
    """Orchestrates the full training lifecycle."""

    def __init__(self) -> None:
        self._curator = DataCurator()
        self._trainer = LocalTrainer()
        self._converter = GGUFConverter()
        self._deployer = ModelDeployer()
        from vetinari.training.continual_learning import LoRAAdapterManager, ReplayBuffer, STABLERegularizer

        self._regularizer = STABLERegularizer()
        self._replay_buffer = ReplayBuffer()
        self._adapter_manager = LoRAAdapterManager()

    def check_requirements(self) -> dict[str, Any]:
        """Check what training capabilities are available.

        Returns:
            Value produced for the caller.
        """
        avail = self._trainer.check_available()
        return {
            "libraries": avail,
            "ready_for_training": avail.get("trl", False) or avail.get("unsloth", False),
            "models_dir": str(_MODELS_DIR),
            "models_dir_exists": _MODELS_DIR.exists(),
            "native_models_dir": str(_NATIVE_MODELS_DIR),
            "native_models_dir_exists": _NATIVE_MODELS_DIR.exists(),
        }

    @staticmethod
    def _resolve_base_model(base_model: str) -> str:
        """Resolve 'auto' to an actual model identifier for training."""
        return _resolve_base_model_for_training(base_model)

    @staticmethod
    def _native_model_roots() -> list[Path]:
        """Return configured native-model roots lazily."""
        return _native_model_roots_for_training()

    @staticmethod
    def _resolve_model_revision(base_model: str, model_revision: str | None = None) -> str | None:
        """Resolve the immutable revision used by generated training loaders."""
        return _resolve_model_revision_for_training(base_model, model_revision)

    def train_cloud(self, config: dict[str, Any]) -> TrainingRun:
        """Record an unsupported cloud training request as a failed run."""
        return _record_unsupported_cloud_training(config)

    @staticmethod
    def _new_run(
        base_model: str,
        task_type: str | None,
        epochs: int,
        gradient_accumulation_steps: int,
        warmup_ratio: float,
        output_base_dir: str,
        backend: str,
        model_format: str,
        resolved_revision: str | None,
    ) -> tuple[str, Path, TrainingRun]:
        """Create a new run directory and TrainingRun record."""
        import uuid

        run_id = f"run_{uuid.uuid4().hex[:8]}"
        run_dir = Path(output_base_dir) / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        run = TrainingRun(
            run_id=run_id,
            timestamp=datetime.now(timezone.utc).isoformat(),
            base_model=base_model,
            task_type=task_type or "all",
            training_examples=0,
            epochs=epochs,
            success=False,
            gradient_accumulation_steps=gradient_accumulation_steps,
            warmup_ratio=warmup_ratio,
            backend=backend,
            model_format=model_format,
            model_revision=resolved_revision or "",
        )
        return run_id, run_dir, run

    def _curate_or_use_dataset(
        self,
        run_id: str,
        run_dir: Path,
        task_type: str | None,
        min_score: float,
        dataset_path: str | None,
    ) -> str:
        """Return the dataset path, curating it when the caller did not provide one."""
        if dataset_path is not None:
            logger.info("[TrainingPipeline] %s: Using caller-supplied dataset at %s", run_id, dataset_path)
            return dataset_path
        logger.info("[TrainingPipeline] %s: Curating training data...", run_id)
        return self._curator.curate(task_type=task_type, min_score=min_score, output_dir=str(run_dir))

    @staticmethod
    def _handle_insufficient_data(
        run: TrainingRun,
        run_dir: Path,
        run_id: str,
        dataset_path: str,
    ) -> bool:
        """Mark an insufficient-data run and return True when training should stop."""
        if run.training_examples >= 10:
            return False
        _set_training_run_field(run, "error", f"Insufficient training data ({run.training_examples} examples)")
        _mark_training_eval_unavailable(
            run,
            run_dir=run_dir,
            run_id=run_id,
            dataset_path=dataset_path,
            reason="insufficient training data for deterministic eval holdout",
        )
        return True

    def _prepare_training_dataset(
        self,
        run: TrainingRun,
        run_dir: Path,
        run_id: str,
        dataset_path: str,
    ) -> tuple[str, str | None]:
        """Configure eval holdout, replay mixing, and forgetting baseline."""
        source_dataset_path = dataset_path
        dataset_path, eval_dataset_path = _configure_training_eval_holdout(
            run,
            dataset_path=dataset_path,
            run_dir=run_dir,
            run_id=run_id,
        )
        dataset_path = _ensure_replay_dataset_provenance(
            dataset_path,
            source_dataset_path=source_dataset_path,
            run_id=run_id,
        )
        try:
            mixed_path = self._replay_buffer.create_mixed_dataset(
                new_data_path=dataset_path,
                output_path=str(run_dir / "mixed_dataset.jsonl"),
            )
            if mixed_path:
                logger.info("[TrainingPipeline] %s: Mixed dataset created with replay buffer", run_id)
                dataset_path = mixed_path
        except Exception as exc:
            raise RuntimeError(
                f"[TrainingPipeline] {run_id}: replay buffer mixing failed; raw dataset replay is blocked"
            ) from exc
        return dataset_path, eval_dataset_path

    def _capture_forgetting_baseline(
        self,
        run: TrainingRun,
        run_id: str,
        base_model: str,
        validation_data_path: str | None,
        model_revision: str | None,
    ) -> None:
        """Capture continual-learning baseline before training."""
        if not validation_data_path:
            message = "STABLE baseline capture requires a validation holdout"
            _set_training_run_field(run, "error", message)
            raise RuntimeError(message)
        try:
            captured = self._regularizer.capture_baseline(
                base_model,
                validation_data_path,
                model_revision=model_revision if model_revision and not Path(base_model).exists() else None,
            )
        except Exception as exc:
            message = f"STABLE baseline capture failed: {exc}"
            _set_training_run_field(run, "error", message)
            raise RuntimeError(message) from exc
        if not captured:
            message = "STABLE baseline capture did not produce validation metrics"
            _set_training_run_field(run, "error", message)
            raise RuntimeError(message)

    def _train_adapter(
        self,
        run: TrainingRun,
        run_id: str,
        base_model: str,
        dataset_path: str,
        run_dir: Path,
        epochs: int,
        resolved_revision: str | None,
        eval_dataset_path: str | None,
        gradient_accumulation_steps: int,
        warmup_ratio: float,
    ) -> str:
        """Run QLoRA training and store the adapter path on the run."""
        logger.info("[TrainingPipeline] %s: Starting QLoRA training...", run_id)
        adapter_path = self._trainer.train_qlora(
            base_model=base_model,
            dataset_path=dataset_path,
            output_dir=str(run_dir),
            epochs=epochs,
            use_unsloth=False,
            model_revision=resolved_revision,
            eval_dataset_path=eval_dataset_path,
            gradient_accumulation_steps=gradient_accumulation_steps,
            warmup_ratio=warmup_ratio,
        )
        _set_training_run_field(run, "adapter_path", adapter_path)
        logger.info("[TrainingPipeline] %s: Training complete -> %s", run_id, adapter_path)
        return adapter_path

    def _check_forgetting(
        self,
        run: TrainingRun,
        run_id: str,
        base_model: str,
        dataset_path: str,
        resolved_revision: str | None,
        *,
        adapter_path: str,
    ) -> None:
        """Measure the trained adapter against baseline and block unsafe deployment."""
        try:
            model_revision = resolved_revision if resolved_revision and not Path(base_model).exists() else None
            if self._regularizer.should_stop_training(
                base_model,
                dataset_path,
                model_revision=model_revision,
                adapter_path=adapter_path,
            ):
                message = "Possible catastrophic forgetting detected - adapter deployment blocked"
                _set_training_run_field(run, "error", message)
                raise RuntimeError(message)
        except Exception as exc:
            if isinstance(exc, RuntimeError) and str(exc).startswith("Possible catastrophic forgetting"):
                raise
            message = "Forgetting check failed - adapter deployment blocked until validation succeeds"
            _set_training_run_field(run, "error", message)
            raise RuntimeError(message) from exc

    def _run_deployment_quality_gate(
        self,
        run: TrainingRun,
        run_id: str,
        base_model: str,
        adapter_path: str,
        resolved_revision: str | None,
        eval_tasks: list[dict[str, str]] | None,
    ) -> None:
        """Run the deployment quality gate before publishing an adapter."""
        from vetinari.training.quality_gate import TrainingEvaluationArtifact, get_training_quality_gate

        if not resolved_revision:
            msg = "Training quality gate requires immutable base-model provenance"
            raise RuntimeError(msg)

        gate_decision = get_training_quality_gate().evaluate(
            candidate_model_id=adapter_path,
            baseline_model_id=base_model,
            eval_tasks=eval_tasks,
            candidate_artifact=TrainingEvaluationArtifact(
                artifact_type="peft_adapter",
                model_format="safetensors",
                path=adapter_path,
                base_model_id=base_model,
                base_model_revision=resolved_revision,
                device="cuda",
            ),
        )
        _set_training_run_field(run, "eval_score", gate_decision.candidate_quality)
        _set_training_run_field(run, "baseline_score", gate_decision.baseline_quality)
        if run.eval_status in {"", "not_started"}:
            _set_training_run_field(run, "eval_status", gate_decision.decision)
        _set_training_run_field(run, "eval_reason", f"quality_gate={gate_decision.decision}: {gate_decision.reasoning}")
        if gate_decision.decision != "deploy":
            _set_training_run_field(run, "eval_status", gate_decision.decision)
            message = f"Training quality gate rejected adapter deployment: {gate_decision.reasoning}"
            _set_training_run_field(run, "error", message)
            raise RuntimeError(message)
        logger.info("[TrainingPipeline] %s: Quality gate passed for %s", run_id, adapter_path)

    def _deploy_adapter(
        self,
        run: TrainingRun,
        run_id: str,
        base_model: str,
        adapter_path: str,
        run_dir: Path,
        backend: str,
        model_format: str,
        resolved_revision: str | None,
        task_type: str | None,
    ) -> tuple[str, str]:
        """Deploy the trained adapter and return deployed path plus task key."""
        task_key = task_type or "general"
        model_name = f"vetinari-{task_type or 'general'}-{base_model.rsplit('/', maxsplit=1)[-1]}"
        if backend in _TRAINING_NATIVE_BACKENDS:
            logger.info("[TrainingPipeline] %s: Deploying native %s/%s adapter...", run_id, backend, model_format)
            native_deploy = self._deployer.deploy_native(
                adapter_path,
                model_name,
                backend=backend,
                model_format=model_format,
                base_model=base_model,
                base_model_revision=resolved_revision,
                run_id=run_id,
                task_type=task_key,
            )
            _set_training_run_field(run, "model_manifest_path", native_deploy.get("manifest_path", ""))
            deployed_path = native_deploy["path"]
        else:
            logger.info("[TrainingPipeline] %s: Converting to GGUF...", run_id)
            gguf_path = self._converter.convert(
                base_model, adapter_path, str(run_dir), model_revision=resolved_revision
            )
            deployed_path = self._deployer.deploy(gguf_path, model_name)
        _set_training_run_field(run, "output_model_path", deployed_path)
        return deployed_path, task_key

    def _register_adapter_and_replay(self, run_id: str, task_key: str, adapter_path: str, dataset_path: str) -> None:
        """Register adapter and update replay buffer after deployment."""
        try:
            self._adapter_manager.register_adapter(task_key, adapter_path)
            logger.info("[TrainingPipeline] %s: Adapter registered for task type '%s'", run_id, task_key)
        except Exception:
            logger.warning(
                "[TrainingPipeline] %s: Adapter registration failed; adapter at %s must be registered manually",
                run_id,
                adapter_path,
            )
        try:
            _update_replay_buffer(self._replay_buffer, dataset_path=dataset_path, run_id=run_id)
        except (OSError, RuntimeError, ValueError, TypeError, AttributeError):
            logger.warning("[TrainingPipeline] %s: Replay buffer update failed; dataset will not be replayed", run_id)

    @staticmethod
    def _record_archive(
        run: TrainingRun,
        run_id: str,
        base_model: str,
        task_key: str,
        epochs: int,
        backend: str,
        model_format: str,
        resolved_revision: str | None,
        deployed_path: str,
    ) -> None:
        """Record the deployed configuration in the improvement archive."""
        try:
            _record_improvement_archive(
                run=run,
                run_id=run_id,
                base_model=base_model,
                task_key=task_key,
                epochs=epochs,
                backend=backend,
                model_format=model_format,
                model_revision=resolved_revision,
                deployed_path=deployed_path,
            )
        except (RuntimeError, OSError, sqlite3.Error, AttributeError, TypeError, ValueError) as exc:
            logger.warning(
                "[TrainingPipeline] %s: Could not record deployed config in improvement archive: %s", run_id, exc
            )

    @staticmethod
    def _finalize_run(
        run: TrainingRun,
        run_dir: Path,
        run_id: str,
        task_type: str | None,
        base_model: str,
        backend: str,
        epochs: int,
        release_ok: bool,
        cpu_tier: Any | None,
    ) -> None:
        """Persist run records, emit receipts, and release CPU tier exactly once."""
        _persist_run_record(run_dir=run_dir, run_id=run_id, run=run)
        _emit_training_step_receipt(
            run=run,
            run_id=run_id,
            task_type=task_type,
            base_model=base_model,
            backend=backend,
            epochs=epochs,
        )
        if release_ok and cpu_tier is not None:
            cpu_tier.release_finished()

    def run(
        self,
        base_model: str,
        task_type: str | None = None,
        min_score: float = 0.8,
        epochs: int = 3,
        gradient_accumulation_steps: int = 1,
        warmup_ratio: float = 0.0,
        output_base_dir: str = "./training_runs",
        dataset_path: str | None = None,
        backend: str = "vllm",
        model_format: str | None = None,
        model_revision: str | None = None,
        quality_eval_tasks: list[dict[str, str]] | None = None,
        cpu_tier: Any | None = None,
        release_timeout_s: float = 30.0,
    ) -> TrainingRun:
        """Run the complete training pipeline end-to-end.

        Args:
            base_model: Base model value consumed by run().
            task_type: Task type value consumed by run().
            min_score: Score value evaluated by the operation.
            epochs: Epochs value consumed by run().
            gradient_accumulation_steps: Gradient accumulation steps passed to local SFT training.
            warmup_ratio: Learning-rate warmup ratio passed to local SFT training.
            output_base_dir: Output base dir value consumed by run().
            dataset_path: Filesystem path read or written by the operation.
            backend: Backend value consumed by run().
            model_format: Model format value consumed by run().
            model_revision: Model revision value consumed by run().
            quality_eval_tasks: Optional deterministic task set consumed by
                the deployment quality gate.
            cpu_tier: Cpu tier value consumed by run().
            release_timeout_s: Timeout value controlling how long the operation may wait.

        Returns:
            Value produced for the caller.

        Raises:
            RuntimeError: Propagated when validation, persistence, or execution fails.
        """
        base_model = self._resolve_base_model(base_model)
        backend = _normalize_training_backend(backend)
        model_format = _normalize_training_format(backend, model_format)
        gradient_accumulation_steps, warmup_ratio = _validate_training_schedule(
            gradient_accumulation_steps, warmup_ratio
        )
        resolved_revision = self._resolve_model_revision(base_model, model_revision)
        run_id, run_dir, run = self._new_run(
            base_model,
            task_type,
            epochs,
            gradient_accumulation_steps,
            warmup_ratio,
            output_base_dir,
            backend,
            model_format,
            resolved_revision,
        )
        release_ok = False
        if cpu_tier is not None:
            release_ok = bool(cpu_tier.request_release(reason="training_start", timeout_s=release_timeout_s))
            if not release_ok:
                raise RuntimeError("CPU tier did not drain within timeout - training aborted")

        try:
            dataset_path = self._curate_or_use_dataset(run_id, run_dir, task_type, min_score, dataset_path)
            _set_training_run_field(run, "training_examples", _count_jsonl_records(dataset_path))
            logger.info("[TrainingPipeline] %s: %d training examples", run_id, run.training_examples)
            if self._handle_insufficient_data(run, run_dir, run_id, dataset_path):
                return run
            dataset_path, eval_dataset_path = self._prepare_training_dataset(run, run_dir, run_id, dataset_path)
            self._capture_forgetting_baseline(run, run_id, base_model, eval_dataset_path, resolved_revision)
            adapter_path = self._train_adapter(
                run,
                run_id,
                base_model,
                dataset_path,
                run_dir,
                epochs,
                resolved_revision,
                eval_dataset_path,
                gradient_accumulation_steps,
                warmup_ratio,
            )
            self._check_forgetting(
                run,
                run_id,
                base_model,
                eval_dataset_path,
                resolved_revision,
                adapter_path=adapter_path,
            )
            self._run_deployment_quality_gate(
                run,
                run_id,
                base_model,
                adapter_path,
                resolved_revision,
                quality_eval_tasks,
            )
            deployed_path, task_key = self._deploy_adapter(
                run, run_id, base_model, adapter_path, run_dir, backend, model_format, resolved_revision, task_type
            )
            self._register_adapter_and_replay(run_id, task_key, adapter_path, dataset_path)
            _set_training_run_field(run, "success", True)
            logger.info("[TrainingPipeline] %s: Complete - model at %s", run_id, deployed_path)
            self._record_archive(
                run, run_id, base_model, task_key, epochs, backend, model_format, resolved_revision, deployed_path
            )
        except Exception as exc:
            _set_training_run_field(run, "error", str(exc))
            logger.error("[TrainingPipeline] %s: Pipeline failed: %s", run_id, exc)
        finally:
            self._finalize_run(run, run_dir, run_id, task_type, base_model, backend, epochs, release_ok, cpu_tier)
        return run
