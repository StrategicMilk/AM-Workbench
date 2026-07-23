"""Run the governed vanilla training path and emit a fail-closed proof receipt."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import os
import random
import subprocess
import tempfile
import venv
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from importlib import metadata
from pathlib import Path
from typing import Any

import tomllib

REPO_ROOT = Path(__file__).resolve().parents[1]
MODEL_ID = "trl-internal-testing/tiny-Qwen2ForCausalLM-2.5"
MODEL_REVISION = "4b10ebee6e13a2669155516652960f50984399fd"
DEPENDENCIES = ("torch", "transformers", "datasets", "peft", "trl", "bitsandbytes")
SCHEMA_VERSION = "training-exit-hatch-receipt.v1"
QUALITY_EVAL_TASKS = [
    {
        "prompt": "Return the token hatch-0.",
        "task_type": "training-exit-hatch",
        "expected": "hatch-0",
    }
]
CUDA_WHEEL_INDEX = "https://download.pytorch.org/whl/cu128"


class ProofFailure(RuntimeError):
    """A named fail-closed proof predicate failure."""

    def __init__(self, rule_id: str, message: str) -> None:
        super().__init__(f"{rule_id}: {message}")
        self.rule_id = rule_id


def _governed_bootstrap_specs(pyproject: Path = REPO_ROOT / "pyproject.toml") -> tuple[str, str]:
    """Return the governed CUDA torch and JSON Schema requirements."""
    try:
        extras = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["optional-dependencies"]
        torch_spec = next(item for item in extras["training"] if item.lower().startswith("torch"))
        jsonschema_spec = next(item for item in extras["redteam"] if item.lower().startswith("jsonschema"))
    except (OSError, KeyError, StopIteration, TypeError, tomllib.TOMLDecodeError) as exc:
        raise ProofFailure("EXIT-ENV", "governed bootstrap requirements are unavailable") from exc
    return torch_spec, jsonschema_spec


def _venv_python(environment: Path) -> Path:
    """Return the platform-specific interpreter inside a virtual environment."""
    return environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _bootstrap_commands(interpreter: Path) -> list[list[str]]:
    """Build the ordered, reviewable install commands for a vanilla proof environment."""
    torch_spec, jsonschema_spec = _governed_bootstrap_specs()
    common = [str(interpreter), "-m", "pip", "install", "--disable-pip-version-check"]
    return [
        [*common, torch_spec, "--index-url", CUDA_WHEEL_INDEX],
        [*common, "--editable", f"{REPO_ROOT}[training]", jsonschema_spec],
    ]


def run_bootstrapped_proof(output_root: Path, receipt_path: Path) -> int:
    """Create a disposable vanilla environment and execute the live proof in it.

    Main-session validation commonly starts in the full developer environment,
    where Unsloth is intentionally installed.  This wrapper reproduces the
    workflow's clean ``training``-only environment instead of treating that
    ambient package as proof that the exit hatch is broken.
    """
    with tempfile.TemporaryDirectory(prefix="vetinari-exit-hatch-venv-") as temporary:
        environment = Path(temporary) / "venv"
        try:
            venv.EnvBuilder(with_pip=True, clear=False).create(environment)
        except (OSError, subprocess.SubprocessError) as exc:
            raise ProofFailure("EXIT-ENV", "could not create the vanilla proof environment") from exc
        interpreter = _venv_python(environment)
        for command in _bootstrap_commands(interpreter):
            try:
                subprocess.run(command, cwd=REPO_ROOT, check=True)
            except subprocess.CalledProcessError as exc:
                raise ProofFailure("EXIT-ENV", "vanilla proof dependency installation failed") from exc
        command = [
            str(interpreter),
            str(Path(__file__).resolve()),
            "--output-dir",
            str(output_root),
            "--receipt",
            str(receipt_path),
        ]
        return subprocess.run(command, cwd=REPO_ROOT, check=False).returncode


def _cuda_quantization_probe() -> str:
    import bitsandbytes as bnb
    import torch

    if not torch.cuda.is_available():
        raise ProofFailure("EXIT-CUDA", "CUDA is unavailable")
    try:
        layer = bnb.nn.Linear4bit(64, 64, quant_type="nf4", compute_dtype=torch.bfloat16).to("cuda")
        output = layer(torch.zeros((1, 64), device="cuda", dtype=torch.bfloat16))
        if output.shape != (1, 64) or not torch.isfinite(output).all():
            raise ProofFailure("EXIT-CUDA", "bitsandbytes 4-bit probe returned invalid output")
    except ProofFailure:
        raise
    except Exception as exc:
        raise ProofFailure("EXIT-CUDA", f"bitsandbytes 4-bit CUDA probe failed: {type(exc).__name__}") from exc
    return str(torch.cuda.get_device_name(0))


def validate_environment(
    *,
    module_finder: Callable[[str], object | None] = importlib.util.find_spec,
    version_reader: Callable[[str], str] = metadata.version,
    cuda_probe: Callable[[], str] = _cuda_quantization_probe,
) -> tuple[dict[str, str], str]:
    """Reject accelerator-present or incomplete vanilla environments."""
    if module_finder("unsloth") is not None:
        raise ProofFailure("EXIT-ENV", "unsloth present; exit-hatch lane invalid")
    versions: dict[str, str] = {}
    for package in DEPENDENCIES:
        if module_finder(package) is None:
            raise ProofFailure("EXIT-ENV", f"required vanilla dependency is not importable: {package}")
        try:
            versions[package] = version_reader(package)
        except metadata.PackageNotFoundError as exc:
            raise ProofFailure("EXIT-ENV", f"installed version is unavailable: {package}") from exc
    return versions, cuda_probe()


def validate_loss_history(values: list[object]) -> tuple[list[float], float, float]:
    """Require two finite observations and a lower final loss window."""
    losses = [float(value) for value in values if isinstance(value, int | float) and math.isfinite(float(value))]
    if len(losses) < 2:
        raise ProofFailure("EXIT-LOSS", "at least two finite eval_loss observations are required")
    window = min(2, len(losses) // 2)
    first = sum(losses[:window]) / window
    final = sum(losses[-window:]) / window
    if final >= first:
        raise ProofFailure("EXIT-LOSS", f"final loss window {final:.6f} did not improve on {first:.6f}")
    return losses, first, final


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _artifact_hashes(path: Path) -> dict[str, str]:
    files = [path] if path.is_file() else sorted(item for item in path.rglob("*") if item.is_file())
    if not files:
        raise ProofFailure("EXIT-ARTIFACT", "adapter/deployment artifact is empty")
    return {item.relative_to(path if path.is_dir() else path.parent).as_posix(): _sha256(item) for item in files}


def _proof_path(raw_path: str, *, label: str, output_root: Path) -> Path:
    if not raw_path:
        raise ProofFailure("EXIT-ARTIFACT", f"{label} path is empty")
    path = Path(raw_path).resolve()
    if output_root not in path.parents:
        raise ProofFailure("EXIT-ARTIFACT", f"{label} path escaped the isolated output root")
    if not path.exists():
        raise ProofFailure("EXIT-ARTIFACT", f"{label} artifact is missing")
    return path


def _coherent_json(path: Path, *, run_id: str, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProofFailure("EXIT-PERSISTENCE", f"{label} evidence is unreadable") from exc
    if not isinstance(value, dict) or value.get("run_id", value.get("training_run_id")) != run_id:
        raise ProofFailure("EXIT-PERSISTENCE", f"{label} evidence does not match the live run")
    return value


def _write_dataset(path: Path) -> None:
    receipt = {
        "privacy_class": "operational",
        "retention_days": 30,
        "source": "training-exit-hatch",
        "redaction_applied": True,
    }
    rows = []
    for index in range(40):
        rows.append({
            "id": f"exit-hatch-{index:02d}",
            "instruction": "Return the token hatch-0.",
            "input": "",
            "output": "hatch-0",
            "text": "Return the token hatch-0. hatch-0",
            "metadata": {
                "dataset_revision": "training-exit-hatch-v1",
                "provenance": "training-exit-hatch",
                "privacy_receipt": receipt,
            },
        })
    path.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows), encoding="utf-8")


def _load_eval_losses(run_dir: Path) -> list[float]:
    state_files = sorted(run_dir.rglob("trainer_state.json"))
    if not state_files:
        raise ProofFailure("EXIT-LOSS", "trainer_state.json is missing")
    try:
        states = [json.loads(path.read_text(encoding="utf-8")) for path in state_files]
        state = max(states, key=lambda item: int(item.get("global_step", -1)))
        return [row["eval_loss"] for row in state.get("log_history", []) if "eval_loss" in row]
    except (OSError, json.JSONDecodeError, TypeError, KeyError, ValueError, AttributeError) as exc:
        raise ProofFailure("EXIT-LOSS", "trainer state is unreadable") from exc


def validate_receipt(receipt: dict[str, Any]) -> None:
    """Validate every terminal receipt predicate independently."""
    if receipt.get("schema_version") != SCHEMA_VERSION or receipt.get("performance_mode") != "degraded-performance":
        raise ProofFailure("EXIT-PERSISTENCE", "receipt schema or performance label is invalid")
    if receipt.get("acceleration_state") != "unsloth-absent" or receipt.get("training_backend") != "trl":
        raise ProofFailure("EXIT-ENV", "receipt does not prove the vanilla TRL branch")
    validate_loss_history(list(receipt.get("eval_losses", [])))
    for key in ("adapter_hashes", "deployment_hashes"):
        if not isinstance(receipt.get(key), dict) or not receipt[key]:
            raise ProofFailure("EXIT-ARTIFACT", f"{key} is empty")
    if not receipt.get("run_persisted") or not receipt.get("evaluation_evidence_persisted"):
        raise ProofFailure("EXIT-PERSISTENCE", "run or evaluation evidence was not persisted")
    if receipt.get("quality_gate") != "deploy" or not receipt.get("pipeline_success"):
        raise ProofFailure("EXIT-GATE", "production quality gate did not approve deployment")


def _safe_reason(exc: Exception, output_root: Path) -> str:
    message = str(exc).replace(str(output_root), "<output-root>").replace(str(REPO_ROOT), "<repo-root>")
    return message[:1000]


@contextmanager
def _isolated_runtime_environment(output_root: Path) -> Iterator[None]:
    """Keep all mutable Vetinari runtime state inside the proof root."""
    overrides = {
        "HF_HOME": str(output_root / "huggingface"),
        "HF_HUB_CACHE": str(output_root / "huggingface" / "hub"),
        "HF_HUB_DISABLE_SYMLINKS_WARNING": "1",
        "HF_HUB_DISABLE_TELEMETRY": "1",
        "HF_HUB_DISABLE_XET": "1",
        "TOKENIZERS_PARALLELISM": "false",
        "VETINARI_USER_DIR": str(output_root),
        "VETINARI_DATA_ROOT": str(output_root / "data-root"),
        "VETINARI_MODELS_DIR": str(output_root / "model-cache"),
        "VETINARI_NATIVE_MODELS_DIR": str(output_root / "deployed-models"),
    }
    previous = {name: os.environ.get(name) for name in overrides}
    previous_working_directory = Path.cwd()
    os.environ.update(overrides)
    os.chdir(output_root)
    try:
        yield
    finally:
        os.chdir(previous_working_directory)
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def _execute_live_pipeline(
    root: Path,
    receipt_path: Path,
    versions: dict[str, str],
    device: str,
) -> dict[str, Any]:
    """Execute and validate the production pipeline inside an isolated state root."""
    random.seed(20260722)
    dataset = root / "fixture.jsonl"
    _write_dataset(dataset)

    from vetinari.training.pipeline import TrainingPipeline

    pipeline = TrainingPipeline()
    run = pipeline.run(
        base_model=MODEL_ID,
        model_revision=MODEL_REVISION,
        dataset_path=str(dataset),
        output_base_dir=str(root / "runs"),
        task_type="training-exit-hatch",
        epochs=60,
        gradient_accumulation_steps=1,
        warmup_ratio=0.0,
        backend="vllm",
        model_format="safetensors",
        quality_eval_tasks=QUALITY_EVAL_TASKS,
    )
    run_dir = root / "runs" / run.run_id
    if not run.success:
        raise ProofFailure("EXIT-GATE", f"TrainingPipeline.run failed: {run.error}")
    losses, first_loss, final_loss = validate_loss_history(_load_eval_losses(run_dir))
    adapter = _proof_path(run.adapter_path, label="adapter", output_root=root)
    deployed = _proof_path(run.output_model_path, label="deployment", output_root=root)
    manifest = _proof_path(run.model_manifest_path, label="manifest", output_root=root)
    persisted = run_dir / "run.json"
    evaluation = _proof_path(run.eval_evidence_path, label="evaluation", output_root=root)
    persisted_payload = _coherent_json(persisted, run_id=run.run_id, label="run")
    _coherent_json(evaluation, run_id=run.run_id, label="evaluation")
    manifest_payload = _coherent_json(manifest, run_id=run.run_id, label="manifest")
    if persisted_payload.get("success") is not True or not manifest_payload.get("files"):
        raise ProofFailure("EXIT-PERSISTENCE", "persisted success or manifest file inventory is incomplete")
    quality_gate = "deploy" if "quality_gate=deploy" in run.eval_reason else run.eval_status
    receipt = {
        "schema_version": SCHEMA_VERSION,
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "performance_mode": "degraded-performance",
        "acceleration_state": "unsloth-absent",
        "dependency_versions": versions,
        "cuda_device": device,
        "model_id": MODEL_ID,
        "model_revision": MODEL_REVISION,
        "seed": 20260722,
        "run_id": run.run_id,
        "backend": run.backend,
        "training_backend": "trl",
        "quality_eval_tasks": len(QUALITY_EVAL_TASKS),
        "eval_losses": losses,
        "first_eval_loss_window": first_loss,
        "final_eval_loss_window": final_loss,
        "loss_decreased": final_loss < first_loss,
        "adapter_path": adapter.relative_to(root).as_posix(),
        "deployment_path": deployed.relative_to(root).as_posix(),
        "manifest_path": manifest.relative_to(root).as_posix(),
        "adapter_hashes": _artifact_hashes(adapter),
        "deployment_hashes": _artifact_hashes(deployed),
        "run_persisted": True,
        "evaluation_evidence_persisted": True,
        "quality_gate": quality_gate,
        "pipeline_success": run.success,
        "failure_reason": None,
    }
    validate_receipt(receipt)
    _atomic_json(receipt_path, receipt)
    return receipt


def run_proof(output_root: Path, receipt_path: Path) -> dict[str, Any]:
    """Execute the live pipeline and return a validated receipt."""
    root = output_root.resolve()
    if root == REPO_ROOT or REPO_ROOT in root.parents:
        raise ProofFailure("EXIT-ENV", "proof output must be isolated outside the repository")
    root.mkdir(parents=True, exist_ok=False)
    versions, device = validate_environment()
    receipt_destination = receipt_path.resolve()
    with _isolated_runtime_environment(root):
        return _execute_live_pipeline(root, receipt_destination, versions, device)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--receipt", type=Path)
    parser.add_argument(
        "--bootstrap-vanilla",
        action="store_true",
        help="create a disposable governed training-only environment before running the proof",
    )
    args = parser.parse_args(argv)
    output = args.output_dir or Path(tempfile.gettempdir()) / f"vetinari-exit-hatch-{os.getpid()}"
    receipt_path = args.receipt or output.with_name(output.name + "-receipt.json")
    try:
        if args.bootstrap_vanilla:
            return run_bootstrapped_proof(output, receipt_path)
        run_proof(output, receipt_path)
    except Exception as exc:
        rule_id = exc.rule_id if isinstance(exc, ProofFailure) else "EXIT-PERSISTENCE"
        failure = {
            "schema_version": SCHEMA_VERSION,
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "performance_mode": "degraded-performance",
            "acceleration_state": "unknown",
            "pipeline_success": False,
            "quality_gate": "unavailable",
            "failure_rule": rule_id,
            "failure_reason": _safe_reason(exc, output),
        }
        _atomic_json(receipt_path, failure)
        print(failure["failure_reason"], file=os.sys.stderr)
        return 1
    print(str(receipt_path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
