"""Model discovery value objects used by search and download workflows.

The classes in this module carry model candidate metadata and immutable
Hugging Face artifact provenance for the facade in ``vetinari.model_discovery``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ModelSource:
    """Provenance record -- kept for backward compat with model_search callers."""

    source_type: str
    url: str
    last_checked: str = ""
    confidence: float = 0.0

    def __repr__(self) -> str:
        """Show key identifying fields for debugging."""
        return f"ModelSource(source_type={self.source_type!r}, url={self.url!r}, confidence={self.confidence!r})"


@dataclass
class ModelCandidate:
    """Ranked model candidate returned by local, cloud, and external searches."""

    id: str
    name: str
    source_type: str
    metrics: dict[str, Any] = field(default_factory=dict)
    memory_gb: int = 2
    context_len: int = 2048
    version: str = ""
    last_updated: str = ""
    hard_data_score: float = 0.0
    benchmark_score: float = 0.0
    sentiment_score: float = 0.0
    recency_score: float = 1.0
    final_score: float = 0.0
    provenance: list[Any] = field(default_factory=list)  # Dict entries or ModelSource records.
    short_rationale: str = ""
    recommended_for: list[str] = field(default_factory=list)

    def __repr__(self) -> str:
        """Show key identifying fields for debugging."""
        return (
            f"ModelCandidate(id={self.id!r}, name={self.name!r},"
            f" source_type={self.source_type!r}, final_score={self.final_score!r})"
        )

    def to_dict(self) -> dict[str, Any]:
        """Convert the candidate to the legacy dictionary response shape.

        Returns:
            Dictionary preserving the public model-discovery response fields.
        """
        provenance_out = []
        for p in self.provenance:
            if isinstance(p, dict):
                provenance_out.append(p)
            else:
                provenance_out.append({
                    "source_type": p.source_type,
                    "url": p.url,
                    "last_checked": p.last_checked,
                    "confidence": p.confidence,
                })
        return {
            "id": self.id,
            "name": self.name,
            "source_type": self.source_type,
            "metrics": self.metrics,
            "memory_gb": self.memory_gb,
            "context_len": self.context_len,
            "version": self.version,
            "last_updated": self.last_updated,
            "hard_data_score": self.hard_data_score,
            "benchmark_score": self.benchmark_score,
            "sentiment_score": self.sentiment_score,
            "recency_score": self.recency_score,
            "final_score": self.final_score,
            "provenance": provenance_out,
            "short_rationale": self.short_rationale,
            "recommended_for": self.recommended_for,
        }


@dataclass(frozen=True, slots=True)
class RepoModelFile:
    """Immutable metadata for a downloadable model artifact."""

    repo_id: str
    filename: str
    revision: str
    requested_revision: str | None = None
    size: int | None = None
    sha256: str | None = None

    def __repr__(self) -> str:
        """Return a compact artifact identity for logs and diagnostics."""
        return (
            f"RepoModelFile(repo_id={self.repo_id!r}, filename={self.filename!r}, "
            f"revision={self.revision!r}, size={self.size!r})"
        )

    def to_dict(self) -> dict[str, Any]:
        """Convert the artifact metadata to a public GGUF descriptor.

        Returns:
            Dictionary with repository, revision, digest, and artifact fields.
        """
        return {
            "repo_id": self.repo_id,
            "filename": self.filename,
            "revision": self.revision,
            "requested_revision": self.requested_revision,
            "size": self.size,
            "sha256": self.sha256,
            "backend": "llama_cpp",
            "format": "gguf",
            "artifact_type": "file",
            "source_type": "huggingface",
        }


@dataclass(frozen=True, slots=True)
class RepoSnapshotFile:
    """Immutable metadata for one file in a native Hugging Face snapshot."""

    filename: str
    size: int | None = None
    sha256: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert one snapshot file to a public descriptor.

        Returns:
            Dictionary with the repository-relative filename and optional digest.
        """
        return {
            "filename": self.filename,
            "size": self.size,
            "sha256": self.sha256,
        }


@dataclass(frozen=True, slots=True)
class RepoModelSnapshot:
    """Immutable metadata for a native vLLM/NIM Hugging Face snapshot."""

    repo_id: str
    revision: str
    backend: str
    model_format: str
    requested_revision: str | None = None
    files: tuple[RepoSnapshotFile, ...] = ()

    def __repr__(self) -> str:
        """Return a compact snapshot identity for logs and diagnostics."""
        return (
            f"RepoModelSnapshot(repo_id={self.repo_id!r}, revision={self.revision!r}, "
            f"backend={self.backend!r}, format={self.model_format!r}, files={len(self.files)})"
        )

    @property
    def total_size(self) -> int | None:
        """Return the combined snapshot size when all file sizes are known."""
        sizes = [file.size for file in self.files]
        if not sizes or any(size is None for size in sizes):
            return None
        return sum(int(size) for size in sizes if size is not None)

    def to_dict(self) -> dict[str, Any]:
        """Convert the snapshot metadata to the public native-model shape.

        Returns:
            Dictionary with repository provenance and included snapshot files.
        """
        return {
            "repo_id": self.repo_id,
            "revision": self.revision,
            "requested_revision": self.requested_revision,
            "backend": self.backend,
            "format": self.model_format,
            "artifact_type": "snapshot",
            "source_type": "huggingface",
            "size": self.total_size,
            "files": [file.to_dict() for file in self.files],
        }
