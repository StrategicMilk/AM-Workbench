"""Training backend classes: LocalTrainer, GGUFConverter, ModelDeployer.

These classes handle the compute-intensive stages of the training pipeline:
QLoRA fine-tuning (via unsloth or trl), GGUF conversion, and deploying the
converted model to the local models directory.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType

from vetinari.constants import OPERATOR_MODELS_CACHE_DIR
from vetinari.learning import atomic_writers
from vetinari.training.pipeline_deployer import (
    ModelDeployer as ModelDeployer,
)

logger = logging.getLogger(__name__)


_MODELS_DIR = Path(OPERATOR_MODELS_CACHE_DIR)

_NATIVE_MODELS_DIR = Path(OPERATOR_MODELS_CACHE_DIR) / "native"

_MISSING_MODULE = object()
_TRAINING_SUBPROCESS_TIMEOUT_SECONDS = 12 * 60 * 60
_CONVERSION_SUBPROCESS_TIMEOUT_SECONDS = 2 * 60 * 60


def _revision_literal(model_revision: str | None) -> str:
    return json.dumps(str(model_revision)) if model_revision else "None"


def _local_files_only_literal(model_revision: str | None) -> str:
    return "False" if model_revision else "True"


def _validate_training_schedule(gradient_accumulation_steps: int, warmup_ratio: float) -> tuple[int, float]:
    """Validate caller-facing SFT schedule parameters before script generation."""
    accumulation = int(gradient_accumulation_steps)
    warmup = float(warmup_ratio)
    if accumulation < 1:
        raise ValueError("gradient_accumulation_steps must be >= 1")
    if not 0.0 <= warmup <= 1.0:
        raise ValueError("warmup_ratio must be between 0.0 and 1.0")
    return accumulation, warmup


def _is_module_available(module_name: str) -> bool:
    """Return True when a real module is already loaded or discoverable without importing it."""
    existing = sys.modules.get(module_name, _MISSING_MODULE)
    if existing is None:
        return False
    if isinstance(existing, ModuleType):
        spec = getattr(existing, "__spec__", None)
        if spec is None:
            return False
        return (
            getattr(spec, "loader", None) is not None or getattr(spec, "submodule_search_locations", None) is not None
        )
    if existing is not _MISSING_MODULE:
        return False
    try:
        return importlib.util.find_spec(module_name) is not None
    except (ImportError, AttributeError, ValueError):
        logger.debug("Module discovery failed for %s", module_name, exc_info=True)
        return False


def _cleanup_tmp_artifacts(output_dir: str | Path) -> list[Path]:
    """Remove temporary artifacts left inside a training output directory.

    Args:
        output_dir: Training output directory whose temporary children should
            be removed.

    Returns:
        Paths that were removed.
    """
    root = Path(output_dir).resolve()
    if not root.exists():
        return []
    removed: list[Path] = []
    for tmp_path in sorted(root.rglob("*.tmp")):
        resolved = tmp_path.resolve()
        if root not in resolved.parents and resolved != root:
            logger.warning("Skipping temp cleanup outside output dir: %s", tmp_path)
            continue
        try:
            if tmp_path.is_dir():
                shutil.rmtree(tmp_path)
            else:
                tmp_path.unlink()
            removed.append(tmp_path)
        except OSError:
            logger.warning("Could not remove temporary training artifact: %s", tmp_path, exc_info=True)
    return removed


def _run_training_subprocess(
    cmd: list[str],
    *,
    timeout_seconds: int = _TRAINING_SUBPROCESS_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[str]:
    """Run a training subprocess with the standard bounded wait.

    Args:
        cmd: Command vector to execute.
        timeout_seconds: Maximum seconds to wait.

    Returns:
        Completed subprocess result.

    Raises:
        subprocess.TimeoutExpired: If the command exceeds the timeout.
    """
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
    )


def _build_trl_script(
    *,
    base_model,
    dataset_path,
    output_dir,
    epochs,
    batch_size,
    lr,
    max_seq_len,
    lora_r,
    model_revision=None,
    eval_dataset_path=None,
    gradient_accumulation_steps=1,
    warmup_ratio=0.0,
) -> str:
    """Build the standard TRL training script."""
    import json as _json

    accumulation, warmup = _validate_training_schedule(gradient_accumulation_steps, warmup_ratio)
    base_model_literal = _json.dumps(str(base_model))
    revision_literal = _revision_literal(model_revision)
    local_files_only_literal = _local_files_only_literal(model_revision)
    eval_dataset_literal = _json.dumps(str(eval_dataset_path)) if eval_dataset_path else "None"
    eval_strategy_literal = _json.dumps("epoch" if eval_dataset_path else "no")
    load_best_literal = "True" if eval_dataset_path else "False"
    return f"""
from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig
from peft import LoraConfig, get_peft_model
from trl import SFTTrainer, SFTConfig
from datasets import load_dataset
import torch

bnb_config = BitsAndBytesConfig(load_in_4bit=True, bnb_4bit_quant_type="nf4", bnb_4bit_compute_dtype=torch.bfloat16)
model = AutoModelForCausalLM.from_pretrained(
    {base_model_literal},
    revision={revision_literal},
    local_files_only={local_files_only_literal},
    quantization_config=bnb_config,
    device_map="auto",
)
tokenizer = AutoTokenizer.from_pretrained(
    {base_model_literal},
    revision={revision_literal},
    local_files_only={local_files_only_literal},
)
tokenizer.pad_token = tokenizer.eos_token
lora_config = LoraConfig(
    r={int(lora_r)},
    lora_alpha={int(lora_r) * 2},
    lora_dropout=0.05,
    target_modules=["q_proj", "k_proj", "v_proj", "o_proj"],
    task_type="CAUSAL_LM",
)
model = get_peft_model(model, lora_config)
dataset = load_dataset("json", data_files={_json.dumps(str(dataset_path))}, split="train")
eval_dataset = load_dataset("json", data_files={eval_dataset_literal}, split="train") if {eval_dataset_literal} else None
trainer = SFTTrainer(
    model=model,
    processing_class=tokenizer,
    train_dataset=dataset,
    eval_dataset=eval_dataset,
    args=SFTConfig(
        output_dir={_json.dumps(str(output_dir))},
        num_train_epochs={int(epochs)},
        per_device_train_batch_size={int(batch_size)},
        learning_rate={float(lr)},
        max_length={int(max_seq_len)},
        gradient_accumulation_steps={accumulation},
        warmup_ratio={warmup},
        lr_scheduler_type="linear",
        logging_steps=10,
        save_strategy="epoch",
        eval_strategy={eval_strategy_literal},
        load_best_model_at_end={load_best_literal},
        metric_for_best_model="eval_loss",
        greater_is_better=False,
        report_to="none",
    ),
)
trainer.train()
model.save_pretrained({_json.dumps(str(output_dir) + "/lora_adapter")})
print("Training complete")"""


def _build_merge_script(base_model: str, adapter_path: str, output_dir: str, model_revision: str | None) -> str:
    """Build the LoRA merge script for GGUF conversion."""
    adapter = json.dumps(str(adapter_path))
    outdir = json.dumps(str(output_dir))
    basemodel = json.dumps(str(base_model))
    revision_literal = _revision_literal(model_revision)
    local_files_only_literal = _local_files_only_literal(model_revision)
    return f"""
import json as _json
from peft import AutoPeftModelForCausalLM
from transformers import AutoTokenizer
import torch

model = AutoPeftModelForCausalLM.from_pretrained(
    {adapter},
    local_files_only=True,
    device_map="cpu",
    torch_dtype=torch.float16,
)
model = model.merge_and_unload()
model.save_pretrained({outdir} + "/merged")
tokenizer = AutoTokenizer.from_pretrained(
    {basemodel},
    revision={revision_literal},
    local_files_only={local_files_only_literal},
)
tokenizer.save_pretrained({outdir} + "/merged")
print("Merge complete:", {outdir} + "/merged")"""


def _run_script_or_raise(script_path: Path, output_dir: str, *, timeout_seconds: int, label: str) -> None:
    """Run a generated script and raise a bounded RuntimeError on failure."""
    try:
        proc = _run_training_subprocess([sys.executable, str(script_path)], timeout_seconds=timeout_seconds)
    except subprocess.TimeoutExpired as exc:
        _cleanup_tmp_artifacts(output_dir)
        raise RuntimeError(f"{label} timed out after {exc.timeout} seconds") from exc
    if proc.returncode != 0:
        _cleanup_tmp_artifacts(output_dir)
        raise RuntimeError(f"{label} failed:\n{proc.stderr[-2000:]}")


class LocalTrainer:
    """QLoRA fine-tuning via unsloth (2x faster) or trl fallback."""

    def check_available(self) -> dict[str, bool]:
        """Check which training libraries are installed.

        Returns:
            Dict mapping library name to a bool indicating whether it can be
            discovered. Keys include: unsloth, trl, peft, transformers,
            bitsandbytes.
        """
        result = {}
        for lib in ["unsloth", "trl", "peft", "transformers", "bitsandbytes"]:
            result[lib] = _is_module_available(lib)
        return result

    def train_qlora(
        self,
        base_model: str,
        dataset_path: str,
        output_dir: str,
        epochs: int = 3,
        batch_size: int = 4,
        learning_rate: float = 2e-4,
        max_seq_length: int = 2048,
        lora_r: int = 16,
        use_unsloth: bool = True,
        model_revision: str | None = None,
        eval_dataset_path: str | None = None,
        gradient_accumulation_steps: int = 1,
        warmup_ratio: float = 0.0,
    ) -> str:
        """Run QLoRA training.  Returns path to the saved adapter.

        VRAM budget (5090 32GB):
          7B Q4 model:  ~8GB VRAM for training
          14B Q4 model: ~14GB VRAM for training
          32B model:    too large — use cloud_trainer

        Args:
            base_model: The base model.
            dataset_path: The dataset path.
            output_dir: The output dir.
            epochs: The epochs.
            batch_size: The batch size.
            learning_rate: The learning rate.
            max_seq_length: The max seq length.
            lora_r: The lora r.
            use_unsloth: The use unsloth.
            model_revision: Optional immutable Hugging Face revision for remote base models.
            eval_dataset_path: Optional JSON holdout dataset used for
                eval-during-training.
            gradient_accumulation_steps: Gradient accumulation steps passed to SFTConfig.
            warmup_ratio: Learning-rate warmup ratio passed to SFTConfig.

        Returns:
            Filesystem path to the saved LoRA adapter directory (e.g.,
            ``<output_dir>/lora_adapter``), ready for GGUF conversion.

        Raises:
            RuntimeError: If required training libraries are not installed or if the training subprocess fails.
        """
        accumulation, warmup = _validate_training_schedule(gradient_accumulation_steps, warmup_ratio)
        avail = self.check_available()
        if not avail.get("trl") and not avail.get("transformers"):
            raise RuntimeError("Training libraries not installed. Run: pip install trl peft bitsandbytes transformers")

        if use_unsloth and avail.get("unsloth"):
            return self._train_with_unsloth(
                base_model,
                dataset_path,
                output_dir,
                epochs,
                batch_size,
                learning_rate,
                max_seq_length,
                lora_r,
                model_revision,
                eval_dataset_path,
                accumulation,
                warmup,
            )
        return self._train_with_trl(
            base_model,
            dataset_path,
            output_dir,
            epochs,
            batch_size,
            learning_rate,
            max_seq_length,
            lora_r,
            model_revision,
            eval_dataset_path,
            accumulation,
            warmup,
        )

    @staticmethod
    def _train_with_unsloth(
        base_model,
        dataset_path,
        output_dir,
        epochs,
        batch_size,
        lr,
        max_seq_len,
        lora_r,
        model_revision=None,
        eval_dataset_path=None,
        gradient_accumulation_steps=1,
        warmup_ratio=0.0,
    ) -> str:
        """Train using unsloth for 2x speed."""
        from vetinari.training.unsloth_train import build_unsloth_script

        script = build_unsloth_script(
            base_model=base_model,
            dataset_path=dataset_path,
            output_dir=output_dir,
            eval_dataset_path=eval_dataset_path,
            epochs=epochs,
            batch_size=batch_size,
            lr=lr,
            max_seq_len=max_seq_len,
            lora_r=lora_r,
            model_revision=model_revision,
            gradient_accumulation_steps=gradient_accumulation_steps,
            warmup_ratio=warmup_ratio,
        )
        script_path = Path(output_dir) / "train_script.py"
        script_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_writers._write_text_atomic(script_path, script)

        try:
            proc = _run_training_subprocess([sys.executable, str(script_path)])
        except subprocess.TimeoutExpired as exc:
            _cleanup_tmp_artifacts(output_dir)
            raise RuntimeError(f"Training timed out after {exc.timeout} seconds") from exc
        if proc.returncode != 0:
            _cleanup_tmp_artifacts(output_dir)
            raise RuntimeError(f"Training failed:\n{proc.stderr[-2000:]}")

        return str(Path(output_dir) / "lora_adapter")

    @staticmethod
    def _train_with_trl(
        base_model,
        dataset_path,
        output_dir,
        epochs,
        batch_size,
        lr,
        max_seq_len,
        lora_r,
        model_revision=None,
        eval_dataset_path=None,
        gradient_accumulation_steps=1,
        warmup_ratio=0.0,
    ) -> str:
        """Train using standard trl (slower than unsloth)."""
        script = _build_trl_script(
            base_model=base_model,
            dataset_path=dataset_path,
            output_dir=output_dir,
            epochs=epochs,
            batch_size=batch_size,
            lr=lr,
            max_seq_len=max_seq_len,
            lora_r=lora_r,
            model_revision=model_revision,
            eval_dataset_path=eval_dataset_path,
            gradient_accumulation_steps=gradient_accumulation_steps,
            warmup_ratio=warmup_ratio,
        )
        script_path = Path(output_dir) / "train_trl_script.py"
        script_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_writers._write_text_atomic(script_path, script)
        _run_script_or_raise(
            script_path,
            output_dir,
            timeout_seconds=_TRAINING_SUBPROCESS_TIMEOUT_SECONDS,
            label="Training",
        )
        return str(Path(output_dir) / "lora_adapter")


class GGUFConverter:
    """Converts trained LoRA adapter to GGUF format for local inference."""

    def convert(
        self,
        base_model: str,
        adapter_path: str,
        output_dir: str,
        quantization: str = "q4_k_m",
        model_revision: str | None = None,
    ) -> str:
        """Merge adapter with base model and convert to GGUF for local inference.

        Returns path to the .gguf file.
        Requires: llama-cpp-python (pip install llama-cpp-python)

        Args:
            base_model: The base model.
            adapter_path: The adapter path.
            output_dir: The output dir.
            quantization: The quantization.
            model_revision: Optional immutable Hugging Face revision for the base model tokenizer.

        Returns:
            Path to the converted ``.gguf`` file on success, or path to the
            merged model directory if GGUF conversion is unavailable
            (llama.cpp not installed).

        Raises:
            RuntimeError: If the adapter merge subprocess fails.
        """
        out_path = Path(output_dir) / f"model_{quantization}.gguf"
        merge_script = _build_merge_script(base_model, adapter_path, output_dir, model_revision)
        merge_path = Path(output_dir) / "merge_script.py"
        atomic_writers._write_text_atomic(merge_path, merge_script)
        _run_script_or_raise(
            merge_path,
            output_dir,
            timeout_seconds=_CONVERSION_SUBPROCESS_TIMEOUT_SECONDS,
            label="Merge",
        )

        try:
            convert_cmd = [
                sys.executable,
                "-m",
                "llama_cpp.tools.convert",
                str(Path(output_dir) / "merged"),
                "--outfile",
                str(out_path),
                "--outtype",
                quantization.replace("-", "_"),
            ]
            proc2 = _run_training_subprocess(
                convert_cmd,
                timeout_seconds=_CONVERSION_SUBPROCESS_TIMEOUT_SECONDS,
            )
            if proc2.returncode == 0:
                return str(out_path)
        except Exception as e:
            logger.warning("[GGUFConverter] llama_cpp convert failed: %s", e)

        logger.warning(
            "[GGUFConverter] GGUF conversion requires llama.cpp: "
            "pip install llama-cpp-python and use llama.cpp/convert.py manually",
        )
        return str(Path(output_dir) / "merged")  # Return merged model dir
