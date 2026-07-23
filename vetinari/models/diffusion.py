"""Diffusion model discovery helpers."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from vetinari.utils.bounded_collections import bounded_rglob

_DIFFUSION_EXTENSIONS = ("*.safetensors", "*.gguf", "*.bin", "*.pt", "*.ckpt")
_DIFFUSION_SCAN_MAX_DEPTH = 8
_DIFFUSION_SCAN_MAX_FILES = 10_000


@dataclass(frozen=True, slots=True)
class DiffusionModelSpec:
    """Diffusion model specification."""

    model_id: str
    load_path: Path

    @property
    def file_path(self) -> Path:
        """Return the discovered model file path."""
        return self.load_path

    @classmethod
    def from_path(cls, path: str | Path) -> DiffusionModelSpec:
        """Build a model spec from a file path.

        Args:
            path: Model file path.

        Returns:
            Diffusion model spec.
        """
        model_path = Path(path)
        return cls(model_id=model_path.stem, load_path=model_path)


class DiffusionModelIndex:
    """Discover diffusion model files under declared roots."""

    def __init__(self, search_roots: list[str | Path]) -> None:
        self.search_roots = [Path(root) for root in search_roots]

    def discover(self) -> list[DiffusionModelSpec]:
        """Discover model specs.

        Returns:
            Discovered model specs with stable unique ids.
        """
        specs: list[DiffusionModelSpec] = []
        seen: dict[str, int] = {}
        for root in self.search_roots:
            for pattern in _DIFFUSION_EXTENSIONS:
                for path in sorted(
                    bounded_rglob(
                        root,
                        pattern,
                        max_depth=_DIFFUSION_SCAN_MAX_DEPTH,
                        max_files=_DIFFUSION_SCAN_MAX_FILES,
                    )
                ):
                    base_id = path.stem
                    index = seen.get(base_id, 0)
                    seen[base_id] = index + 1
                    model_id = base_id if index == 0 else f"{base_id}-{index + 1}"
                    specs.append(DiffusionModelSpec(model_id=model_id, load_path=path))
        return specs


__all__ = ["DiffusionModelIndex", "DiffusionModelSpec"]
