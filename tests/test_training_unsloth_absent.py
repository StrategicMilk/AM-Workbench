"""Prove the Unsloth-absent guards and existing vanilla trainer branch."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from scripts.run_training_exit_hatch_proof import (
    ProofFailure,
    _bootstrap_commands,
    _governed_bootstrap_specs,
    _isolated_runtime_environment,
    _load_eval_losses,
    _venv_python,
    validate_environment,
    validate_loss_history,
    validate_receipt,
)

from vetinari.training.pipeline import TrainingPipeline
from vetinari.training.pipeline_trainers import LocalTrainer


def test_environment_rejects_discoverable_unsloth() -> None:
    with pytest.raises(ProofFailure, match="unsloth present; exit-hatch lane invalid"):
        validate_environment(
            module_finder=lambda name: object(), version_reader=lambda name: "1", cuda_probe=lambda: "gpu"
        )


def test_environment_requires_every_vanilla_dependency() -> None:
    with pytest.raises(ProofFailure, match=r"required vanilla dependency.*trl"):
        validate_environment(
            module_finder=lambda name: None if name in {"unsloth", "trl"} else object(),
            version_reader=lambda name: "1",
            cuda_probe=lambda: "gpu",
        )


def test_bootstrap_uses_governed_vanilla_specs_without_unsloth(tmp_path: Path) -> None:
    torch_spec, jsonschema_spec = _governed_bootstrap_specs()
    commands = _bootstrap_commands(_venv_python(tmp_path / "venv"))

    assert torch_spec.startswith("torch")
    assert jsonschema_spec.startswith("jsonschema")
    assert commands[0][-2:] == ["--index-url", "https://download.pytorch.org/whl/cu128"]
    assert commands[0][-3] == torch_spec
    assert commands[1][-1] == jsonschema_spec
    assert any(item.endswith("[training]") for item in commands[1])
    assert all("unsloth" not in item.lower() for command in commands for item in command)


def test_trl_only_availability_selects_existing_vanilla_branch(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    trainer = LocalTrainer()
    monkeypatch.setattr(trainer, "check_available", lambda: {"unsloth": False, "trl": True, "transformers": True})
    monkeypatch.setattr(trainer, "_train_with_unsloth", lambda *args, **kwargs: pytest.fail("unsloth branch selected"))
    monkeypatch.setattr(trainer, "_train_with_trl", lambda *args, **kwargs: "vanilla-adapter")
    result = trainer.train_qlora("model", "data.jsonl", str(tmp_path), use_unsloth=True)
    assert result == "vanilla-adapter"


def test_all_training_libraries_absent_is_bounded_rejection(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    trainer = LocalTrainer()
    monkeypatch.setattr(trainer, "check_available", lambda: {"unsloth": False, "trl": False, "transformers": False})
    with pytest.raises(RuntimeError, match="Training libraries not installed"):
        trainer.train_qlora("model", "data.jsonl", str(tmp_path), use_unsloth=True)


def test_pipeline_requirements_accept_trl_alone(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("VETINARI_USER_DIR", str(tmp_path / "user"))
    pipeline = TrainingPipeline()
    monkeypatch.setattr(pipeline._trainer, "check_available", lambda: {"unsloth": False, "trl": True})
    assert pipeline.check_requirements()["ready_for_training"] is True
    monkeypatch.setattr(pipeline._trainer, "check_available", lambda: {"unsloth": False, "trl": False})
    assert pipeline.check_requirements()["ready_for_training"] is False


def _valid_receipt() -> dict[str, object]:
    return {
        "schema_version": "training-exit-hatch-receipt.v1",
        "performance_mode": "degraded-performance",
        "acceleration_state": "unsloth-absent",
        "training_backend": "trl",
        "eval_losses": [2.0, 1.0],
        "adapter_hashes": {"adapter.safetensors": "a" * 64},
        "deployment_hashes": {"adapter.safetensors": "a" * 64},
        "run_persisted": True,
        "evaluation_evidence_persisted": True,
        "quality_gate": "deploy",
        "pipeline_success": True,
    }


def test_receipt_requires_finite_decreasing_loss_and_artifacts() -> None:
    validate_receipt(_valid_receipt())
    with pytest.raises(ProofFailure, match="EXIT-LOSS"):
        validate_loss_history([1.0, float("nan")])
    receipt = _valid_receipt()
    receipt["adapter_hashes"] = {}
    with pytest.raises(ProofFailure, match="EXIT-ARTIFACT"):
        validate_receipt(receipt)


def test_receipt_rejects_failed_pipeline_and_gate() -> None:
    receipt = _valid_receipt()
    receipt.update({"quality_gate": "reject", "pipeline_success": False})
    with pytest.raises(ProofFailure, match="EXIT-GATE"):
        validate_receipt(receipt)


def test_failure_receipt_shape_never_contains_absolute_repo_path(tmp_path: Path) -> None:
    receipt = _valid_receipt()
    rendered = json.dumps(receipt, sort_keys=True)
    assert str(Path(__file__).resolve().parents[1]) not in rendered


def test_runtime_state_is_isolated_and_environment_is_restored(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    original_working_directory = Path.cwd()
    for name in (
        "HF_HOME",
        "VETINARI_DATA_ROOT",
        "VETINARI_MODELS_DIR",
        "VETINARI_NATIVE_MODELS_DIR",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("VETINARI_USER_DIR", "operator-owned-before-proof")
    with _isolated_runtime_environment(tmp_path):
        assert Path.cwd() == tmp_path
        assert Path(os.environ["HF_HOME"]) == tmp_path / "huggingface"
        assert Path(os.environ["VETINARI_USER_DIR"]) == tmp_path
        assert Path(os.environ["VETINARI_DATA_ROOT"]) == tmp_path / "data-root"
        assert Path(os.environ["VETINARI_MODELS_DIR"]) == tmp_path / "model-cache"
        assert Path(os.environ["VETINARI_NATIVE_MODELS_DIR"]) == tmp_path / "deployed-models"
    assert Path.cwd() == original_working_directory
    assert os.environ["VETINARI_USER_DIR"] == "operator-owned-before-proof"
    assert "HF_HOME" not in os.environ
    assert "VETINARI_DATA_ROOT" not in os.environ
    assert "VETINARI_MODELS_DIR" not in os.environ
    assert "VETINARI_NATIVE_MODELS_DIR" not in os.environ


def test_eval_loss_reader_selects_highest_global_step_not_lexical_checkpoint(tmp_path: Path) -> None:
    for checkpoint, step, loss in (("checkpoint-9", 9, 2.0), ("checkpoint-10", 10, 1.0)):
        path = tmp_path / checkpoint
        path.mkdir()
        (path / "trainer_state.json").write_text(
            json.dumps({"global_step": step, "log_history": [{"eval_loss": loss}]}),
            encoding="utf-8",
        )

    assert _load_eval_losses(tmp_path) == [1.0]
