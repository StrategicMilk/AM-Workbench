"""Hugging Face repository resolution for model discovery.

The repository mixin resolves mutable repository requests into immutable commit
and artifact metadata before downloads or UI catalog responses use them.
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
import sys
from pathlib import PurePosixPath
from typing import Any

from vetinari.constants import MODEL_DISCOVERY_TIMEOUT
from vetinari.model_discovery_artifacts import (
    _MAX_REPO_FILES,
    _NATIVE_DOWNLOAD_BACKENDS,
    _extract_lfs_metadata,
    _infer_file_quantization,
    _infer_model_family,
    _matches_artifact_filters,
    _normalize_backend,
    _normalize_model_format,
    _safe_hf_filename,
    _select_native_snapshot_files,
    _validate_repo_id,
)
from vetinari.model_discovery_types import RepoModelFile, RepoModelSnapshot

logger = logging.getLogger(__name__)
_HF_TERMS_URL = "https://huggingface.co/terms-of-service"


def _get_hf_api_class() -> Any:
    """Return the Hugging Face API class after a non-executing availability probe."""
    if "huggingface_hub" in sys.modules and sys.modules["huggingface_hub"] is None:
        raise RuntimeError("huggingface_hub is not installed")
    try:
        available = "huggingface_hub" in sys.modules or importlib.util.find_spec("huggingface_hub") is not None
    except (ModuleNotFoundError, ValueError):
        available = False
    if not available:
        raise RuntimeError("huggingface_hub is not installed")
    try:
        return importlib.import_module("huggingface_hub").HfApi
    except ModuleNotFoundError as exc:
        raise RuntimeError("huggingface_hub is not installed") from exc


class _ModelDiscoveryRepository:
    """Repository metadata behavior mixed into ModelDiscovery downloads."""

    @staticmethod
    def _resolve_repo_file(repo_id: str, filename: str, revision: str | None = None) -> RepoModelFile:
        """Resolve a repo file to immutable Hugging Face metadata."""
        repo_id = _validate_repo_id(repo_id)
        relative = _safe_hf_filename(filename)

        HfApi = _get_hf_api_class()

        api = HfApi()
        info = api.model_info(
            repo_id=repo_id,
            revision=revision,
            timeout=MODEL_DISCOVERY_TIMEOUT,
            files_metadata=True,
            token=False,
        )
        resolved_revision = getattr(info, "sha", None)
        if not resolved_revision:
            raise RuntimeError("Hugging Face did not return an immutable revision for the model repo")

        match = None
        for sibling in getattr(info, "siblings", []) or []:
            if getattr(sibling, "rfilename", None) == str(relative):
                match = sibling
                break
        if match is None:
            raise FileNotFoundError(f"{filename!r} was not found in Hugging Face repo {repo_id!r}")

        size, sha256 = _extract_lfs_metadata(match)
        return RepoModelFile(
            repo_id=repo_id,
            filename=str(relative),
            revision=resolved_revision,
            requested_revision=revision,
            size=size,
            sha256=sha256,
        )

    @staticmethod
    def _resolve_repo_snapshot(
        repo_id: str,
        *,
        backend: str,
        model_format: str,
        revision: str | None = None,
        objective: str | None = None,
        family: str | None = None,
        quantization: str | None = None,
        file_type: str | None = None,
        min_size_gb: float | None = None,
        max_size_gb: float | None = None,
    ) -> RepoModelSnapshot:
        """Resolve a native Hugging Face snapshot to immutable metadata."""
        repo_id = _validate_repo_id(repo_id)

        HfApi = _get_hf_api_class()

        info = HfApi().model_info(
            repo_id=repo_id,
            revision=revision,
            timeout=MODEL_DISCOVERY_TIMEOUT,
            files_metadata=True,
            token=False,
        )
        resolved_revision = getattr(info, "sha", None)
        if not resolved_revision:
            raise RuntimeError("Hugging Face did not return an immutable revision for the model repo")

        files = _select_native_snapshot_files(
            list(getattr(info, "siblings", []) or []),
            repo_id=repo_id,
            model_format=model_format,
            objective=objective,
            family=family,
            quantization=quantization,
            file_type=file_type,
            min_size_gb=min_size_gb,
            max_size_gb=max_size_gb,
        )
        return RepoModelSnapshot(
            repo_id=repo_id,
            revision=resolved_revision,
            requested_revision=revision,
            backend=backend,
            model_format=model_format,
            files=files,
        )

    def _native_repo_file_dicts(
        self,
        repo_id: str,
        *,
        backend: str,
        model_format: str,
        revision: str | None,
        objective: str | None,
        family: str | None,
        quantization: str | None,
        file_type: str | None,
        min_size_gb: float | None,
        max_size_gb: float | None,
        use_case: str,
    ) -> list[dict[str, Any]]:
        """Return artifact dictionaries for native snapshot backends."""
        snapshot = self._resolve_repo_snapshot(
            repo_id,
            backend=backend,
            model_format=model_format,
            revision=revision,
            objective=objective,
            family=family,
            quantization=quantization,
            file_type=file_type,
            min_size_gb=min_size_gb,
            max_size_gb=max_size_gb,
        )
        return [
            {
                **file.to_dict(),
                "repo_id": snapshot.repo_id,
                "revision": snapshot.revision,
                "backend": snapshot.backend,
                "format": snapshot.model_format,
                "artifact_type": "snapshot_file",
                "use_case": use_case,
                "source_type": "huggingface",
                "family": _infer_model_family(f"{snapshot.repo_id}/{file.filename}"),
                "quantization": _infer_file_quantization(file.filename, model_format=snapshot.model_format),
                "file_type": PurePosixPath(file.filename).suffix.lower().lstrip("."),
                "license": "unknown",
                "terms_url": _HF_TERMS_URL,
            }
            for file in snapshot.files
        ]

    def _gguf_repo_file_dicts(
        self,
        repo_id: str,
        *,
        revision: str | None,
        vram_gb: int,
        use_case: str,
        objective: str | None,
        family: str | None,
        quantization: str | None,
        file_type: str | None,
        min_size_gb: float | None,
        max_size_gb: float | None,
    ) -> list[dict[str, Any]]:
        """Return bounded GGUF file dictionaries for llama-cpp downloads."""
        info = _get_hf_api_class()().model_info(
            repo_id=repo_id,
            revision=revision,
            timeout=MODEL_DISCOVERY_TIMEOUT,
            files_metadata=True,
            token=False,
        )
        resolved_revision = getattr(info, "sha", None)
        if not resolved_revision:
            raise RuntimeError("Hugging Face did not return an immutable revision for the model repo")

        files: list[dict[str, Any]] = []
        max_bytes = int(vram_gb * 0.9 * 1024**3) if vram_gb > 0 else None
        for sibling in getattr(info, "siblings", []) or []:
            file_data = self._gguf_sibling_file_dict(
                sibling,
                repo_id=repo_id,
                resolved_revision=resolved_revision,
                max_bytes=max_bytes,
                use_case=use_case,
                objective=objective,
                family=family,
                quantization=quantization,
                file_type=file_type,
                min_size_gb=min_size_gb,
                max_size_gb=max_size_gb,
            )
            if file_data is not None:
                files.append(file_data)
            if len(files) >= _MAX_REPO_FILES:
                break
        return files

    @staticmethod
    def _gguf_sibling_file_dict(
        sibling: Any,
        *,
        repo_id: str,
        resolved_revision: str,
        max_bytes: int | None,
        use_case: str,
        objective: str | None,
        family: str | None,
        quantization: str | None,
        file_type: str | None,
        min_size_gb: float | None,
        max_size_gb: float | None,
    ) -> dict[str, Any] | None:
        """Return one GGUF file dictionary when the repo sibling matches filters."""
        name = getattr(sibling, "rfilename", "")
        if not isinstance(name, str) or not name.lower().endswith(".gguf"):
            return None
        size, sha256 = _extract_lfs_metadata(sibling)
        if max_bytes is not None and size is not None and size > max_bytes:
            return None
        if not _matches_artifact_filters(
            name=f"{repo_id}/{name}",
            size=size,
            model_format="gguf",
            objective=objective,
            family=family,
            quantization=quantization,
            file_type=file_type,
            min_size_gb=min_size_gb,
            max_size_gb=max_size_gb,
        ):
            return None
        return {
            "filename": name,
            "repo_id": repo_id,
            "revision": resolved_revision,
            "size": size,
            "sha256": sha256,
            "backend": "llama_cpp",
            "format": "gguf",
            "artifact_type": "file",
            "use_case": use_case,
            "source_type": "huggingface",
            "family": _infer_model_family(f"{repo_id}/{name}"),
            "quantization": _infer_file_quantization(name, model_format="gguf"),
            "file_type": "gguf",
            "license": "unknown",
            "terms_url": _HF_TERMS_URL,
        }

    def get_repo_files(
        self,
        repo_id: str,
        vram_gb: int = 32,
        use_case: str = "general",
        *,
        backend: str = "llama_cpp",
        model_format: str | None = None,
        revision: str | None = None,
        objective: str | None = None,
        family: str | None = None,
        quantization: str | None = None,
        file_type: str | None = None,
        min_size_gb: float | None = None,
        max_size_gb: float | None = None,
    ) -> list[dict[str, Any]]:
        """Return bounded model artifact descriptors for a Hugging Face repository.

        The response resolves the repository to the current immutable commit SHA
        and marks that SHA on every returned file.  Callers must pass that
        revision back into download operations if they need exact provenance.

        Args:
            repo_id: Hugging Face repository id to inspect.
            vram_gb: Available VRAM used for GGUF scoring and filtering.
            use_case: Model-use objective used for GGUF scoring.
            backend: Target runtime backend.
            model_format: Optional model artifact format override.
            revision: Optional branch, tag, or commit SHA to inspect.
            objective: Optional native snapshot objective filter.
            family: Optional model-family filter.
            quantization: Optional quantization filter.
            file_type: Optional artifact suffix/type filter.
            min_size_gb: Optional minimum artifact size.
            max_size_gb: Optional maximum artifact size.

        Returns:
            Bounded artifact dictionaries with immutable revision provenance.

        Raises:
            ValueError: If repository, backend, format, revision, or filters are invalid.
            FileNotFoundError: If no supported artifacts match the request.
            RuntimeError: If Hugging Face metadata cannot be resolved.
        """
        repo_id = _validate_repo_id(repo_id)
        backend = _normalize_backend(backend)
        model_format = _normalize_model_format(backend, model_format)

        if backend in _NATIVE_DOWNLOAD_BACKENDS:
            return self._native_repo_file_dicts(
                repo_id,
                backend=backend,
                model_format=model_format,
                revision=revision,
                objective=objective,
                family=family,
                quantization=quantization,
                file_type=file_type,
                min_size_gb=min_size_gb,
                max_size_gb=max_size_gb,
                use_case=use_case,
            )
        return self._gguf_repo_file_dicts(
            repo_id,
            revision=revision,
            vram_gb=vram_gb,
            use_case=use_case,
            objective=objective,
            family=family,
            quantization=quantization,
            file_type=file_type,
            min_size_gb=min_size_gb,
            max_size_gb=max_size_gb,
        )
