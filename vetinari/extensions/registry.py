"""File-backed extension registry."""

from __future__ import annotations

import json
import logging
import shutil
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from vetinari.constants import get_user_dir
from vetinari.extensions.protocol import ExtensionManifest
from vetinari.learning.atomic_writers import write_json_atomic

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ExtensionRecord:
    """Installed extension registry row.

    Args:
        manifest: Extension manifest metadata.
        manifest_path: Registry-local manifest path.
        enabled: Whether the extension is active.
    """

    manifest: ExtensionManifest
    manifest_path: str
    enabled: bool = True

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible registry row.

        Returns:
            Dictionary representation for API responses.
        """
        data = asdict(self.manifest)
        data["manifest_path"] = self.manifest_path
        data["enabled"] = self.enabled
        return data


class ExtensionRegistry:
    """Discover and install extension manifests from a local registry root."""

    def __init__(self, root: str | Path | None = None) -> None:
        """Create an extension registry.

        Args:
            root: Optional registry root. Defaults to the user's Vetinari
                extension directory.
        """
        self.root = Path(root) if root is not None else get_user_dir() / "extensions"
        self._lock = threading.Lock()
        self.root.mkdir(parents=True, exist_ok=True)

    def discover_installed(self) -> list[ExtensionRecord]:
        """List installed extension manifests.

        Returns:
            Installed extension records sorted by name.
        """
        records: list[ExtensionRecord] = []
        for manifest_path in sorted(self.root.glob("*.json")):
            try:
                records.append(self._record_from_manifest_path(manifest_path))
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                logger.warning("Skipping invalid extension manifest %s: %s", manifest_path, exc)
        return sorted(records, key=lambda record: record.manifest.name)

    def install_manifest(self, manifest_path: str | Path) -> ExtensionRecord:
        """Install a manifest file into the local registry.

        Args:
            manifest_path: Source manifest path.

        Returns:
            Installed extension record.

        Raises:
            FileNotFoundError: If the manifest path does not exist.
            ValueError: If the manifest is invalid.
        """
        source = Path(manifest_path).resolve(strict=True)
        record = self._record_from_manifest_path(source)
        destination = self.root / f"{record.manifest.name}.json"
        with self._lock:
            shutil.copyfile(source, destination)
        return self._record_from_manifest_path(destination)

    def install_manifest_data(self, data: dict[str, Any]) -> ExtensionRecord:
        """Install a manifest supplied as JSON data.

        Args:
            data: Manifest dictionary.

        Returns:
            Installed extension record.

        Raises:
            ValueError: If the manifest is invalid.
        """
        manifest = self._manifest_from_data(data)
        destination = self.root / f"{manifest.name}.json"
        with self._lock:
            write_json_atomic(destination, asdict(manifest))
        return ExtensionRecord(manifest=manifest, manifest_path=str(destination))

    def _record_from_manifest_path(self, manifest_path: Path) -> ExtensionRecord:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest = self._manifest_from_data(data)
        return ExtensionRecord(manifest=manifest, manifest_path=str(manifest_path))

    @staticmethod
    def _manifest_from_data(data: dict[str, Any]) -> ExtensionManifest:
        name = str(data.get("name") or "").strip()
        version = str(data.get("version") or "").strip()
        entrypoint = str(data.get("entrypoint") or "").strip()
        if not name or "/" in name or "\\" in name:
            raise ValueError("extension name is required and must not contain path separators")
        if not version:
            raise ValueError("extension version is required")
        if not entrypoint:
            raise ValueError("extension entrypoint is required")
        raw_capabilities = data.get("capabilities", [])
        if not isinstance(raw_capabilities, list):
            raise ValueError("extension capabilities must be a list")
        return ExtensionManifest(
            name=name,
            version=version,
            entrypoint=entrypoint,
            capabilities=[str(item) for item in raw_capabilities],
        )


_REGISTRY: ExtensionRegistry | None = None
_REGISTRY_LOCK = threading.Lock()


def get_extension_registry() -> ExtensionRegistry:
    """Return the process-wide extension registry.

    Returns:
        Shared extension registry instance.
    """
    global _REGISTRY
    if _REGISTRY is not None:
        return _REGISTRY
    with _REGISTRY_LOCK:
        if _REGISTRY is None:
            _REGISTRY = ExtensionRegistry()
    return _REGISTRY


def reset_extension_registry() -> None:
    """Reset the process-wide extension registry for tests."""
    global _REGISTRY
    with _REGISTRY_LOCK:
        _REGISTRY = None


__all__ = [
    "ExtensionRecord",
    "ExtensionRegistry",
    "get_extension_registry",
    "reset_extension_registry",
]
