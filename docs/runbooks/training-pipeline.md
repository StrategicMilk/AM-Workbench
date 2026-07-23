# Training Pipeline Runbook

## Prerequisites

- Training data in the configured user data directory.
- A base model that matches the selected backend and format.
- Enough free disk space for checkpoints and generated adapters.
- GPU capacity for local fine-tuning, or a CPU path for small validation runs.
- Optional libraries installed when running full TRL or PEFT workflows.

## Check Status

```bash
python -m vetinari train status
python -m vetinari train data
```

`train status` reports the current curriculum phase and idle state. `train data`
reports seed datasets and collected execution records.

## Seed Data

```bash
python -m vetinari train seed
```

Run this when `train data` shows no seed datasets. Review generated or imported
records before using them for promotion decisions.

## Run Training

```bash
python -m vetinari train run --backend vllm --base-model auto
```

Use `--skill <task-type>` to scope a manual run to one task type. For
llama.cpp-backed GGUF work, choose the backend and model format that match the
local model inventory.

## Failure Checks

- If libraries are missing, install the packages named by the CLI error.
- If training data is absent, run `train seed` or collect execution records.
- If GPU memory is low, reduce the run size or switch to a smaller base model.
- If post-training validation fails, keep the candidate out of promoted
  defaults and inspect `vetinari/training/quality_gate.py`.

## References

- [`vetinari/training/`](../../vetinari/training/)
- [`vetinari/cli_training.py`](../../vetinari/cli_training.py)
- [`docs/reference/training.md`](../reference/training.md)
