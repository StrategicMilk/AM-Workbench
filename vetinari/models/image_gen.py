"""Image generation routing helpers."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from vetinari.models.diffusion import DiffusionModelIndex, DiffusionModelSpec

_VIDEO_FORMATS = frozenset({"mp4", "webm", "avi", "mov", "mkv"})
_AUDIO_FORMATS = frozenset({"mp3", "wav", "ogg", "flac", "aac", "m4a"})
_3D_FORMATS = frozenset({"obj", "glb", "gltf", "usdz", "fbx", "stl"})


class ImageGenRouter:
    """Select an image generation backend."""

    @staticmethod
    def select_backend(*, output_format: str) -> str:
        """Select a backend for an output format.

        Args:
            output_format: Desired image output format.

        Returns:
            Backend identifier.
        """
        normalized_format = output_format.lower()
        if normalized_format == "svg":
            return "svg-vector-backend"
        if normalized_format in _VIDEO_FORMATS:
            return "video-backend"
        if normalized_format in _AUDIO_FORMATS:
            return "audio-backend"
        if normalized_format in _3D_FORMATS:
            return "3d-backend"
        return "diffusion-backend"


@dataclass(frozen=True, slots=True)
class ImageGenResult:
    """Image generation result."""

    placeholder: bool = False
    image_path: str | None = None

    @property
    def success(self) -> bool:
        """Return whether generation produced a real image."""
        return not self.placeholder and self.image_path is not None


class ImageModelDiscovery:
    """Discover image model files."""

    def __init__(self, roots: list[str | Path]) -> None:
        self.roots = [Path(root) for root in roots]

    def run(self) -> list[DiffusionModelSpec]:
        """Run image model discovery.

        Returns:
            Discovered image model specs.
        """
        return DiffusionModelIndex(self.roots).discover()


__all__ = ["ImageGenResult", "ImageGenRouter", "ImageModelDiscovery"]
