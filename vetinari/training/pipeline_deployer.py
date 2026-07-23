"""Deployment helpers for training pipeline artifacts."""

from __future__ import annotations

import hashlib
import logging
import sys
from pathlib import Path

from vetinari.constants import OPERATOR_MODELS_CACHE_DIR
from vetinari.learning import atomic_writers
from vetinari.utils.bounded_collections import BoundedList, bounded_rglob

logger = logging.getLogger(__name__)
_DEFAULT_NATIVE_MODELS_DIR = Path(OPERATOR_MODELS_CACHE_DIR) / "native"
_NATIVE_MODELS_DIR = _DEFAULT_NATIVE_MODELS_DIR
_MODELS_DIR = Path(OPERATOR_MODELS_CACHE_DIR)
_MAX_MANIFEST_FILES = 10000
_MAX_MANIFEST_DEPTH = 6
_HASH_CHUNK_BYTES = 1024 * 1024


def _native_models_dir() -> Path:
    if _NATIVE_MODELS_DIR != _DEFAULT_NATIVE_MODELS_DIR:
        return _NATIVE_MODELS_DIR
    trainers_module = sys.modules.get("vetinari.training.pipeline_trainers")
    if trainers_module is not None:
        patched_value = getattr(trainers_module, "_NATIVE_MODELS_DIR", None)
        if patched_value is not None:
            return Path(patched_value)
    return _NATIVE_MODELS_DIR


def _safe_model_name(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "-" for ch in value)
    return cleaned.strip("-._") or "model"


class ModelDeployer:
    """Deploys converted GGUF model to the local models directory."""

    def deploy(self, gguf_path: str, model_name: str) -> str:
        """Copy GGUF to local models directory.

        Args:
            gguf_path: Path to the source GGUF file.
            model_name: Subdirectory name under ``models/vetinari/`` where
                the file will be placed.

        Returns:
            Absolute path to the deployed GGUF file in the models directory.

        Raises:
            FileNotFoundError: If the GGUF model file does not exist at the given path.
        """
        src = Path(gguf_path)
        if not src.exists():
            raise FileNotFoundError(f"Model file not found: {gguf_path}")

        safe_name = _safe_model_name(model_name)
        dest_dir = _MODELS_DIR / "vetinari" / safe_name
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / src.name

        import shutil

        shutil.copy2(str(src), str(dest))
        manifest_path = dest_dir / ".vetinari-training-manifest.json"
        manifest = _gguf_manifest(
            artifact_path=dest,
            manifest_path=manifest_path,
            model_name=model_name,
            safe_model_name=safe_name,
            source_path=src,
        )
        atomic_writers.write_json_atomic(manifest_path, manifest)

        logger.info("[ModelDeployer] Deployed %s to %s", src.name, dest)
        return str(dest)

    def deploy_native(
        self,
        adapter_path: str,
        model_name: str,
        *,
        backend: str,
        model_format: str,
        base_model: str,
        base_model_revision: str | None = None,
        run_id: str = "",
        task_type: str = "general",
    ) -> dict[str, str]:
        """Copy a trained LoRA adapter into the native model artifact tree.

        The native path is used by vLLM and NIM handoff flows. It keeps the
        adapter separate from GGUF artifacts and writes enough provenance for a
        later server/package step to know the base model and exact revision.

        Args:
            adapter_path: Trained adapter file or directory to deploy.
            model_name: Logical model name used for the destination directory.
            backend: Native serving backend such as ``vllm`` or ``nim``.
            model_format: Native artifact format.
            base_model: Base model identifier for provenance.
            base_model_revision: Optional immutable base-model revision.
            run_id: Optional training run identifier.
            task_type: Task type associated with the adapter.

        Returns:
            Manifest dictionary describing the deployed adapter.

        Raises:
            FileNotFoundError: If the adapter path does not exist.
            OSError: If files cannot be copied or the manifest cannot be written.
        """
        import shutil

        src = Path(adapter_path)
        if not src.exists():
            raise FileNotFoundError(f"Adapter path not found: {adapter_path}")

        safe_name = _safe_model_name(model_name)
        dest_dir = _native_models_dir() / backend / model_format / "vetinari" / safe_name
        dest_dir.mkdir(parents=True, exist_ok=True)

        if src.is_dir():
            shutil.copytree(src, dest_dir, dirs_exist_ok=True)
            artifact_path = dest_dir
        else:
            artifact_path = dest_dir / src.name
            shutil.copy2(str(src), str(artifact_path))

        manifest_path = dest_dir / ".vetinari-training-manifest.json"
        manifest = _native_manifest(
            artifact_path=artifact_path,
            manifest_path=manifest_path,
            backend=backend,
            model_format=model_format,
            base_model=base_model,
            base_model_revision=base_model_revision,
            adapter_source=src,
            run_id=run_id,
            task_type=task_type,
        )
        atomic_writers.write_json_atomic(manifest_path, manifest)

        logger.info("[ModelDeployer] Deployed native adapter to %s", artifact_path)
        return {"path": str(artifact_path), "manifest_path": str(manifest_path)}


def _native_manifest(
    *,
    artifact_path: Path,
    manifest_path: Path,
    backend: str,
    model_format: str,
    base_model: str,
    base_model_revision: str | None,
    adapter_source: Path,
    run_id: str,
    task_type: str,
) -> dict[str, object]:
    from datetime import datetime, timezone

    return {
        "artifact_type": "trained_lora_adapter",
        "backend": backend,
        "format": model_format,
        "base_model": base_model,
        "base_model_revision": base_model_revision,
        "path": str(artifact_path),
        "adapter_source_path": str(adapter_source),
        "training_run_id": run_id,
        "task_type": task_type,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "files": _artifact_files(artifact_path),
        # vLLM/NIM server_handoff removed per AM Engine architecture decision 2026-06-09.
    }


def _gguf_manifest(
    *,
    artifact_path: Path,
    manifest_path: Path,
    model_name: str,
    safe_model_name: str,
    source_path: Path,
) -> dict[str, object]:
    from datetime import datetime, timezone

    return {
        "artifact_type": "gguf_model",
        "backend": "llama_cpp",
        "format": "gguf",
        "model_name": model_name,
        "safe_model_name": safe_model_name,
        "path": str(artifact_path),
        "source_path": str(source_path),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "files": _artifact_files(artifact_path),
        "server_handoff": {
            "llama_cpp_model_path": str(artifact_path),
            "training_manifest_path": str(manifest_path),
        },
    }


def _artifact_files(artifact_path: Path) -> list[dict[str, object]]:
    files: BoundedList[dict[str, object]] = BoundedList(_MAX_MANIFEST_FILES)
    scan_root = artifact_path if artifact_path.is_dir() else artifact_path.parent
    for path in sorted(
        p
        for p in bounded_rglob(scan_root, "*", max_depth=_MAX_MANIFEST_DEPTH, max_files=_MAX_MANIFEST_FILES)
        if p.is_file()
    ):
        if path.name == ".vetinari-training-manifest.json":
            continue
        files.append({
            "filename": path.relative_to(scan_root).as_posix(),
            "size": path.stat().st_size,
            "sha256": _sha256_file(path, hashlib),
        })
    return list(files)


def _sha256_file(path: Path, hashlib_module: object = hashlib) -> str:
    digest = hashlib_module.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_HASH_CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()
