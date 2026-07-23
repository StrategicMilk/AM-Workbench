"""Build subprocess scripts for unsloth-backed QLoRA training."""

from __future__ import annotations

import json as _json
import logging

logger = logging.getLogger(__name__)

UNSLOTH_LORA_ALPHA_DEFAULT: int = 32
UNSLOTH_LORA_DROPOUT_DEFAULT: float = 0.05

_UNSLOTH_SCRIPT_TEMPLATE = """from unsloth import FastLanguageModel
from datasets import load_dataset
from trl import SFTTrainer, SFTConfig
import torch

model, tokenizer = FastLanguageModel.from_pretrained(
    model_name={base_model_literal},
    revision={revision_literal},
    local_files_only={local_files_only_literal},
    max_seq_length={max_seq_len},
    dtype=None,
    load_in_4bit=True,
)

model = FastLanguageModel.get_peft_model(
    model,
    r={lora_r},
    target_modules=["q_proj", "k_proj", "v_proj", "o_proj",
                    "gate_proj", "up_proj", "down_proj"],
    lora_alpha={lora_alpha},
    lora_dropout={lora_dropout},
    bias="none",
    use_gradient_checkpointing="unsloth",
    random_state=42,
)

dataset = load_dataset("json", data_files={dataset_path_literal}, split="train")
eval_dataset = load_dataset("json", data_files={eval_dataset_literal}, split="train") if {eval_dataset_literal} else None

trainer = SFTTrainer(
    model=model,
    processing_class=tokenizer,
    train_dataset=dataset,
    eval_dataset=eval_dataset,
    args=SFTConfig(
        output_dir={output_dir_literal},
        num_train_epochs={epochs},
        per_device_train_batch_size={batch_size},
        learning_rate={lr},
        max_length={max_seq_len},
        fp16=not torch.cuda.is_bf16_supported(),
        bf16=torch.cuda.is_bf16_supported(),
        gradient_accumulation_steps={gradient_accumulation_steps},
        warmup_ratio={warmup_ratio},
        lr_scheduler_type="linear",
        logging_steps=10,
        save_strategy="epoch",
        eval_strategy={eval_strategy_literal},
        load_best_model_at_end={load_best_literal},
        metric_for_best_model="eval_loss",
        greater_is_better=False,
        report_to="none",
        optim="adamw_8bit",
    ),
)
trainer.train()
model.save_pretrained({adapter_dir_literal})
print("Training complete:", {adapter_dir_literal})
"""


def _revision_literal(model_revision: str | None) -> str:
    """Return a JSON-serialized revision string or a Python None literal."""
    return _json.dumps(str(model_revision)) if model_revision else "None"


def _local_files_only_literal(model_revision: str | None) -> str:
    """Return the local-files-only literal for generated model loading code."""
    return "False" if model_revision else "True"


def build_unsloth_script(
    base_model: str,
    dataset_path: str,
    output_dir: str,
    eval_dataset_path: str | None = None,
    epochs: int = 3,
    batch_size: int = 4,
    lr: float = 2e-4,
    max_seq_len: int = 2048,
    lora_r: int = 16,
    lora_alpha: int = UNSLOTH_LORA_ALPHA_DEFAULT,
    lora_dropout: float = UNSLOTH_LORA_DROPOUT_DEFAULT,
    model_revision: str | None = None,
    gradient_accumulation_steps: int = 1,
    warmup_ratio: float = 0.0,
) -> str:
    """Return a TRL 0.23-compatible unsloth QLoRA training script.

    Args:
        base_model: Base model identifier.
        dataset_path: Training dataset path.
        output_dir: Output adapter directory.
        eval_dataset_path: Optional eval dataset path.
        epochs: Training epochs.
        batch_size: Per-device batch size.
        lr: Learning rate.
        max_seq_len: Maximum sequence length.
        lora_r: LoRA rank.
        lora_alpha: LoRA alpha.
        lora_dropout: LoRA dropout.
        model_revision: Optional immutable model revision.
        gradient_accumulation_steps: Gradient accumulation steps.
        warmup_ratio: Learning-rate warmup ratio.

    Returns:
        Complete Python training script.

    Raises:
        ValueError: If ``eval_dataset_path`` is blank.
    """
    if eval_dataset_path is not None and not str(eval_dataset_path).strip():
        raise ValueError("eval_dataset_path must be None or a non-empty path string")
    if int(gradient_accumulation_steps) < 1:
        raise ValueError("gradient_accumulation_steps must be >= 1")
    if not 0.0 <= float(warmup_ratio) <= 1.0:
        raise ValueError("warmup_ratio must be between 0.0 and 1.0")

    return _UNSLOTH_SCRIPT_TEMPLATE.format(
        base_model_literal=_json.dumps(str(base_model)),
        revision_literal=_revision_literal(model_revision),
        local_files_only_literal=_local_files_only_literal(model_revision),
        dataset_path_literal=_json.dumps(str(dataset_path)),
        output_dir_literal=_json.dumps(str(output_dir)),
        adapter_dir_literal=_json.dumps(str(output_dir) + "/lora_adapter"),
        eval_dataset_literal=_json.dumps(str(eval_dataset_path)) if eval_dataset_path else "None",
        eval_strategy_literal=_json.dumps("epoch" if eval_dataset_path else "no"),
        load_best_literal="True" if eval_dataset_path else "False",
        epochs=int(epochs),
        batch_size=int(batch_size),
        lr=float(lr),
        max_seq_len=int(max_seq_len),
        lora_r=int(lora_r),
        lora_alpha=int(lora_alpha),
        lora_dropout=float(lora_dropout),
        gradient_accumulation_steps=int(gradient_accumulation_steps),
        warmup_ratio=float(warmup_ratio),
    )
