"""STABLE regularization for Vetinari continual learning.

This module handles forward-pass forgetting detection for the training
pipeline: it captures baseline model metrics, computes per-layer LoRA gates,
and exposes stop-training signals when drift becomes severe.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass
from importlib.util import find_spec
from pathlib import Path
from typing import Any

from vetinari.training.continual_learning_persistence import _require_immutable_model_revision

logger = logging.getLogger(__name__)


# Forgetting risk multiplier: gates halved when global metrics breach thresholds.
GLOBAL_BREACH_GATE_FACTOR = 0.5

# Severe forgetting threshold multiplier for early stop.
STOP_TRAINING_THRESHOLD_MULTIPLIER = 2.0


@dataclass(frozen=True, slots=True)
class _ForwardMetrics:
    """Aggregated forward-pass metrics for STABLE drift checks."""

    average_loss: float
    average_kl: float
    layer_norms: dict[str, list[float]]
    sample_count: int
    log_distribution: Any | None

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}("
            f"average_loss={self.average_loss!r}, "
            f"average_kl={self.average_kl!r}, "
            f"layer_norms={self.layer_norms!r}, "
            f"sample_count={self.sample_count!r}, "
            f"has_log_distribution={self.log_distribution is not None!r}"
            ")"
        )


def _same_model_identity(expected: str, actual: str) -> bool:
    expected_path = Path(expected)
    actual_path = Path(actual)
    if expected_path.exists() and actual_path.exists():
        return expected_path.resolve() == actual_path.resolve()
    return expected.strip().rstrip("/") == actual.strip().rstrip("/")


def _validated_adapter_root(model_path: str, adapter_path: str) -> Path:
    """Return a local PEFT adapter only when its base-model provenance matches."""
    adapter_root = Path(adapter_path).resolve()
    config_path = adapter_root / "adapter_config.json"
    if not adapter_root.is_dir() or not config_path.is_file():
        msg = f"trained adapter is missing its PEFT configuration: {adapter_root}"
        raise ValueError(msg)
    try:
        adapter_config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        msg = f"trained adapter configuration is unreadable: {config_path}"
        raise ValueError(msg) from exc
    configured_base = adapter_config.get("base_model_name_or_path")
    if not isinstance(configured_base, str) or not _same_model_identity(model_path, configured_base):
        msg = f"trained adapter base model does not match {model_path!r}"
        raise ValueError(msg)
    return adapter_root


def _load_metric_model(
    model_path: str,
    model_revision: str | None,
    *,
    adapter_path: str | None = None,
) -> tuple[Any, Any, Any] | None:
    """Load tokenizer/model dependencies needed for STABLE metric collection."""
    adapter_root = _validated_adapter_root(model_path, adapter_path) if adapter_path is not None else None
    if find_spec("torch") is None or find_spec("transformers") is None:
        logger.warning(
            "torch or transformers not available; STABLERegularizer metric "
            "collection skipped. Install optional [training] dependencies.",
        )
        return None
    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer

    model_path_obj = Path(model_path)
    if model_path_obj.exists():
        tokenizer = AutoTokenizer.from_pretrained(
            model_path,
            revision=model_revision,
            local_files_only=True,
        )
        model = AutoModelForCausalLM.from_pretrained(
            model_path,
            output_hidden_states=True,
            dtype=torch.float32,
            revision=model_revision,
            local_files_only=True,
        )
    else:
        resolved_revision = _require_immutable_model_revision(model_revision)
        tokenizer = AutoTokenizer.from_pretrained(
            model_path,
            revision=resolved_revision,
        )
        model = AutoModelForCausalLM.from_pretrained(
            model_path,
            output_hidden_states=True,
            dtype=torch.float32,
            revision=resolved_revision,
        )
    if adapter_root is not None:
        if find_spec("peft") is None:
            msg = "peft is required to measure the trained adapter distribution"
            raise RuntimeError(msg)
        from peft import PeftModel

        model = PeftModel.from_pretrained(
            model,
            adapter_root,
            is_trainable=False,
            local_files_only=True,
        )
    model.eval()
    return tokenizer, model, torch


def _aggregate_log_distribution(torch_module: Any, logits: Any) -> Any:
    """Return a normalized mean token distribution in log space on CPU."""
    log_probabilities = torch_module.log_softmax(logits.float(), dim=-1)
    flattened = log_probabilities.reshape(-1, log_probabilities.shape[-1])
    if flattened.shape[0] == 0:
        msg = "model produced no token distributions"
        raise ValueError(msg)
    aggregate = torch_module.logsumexp(flattened, dim=0) - math.log(flattened.shape[0])
    return aggregate.detach().to(device="cpu", dtype=torch_module.float64)


def _reference_kl_divergence(torch_module: Any, baseline_log_probs: Any, current_log_probs: Any) -> float:
    """Compute ``D_KL(P_baseline || P_current)`` with stable normalization."""
    if baseline_log_probs.shape != current_log_probs.shape:
        msg = "baseline and current output distributions have different vocabularies"
        raise ValueError(msg)
    baseline = baseline_log_probs - torch_module.logsumexp(baseline_log_probs, dim=-1)
    current = current_log_probs - torch_module.logsumexp(current_log_probs, dim=-1)
    divergence = torch_module.sum(torch_module.exp(baseline) * (baseline - current))
    if not bool(torch_module.isfinite(divergence)):
        msg = "output-distribution KL is not finite"
        raise ValueError(msg)
    return max(0.0, float(divergence.item()))


def _reference_kl_divergence_values(baseline_values: list[float], current_values: list[float]) -> float:
    """Scalar reference for ``D_KL(P_baseline || P_current)`` used by controls."""
    if not baseline_values or len(baseline_values) != len(current_values):
        msg = "baseline and current output distributions must have the same nonzero size"
        raise ValueError(msg)

    def _log_normalize(values: list[float]) -> list[float]:
        maximum = max(values)
        log_total = maximum + math.log(math.fsum(math.exp(value - maximum) for value in values))
        return [value - log_total for value in values]

    baseline = _log_normalize(baseline_values)
    current = _log_normalize(current_values)
    divergence = math.fsum(
        math.exp(reference) * (reference - observed) for reference, observed in zip(baseline, current, strict=True)
    )
    if not math.isfinite(divergence):
        msg = "output-distribution KL is not finite"
        raise ValueError(msg)
    return max(0.0, divergence)


def _collect_forward_metrics(
    torch_module: Any,
    tokenizer: Any,
    model: Any,
    val_texts: list[str],
    *,
    reference_log_distribution: Any | None = None,
) -> _ForwardMetrics:
    """Run bounded forward passes and aggregate loss, KL, and layer norms."""
    total_loss = 0.0
    sample_count = 0
    layer_norm_accum: dict[str, list[float]] = {}
    log_distribution_accumulator: Any | None = None

    with torch_module.no_grad():
        for text in val_texts[:100]:
            inputs = tokenizer(text, return_tensors="pt", truncation=True, max_length=512)
            outputs = model(**inputs, labels=inputs["input_ids"])
            total_loss += outputs.loss.item()
            sample_count += 1

            sample_log_distribution = _aggregate_log_distribution(torch_module, outputs.logits)
            if log_distribution_accumulator is None:
                log_distribution_accumulator = sample_log_distribution
            else:
                log_distribution_accumulator = torch_module.logaddexp(
                    log_distribution_accumulator,
                    sample_log_distribution,
                )

            if outputs.hidden_states:
                for idx, hidden_state in enumerate(outputs.hidden_states):
                    key = f"layer.{idx}"
                    norm = hidden_state.norm(dim=-1).mean().item()
                    layer_norm_accum.setdefault(key, []).append(norm)

    if sample_count == 0:
        return _ForwardMetrics(
            average_loss=0.0,
            average_kl=0.0,
            layer_norms={},
            sample_count=0,
            log_distribution=None,
        )

    if log_distribution_accumulator is None:
        msg = "model produced no output distribution"
        raise ValueError(msg)
    log_distribution = log_distribution_accumulator - math.log(sample_count)
    average_kl = (
        _reference_kl_divergence(torch_module, reference_log_distribution, log_distribution)
        if reference_log_distribution is not None
        else 0.0
    )
    return _ForwardMetrics(
        average_loss=total_loss / sample_count,
        average_kl=average_kl,
        layer_norms=layer_norm_accum,
        sample_count=sample_count,
        log_distribution=log_distribution,
    )


def _average_layer_norms(layer_norm_accum: dict[str, list[float]]) -> dict[str, float]:
    """Average accumulated hidden-state norms by layer."""
    return {layer_key: sum(norms) / len(norms) for layer_key, norms in layer_norm_accum.items()}


class STABLERegularizer:
    """Threshold-based LoRA gating for continual learning.

    Uses forward-pass-only metrics (no gradient computation needed) to detect
    catastrophic forgetting risk during fine-tuning, then gates LoRA updates
    per-layer to protect previously learned capabilities.

    Metrics monitored:
    - EM drop: per-layer embedding drift (L2 norm change from baseline)
    - KL divergence: output distribution shift on validation data
    - Bits increase: cross-entropy increase (nats to bits) on validation data

    All metrics are computed via forward passes only, making this suitable for
    monitoring during training without disrupting gradient flow.

    Example:
        regularizer = STABLERegularizer(em_threshold=0.15)
        regularizer.capture_baseline("path/to/model", "path/to/val.jsonl")
        gates = regularizer.compute_layer_gates("path/to/model", "path/to/val.jsonl")
    """

    def __init__(
        self,
        em_threshold: float = 0.15,
        kl_threshold: float = 0.5,
        bits_threshold: float = 0.3,
    ) -> None:
        """Initialise the regularizer with configurable forgetting thresholds.

        Args:
            em_threshold: Maximum tolerated L2 norm change per embedding layer
                before gating is applied.
            kl_threshold: Maximum tolerated KL divergence on validation data
                before all layer gates are halved.
            bits_threshold: Maximum tolerated increase in bits-per-token on
                validation data before all layer gates are halved.
        """
        self.em_threshold = em_threshold
        self.kl_threshold = kl_threshold
        self.bits_threshold = bits_threshold

        self._baseline_embeddings: dict[str, float] = {}
        self._baseline_loss: float = 0.0
        self._baseline_log_distribution: Any | None = None
        self._baseline_captured: bool = False

        self._current_kl: float = 0.0
        self._current_bits_increase: float = 0.0
        self._current_em_drops: dict[str, float] = {}
        self._measurement_available: bool = False

    def capture_baseline(
        self,
        model_path: str,
        validation_data_path: str,
        model_revision: str | None = None,
    ) -> bool:
        """Capture baseline metrics before training on a new task.

        Runs a forward pass on the validation set to record embedding norms and
        loss. Must be called before fine-tuning begins so that drift can be
        measured relative to the pre-training state.

        Args:
            model_path: Path or Hugging Face model identifier for the base model.
            validation_data_path: Path to JSONL validation examples with
                ``text`` or ``prompt`` plus ``completion`` fields.
            model_revision: Immutable revision for remote Hugging Face loads.

        Returns:
            True if baseline capture succeeds, or False when optional training
            dependencies or validation examples are unavailable.
        """
        metric_model = _load_metric_model(model_path, model_revision)
        if metric_model is None:
            return False
        logger.info("Capturing STABLE baseline from model=%s", model_path)
        tokenizer, model, torch_module = metric_model

        val_texts = self._load_validation_texts(validation_data_path)
        if not val_texts:
            logger.warning("No validation texts loaded from %s", validation_data_path)
            return False

        metrics = _collect_forward_metrics(torch_module, tokenizer, model, val_texts)
        if metrics.sample_count == 0 or metrics.log_distribution is None:
            return False

        self._baseline_loss = metrics.average_loss
        self._baseline_embeddings = _average_layer_norms(metrics.layer_norms)
        self._baseline_log_distribution = metrics.log_distribution
        self._baseline_captured = True

        logger.info(
            "Baseline captured: loss=%.4f, layers=%d",
            self._baseline_loss,
            len(self._baseline_embeddings),
        )
        return True

    def compute_layer_gates(
        self,
        model_path: str,
        validation_data_path: str,
        model_revision: str | None = None,
        adapter_path: str | None = None,
    ) -> dict[str, float]:
        """Compute per-layer gating factors to control LoRA update magnitude.

        A gate of 1.0 means full updates are allowed; 0.0 means the layer is
        frozen. Gates are halved globally if KL divergence or bits increase
        exceeds their respective thresholds.

        Args:
            model_path: Path or Hugging Face identifier for the current model.
            validation_data_path: Path to JSONL validation data.
            model_revision: Immutable revision for remote Hugging Face loads.
            adapter_path: Optional trained PEFT adapter whose output distribution
                is compared with the captured baseline distribution.

        Returns:
            Dictionary mapping layer name to gate factor in ``[0.0, 1.0]``.
            Returns an empty dictionary if baseline capture has not run or
            optional training dependencies are unavailable.

        Raises:
            RuntimeError: If the captured baseline or required adapter
                dependency is unavailable.
            ValueError: If trained-adapter provenance does not match the
                requested base model.
        """
        self._measurement_available = False
        if not self._baseline_captured:
            logger.warning(
                "compute_layer_gates called before capture_baseline; returning "
                "empty gates. Call capture_baseline() first.",
            )
            return {}

        if self._baseline_log_distribution is None:
            msg = "baseline output distribution is unavailable"
            raise RuntimeError(msg)
        metric_model = _load_metric_model(model_path, model_revision, adapter_path=adapter_path)
        if metric_model is None:
            return {}
        tokenizer, model, torch_module = metric_model

        val_texts = self._load_validation_texts(validation_data_path)
        if not val_texts:
            return {}

        metrics = _collect_forward_metrics(
            torch_module,
            tokenizer,
            model,
            val_texts,
            reference_log_distribution=self._baseline_log_distribution,
        )
        if metrics.sample_count == 0:
            return {}

        bits_increase = (metrics.average_loss - self._baseline_loss) / math.log(2)
        self._current_kl = metrics.average_kl
        self._current_bits_increase = bits_increase
        self._measurement_available = True

        gates = self._compute_embedding_gates(metrics.layer_norms)

        global_breach = self._current_bits_increase > self.bits_threshold or self._current_kl > self.kl_threshold
        if global_breach:
            logger.warning(
                "Global forgetting breach detected; kl=%.4f (thresh=%.4f), "
                "bits_increase=%.4f (thresh=%.4f). Halving all layer gates.",
                self._current_kl,
                self.kl_threshold,
                self._current_bits_increase,
                self.bits_threshold,
            )
            gates = {key: value * GLOBAL_BREACH_GATE_FACTOR for key, value in gates.items()}

        logger.info(
            "Layer gates computed: %d layers, global_breach=%s",
            len(gates),
            global_breach,
        )
        return gates

    def should_stop_training(
        self,
        model_path: str,
        validation_data_path: str,
        model_revision: str | None = None,
        adapter_path: str | None = None,
    ) -> bool:
        """Check whether training should stop due to severe forgetting.

        Args:
            model_path: Path or Hugging Face identifier for the current model.
            validation_data_path: Path to JSONL validation data.
            model_revision: Immutable revision for remote Hugging Face loads.
            adapter_path: Optional trained PEFT adapter to measure against the
                untouched pre-training baseline.

        Returns:
            True if severe forgetting is detected and training should stop,
            otherwise False.

        Raises:
            RuntimeError: If the captured baseline or required adapter
                dependency is unavailable.
            ValueError: If trained-adapter provenance does not match the
                requested base model.
        """
        self._measurement_available = False
        if adapter_path is None:
            self.compute_layer_gates(model_path, validation_data_path, model_revision=model_revision)
        else:
            self.compute_layer_gates(
                model_path,
                validation_data_path,
                model_revision=model_revision,
                adapter_path=adapter_path,
            )
        if not self._measurement_available:
            msg = "forgetting metrics are unavailable; deployment must fail closed"
            raise RuntimeError(msg)

        severe_kl = self._current_kl > self.kl_threshold * STOP_TRAINING_THRESHOLD_MULTIPLIER
        severe_bits = self._current_bits_increase > self.bits_threshold * STOP_TRAINING_THRESHOLD_MULTIPLIER
        severe_em = any(
            drop > self.em_threshold * STOP_TRAINING_THRESHOLD_MULTIPLIER for drop in self._current_em_drops.values()
        )

        stop = severe_kl or severe_bits or severe_em
        if stop:
            logger.warning(
                "Severe forgetting detected; stopping training. kl=%.4f, bits_increase=%.4f, max_em_drop=%.4f",
                self._current_kl,
                self._current_bits_increase,
                max(self._current_em_drops.values(), default=0.0),
            )
        return stop

    def get_metrics(self) -> dict[str, Any]:
        """Return current metric values and configured thresholds.

        Returns:
            Dictionary containing baseline status, latest metric values, and
            threshold settings.
        """
        return {
            "baseline_captured": self._baseline_captured,
            "current_kl": self._current_kl,
            "current_bits_increase": self._current_bits_increase,
            "current_em_drops": dict(self._current_em_drops),
            "thresholds": {
                "em": self.em_threshold,
                "kl": self.kl_threshold,
                "bits": self.bits_threshold,
            },
        }

    @staticmethod
    def _load_validation_texts(validation_data_path: str) -> list[str]:
        """Load plain text strings from a JSONL validation file.

        Args:
            validation_data_path: Path to JSONL validation data.

        Returns:
            List of validation text strings. Returns an empty list when the
            file is missing or no valid records are present.
        """
        path = Path(validation_data_path)
        if not path.exists():
            logger.warning("Validation data path does not exist: %s", path)
            return []

        texts: list[str] = []
        with Path(path).open(encoding="utf-8") as file:
            for line in file:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                    if "text" in record:
                        texts.append(record["text"])
                    elif "prompt" in record and "completion" in record:
                        texts.append(record["prompt"] + record["completion"])
                except json.JSONDecodeError:
                    logger.warning("Skipping malformed JSONL line in validation data")
        return texts

    def _compute_embedding_gates(self, current_norms: dict[str, list[float]]) -> dict[str, float]:
        """Convert current embedding norms into per-layer LoRA gate values."""
        gates: dict[str, float] = {}
        for layer_key, norms in current_norms.items():
            current_norm = sum(norms) / len(norms)
            baseline_norm = self._baseline_embeddings.get(layer_key, current_norm)
            if baseline_norm > 0:
                em_drop = abs(current_norm - baseline_norm) / baseline_norm
            else:
                em_drop = 0.0
            self._current_em_drops[layer_key] = em_drop

            if em_drop >= self.em_threshold:
                gate = max(0.0, 1.0 - (em_drop / (2.0 * self.em_threshold)))
            else:
                gate = 1.0
            gates[layer_key] = gate
        return gates


__all__ = [
    "GLOBAL_BREACH_GATE_FACTOR",
    "STOP_TRAINING_THRESHOLD_MULTIPLIER",
    "STABLERegularizer",
]
