"""SFTConfig construction adapter for TRL versioned keyword mapping."""

from __future__ import annotations

import inspect
import logging
from typing import Any

logger = logging.getLogger(__name__)


def build_sft_config(
    output_dir: str,
    epochs: int = 3,
    batch_size: int = 4,
    learning_rate: float = 2e-4,
    max_seq_length: int = 2048,
    gradient_accumulation_steps: int = 1,
    warmup_ratio: float = 0.0,
    lr_scheduler_type: str = "linear",
    logging_steps: int = 10,
    save_strategy: str = "epoch",
    eval_strategy: str = "no",
    load_best_model_at_end: bool = False,
    metric_for_best_model: str = "eval_loss",
    greater_is_better: bool = False,
    report_to: str | list[str] | None = "none",
) -> Any:
    """Construct a TRL SFTConfig with stable caller-facing parameter names.

    TRL 0.23 renamed the old ``max_seq_length`` setting to ``max_length``.
    Keeping that translation here gives training callers one fix point if TRL
    changes the constructor again.

    Args:
        output_dir: Directory where TRL writes checkpoints and trainer state.
        epochs: Number of training epochs.
        batch_size: Per-device training batch size.
        learning_rate: Trainer learning rate.
        max_seq_length: Maximum sequence length exposed to local callers.
        gradient_accumulation_steps: Gradient accumulation steps.
        warmup_ratio: Learning-rate warmup ratio.
        lr_scheduler_type: Scheduler name passed to Transformers.
        logging_steps: Step interval for trainer logging.
        save_strategy: TRL checkpoint save strategy.
        eval_strategy: Evaluation cadence, usually ``"epoch"`` when an
            eval_dataset is supplied to SFTTrainer.
        load_best_model_at_end: Whether SFT should restore the best checkpoint.
        metric_for_best_model: Metric used when selecting the best checkpoint.
        greater_is_better: Whether higher metric values are better.
        report_to: Reporting integrations passed to Transformers. Defaults to
            ``"none"`` so config construction does not auto-import optional
            native reporting stacks during smoke tests or local setup probes.

    Returns:
        A configured ``trl.SFTConfig`` instance.

    Raises:
        ImportError: If TRL is not installed in the active environment.
    """
    try:
        from trl import SFTConfig
    except ImportError as exc:
        raise ImportError("trl is required for SFTConfig construction; install 'trl>=0.23,<0.24'") from exc

    eval_strategy_key = "eval_strategy"
    if "eval_strategy" not in inspect.signature(SFTConfig).parameters:
        eval_strategy_key = "evaluation_strategy"

    return SFTConfig(
        output_dir=output_dir,
        num_train_epochs=epochs,
        per_device_train_batch_size=batch_size,
        learning_rate=learning_rate,
        max_length=max_seq_length,
        gradient_accumulation_steps=gradient_accumulation_steps,
        warmup_ratio=warmup_ratio,
        lr_scheduler_type=lr_scheduler_type,
        logging_steps=logging_steps,
        save_strategy=save_strategy,
        load_best_model_at_end=load_best_model_at_end,
        metric_for_best_model=metric_for_best_model,
        greater_is_better=greater_is_better,
        report_to=report_to,
        **{eval_strategy_key: eval_strategy},
    )
