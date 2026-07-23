"""Training script builders for SimPO and DAPO-reward/DPO-loss subprocess execution."""

from __future__ import annotations

import json as _json

from vetinari.security.fail_closed import sanitize_untrusted_text
from vetinari.training.loss import SIMPO_BETA_DEFAULT, SIMPO_GAMMA_DEFAULT

LORA_R_DEFAULT: int = 16
LORA_ALPHA_DEFAULT: int = 32
LORA_DROPOUT_DEFAULT: float = 0.05
DAPO_BETA_DEFAULT: float = 0.1
DAPO_LR_DEFAULT: float = 1e-6

_COMMON_DPO_SCRIPT_PREFIX = """import torch
from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig
from peft import LoraConfig, get_peft_model
from trl import DPOTrainer, DPOConfig
from datasets import Dataset, load_dataset

bnb_config = BitsAndBytesConfig(
    load_in_4bit=True,
    bnb_4bit_quant_type="nf4",
    bnb_4bit_compute_dtype=torch.bfloat16,
)

model = AutoModelForCausalLM.from_pretrained(
    {model_literal},
    revision={revision_literal},
    local_files_only={local_files_only_literal},
    quantization_config=bnb_config,
    device_map="auto",
)
tokenizer = AutoTokenizer.from_pretrained(
    {model_literal},
    revision={revision_literal},
    local_files_only={local_files_only_literal},
)
tokenizer.pad_token = tokenizer.eos_token

lora_config = LoraConfig(
    r={lora_r},
    lora_alpha={lora_alpha},
    lora_dropout={lora_dropout},
    target_modules=["q_proj", "k_proj", "v_proj", "o_proj"],
    task_type="CAUSAL_LM",
    use_dora=True,
)
model = get_peft_model(model, lora_config)

dataset = load_dataset("json", data_files={dataset_path_literal}, split="train")
"""

_SIMPO_TRAINER_TEMPLATE = """
trainer = DPOTrainer(
    model=model,
    processing_class=tokenizer,
    train_dataset=dataset,
    args=DPOConfig(
        output_dir={output_dir_literal},
        loss_type="simpo",
        beta={beta_literal!r},
        simpo_gamma={simpo_gamma_literal!r},
        num_train_epochs={epochs},
        per_device_train_batch_size=1,
        learning_rate={lr},
        logging_steps=10,
        save_strategy="epoch",
    ),
)
trainer.train()
model.save_pretrained({adapter_dir_literal})
"""

_DAPO_REWARD_DPO_TRAINER_TEMPLATE = """
def _expand_reward_weighted_preferences(source_dataset):
    expanded_rows = []
    for row in source_dataset:
        chosen_reward = float(row.get("chosen_reward", 0.0) or 0.0)
        rejected_reward = float(row.get("rejected_reward", 0.0) or 0.0)
        reward_gap = max(0.0, chosen_reward - rejected_reward)
        repeats = max(1, min(5, 1 + int(round(reward_gap * 4))))
        for _ in range(repeats):
            expanded_rows.append(row)
    return Dataset.from_list(expanded_rows) if expanded_rows else source_dataset

dataset = _expand_reward_weighted_preferences(dataset)

trainer = DPOTrainer(
    model=model,
    processing_class=tokenizer,
    train_dataset=dataset,
    args=DPOConfig(
        output_dir={output_dir_literal},
        beta={beta},
        num_train_epochs={epochs},
        per_device_train_batch_size=1,
        learning_rate={lr},
        logging_steps=10,
        save_strategy="epoch",
    ),
)
trainer.train()
model.save_pretrained({adapter_dir_literal})
"""


def _revision_literal(model_revision: str | None) -> str:
    return _json.dumps(str(model_revision)) if model_revision else "None"


def _local_files_only_literal(model_revision: str | None) -> str:
    return "False" if model_revision else "True"


def _script_literals(model: str, dataset_path: str, output_dir: str, model_revision: str | None) -> dict[str, str]:
    safe_model = sanitize_untrusted_text(model, max_length=2048)
    safe_dataset_path = sanitize_untrusted_text(dataset_path, max_length=4096)
    safe_output_dir = sanitize_untrusted_text(output_dir, max_length=4096)
    safe_revision = sanitize_untrusted_text(model_revision, max_length=512) if model_revision else None
    return {
        "model_literal": _json.dumps(safe_model),
        "revision_literal": _revision_literal(safe_revision),
        "local_files_only_literal": _local_files_only_literal(safe_revision),
        "dataset_path_literal": _json.dumps(safe_dataset_path),
        "output_dir_literal": _json.dumps(safe_output_dir),
        "adapter_dir_literal": _json.dumps(safe_output_dir + "/lora_adapter"),
    }


def build_simpo_script(
    model: str,
    dpo_path: str,
    output_dir: str,
    epochs: int = 1,
    model_revision: str | None = None,
    beta: float = SIMPO_BETA_DEFAULT,
    simpo_gamma: float = SIMPO_GAMMA_DEFAULT,
    lora_r: int = LORA_R_DEFAULT,
    lora_alpha: int = LORA_ALPHA_DEFAULT,
    lora_dropout: float = LORA_DROPOUT_DEFAULT,
    lr: float = DAPO_LR_DEFAULT,
) -> str:
    """Return a Python script string for SimPO reference-free alignment training.

    Args:
        model: Model value consumed by build_simpo_script().
        dpo_path: Filesystem path read or written by the operation.
        output_dir: Output dir value consumed by build_simpo_script().
        epochs: Epochs value consumed by build_simpo_script().
        model_revision: Model revision value consumed by build_simpo_script().
        beta: Beta value consumed by build_simpo_script().
        simpo_gamma: Simpo gamma value consumed by build_simpo_script().
        lora_r: LoRA rank interpolated into the subprocess script.
        lora_alpha: LoRA alpha interpolated into the subprocess script.
        lora_dropout: LoRA dropout interpolated into the subprocess script.
        lr: Learning rate value reserved for caller parity with DAPO builders.

    Returns:
        Value produced for the caller.
    """
    literals = _script_literals(model, dpo_path, output_dir, model_revision)
    prefix = _COMMON_DPO_SCRIPT_PREFIX.format(
        **literals,
        lora_r=int(lora_r),
        lora_alpha=int(lora_alpha),
        lora_dropout=float(lora_dropout),
    )
    return prefix + _SIMPO_TRAINER_TEMPLATE.format(
        **literals,
        beta_literal=float(beta),
        simpo_gamma_literal=float(simpo_gamma),
        lr=float(lr),
        epochs=int(epochs),
    )


def build_dapo_reward_dpo_script(
    model: str,
    dapo_dataset_path: str,
    output_dir: str,
    epochs: int = 1,
    model_revision: str | None = None,
    lora_r: int = LORA_R_DEFAULT,
    lora_alpha: int = LORA_ALPHA_DEFAULT,
    lora_dropout: float = LORA_DROPOUT_DEFAULT,
    beta: float = DAPO_BETA_DEFAULT,
    lr: float = DAPO_LR_DEFAULT,
) -> str:
    """Return a Python script string for DAPO-reward-weighted DPO training.

    Args:
        model: Model value consumed by build_dapo_reward_dpo_script().
        dapo_dataset_path: Filesystem path read or written by the operation.
        output_dir: Output dir value consumed by build_dapo_reward_dpo_script().
        epochs: Epochs value consumed by build_dapo_reward_dpo_script().
        model_revision: Model revision value consumed by build_dapo_reward_dpo_script().
        lora_r: LoRA rank interpolated into the subprocess script.
        lora_alpha: LoRA alpha interpolated into the subprocess script.
        lora_dropout: LoRA dropout interpolated into the subprocess script.
        beta: DAPO beta interpolated into the subprocess script.
        lr: DAPO learning rate interpolated into the subprocess script.

    Returns:
        Value produced for the caller.
    """
    literals = _script_literals(model, dapo_dataset_path, output_dir, model_revision)
    prefix = _COMMON_DPO_SCRIPT_PREFIX.format(
        **literals,
        lora_r=int(lora_r),
        lora_alpha=int(lora_alpha),
        lora_dropout=float(lora_dropout),
    )
    return prefix + _DAPO_REWARD_DPO_TRAINER_TEMPLATE.format(
        **literals,
        beta=float(beta),
        lr=float(lr),
        epochs=int(epochs),
    )
