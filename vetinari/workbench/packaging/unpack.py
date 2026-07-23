"""Selective unpacking for verified local AI bundles."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from vetinari.workbench.packaging.manifest import AIBundleComponentKind
from vetinari.workbench.packaging.verify import AIBundleVerifier, BundleVerificationError


class BundleUnpackError(Exception):
    """Raised when a bundle cannot be safely unpacked."""


@dataclass(frozen=True, slots=True)
class BundleUnpackRequest:
    """Inputs for selective component unpacking."""

    bundle_dir: Path
    destination: Path
    component_kinds: tuple[AIBundleComponentKind, ...]


@dataclass(frozen=True, slots=True)
class BundleUnpackResult:
    """Files written by a successful selective unpack."""

    destination: Path
    files_written: tuple[Path, ...]


class AIBundleUnpacker:
    """Verify first, then unpack only requested component groups."""

    def __init__(self, *, verifier: AIBundleVerifier | None = None) -> None:
        self._verifier = verifier if verifier is not None else AIBundleVerifier()

    def unpack(self, request: BundleUnpackRequest) -> BundleUnpackResult:
        """Execute the unpack operation.

        Returns:
            BundleUnpackResult value produced by unpack().

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        try:
            requested = _normalize_kinds(request.component_kinds)
            if not requested:
                raise BundleUnpackError("component_kinds must be non-empty")
            report = self._verifier.verify(request.bundle_dir)
            destination = Path(request.destination).resolve()
            if destination.exists() and any(destination.iterdir()):
                raise BundleUnpackError("unpack destination must not contain existing files")
            files_to_write: list[tuple[Path, bytes]] = []
            for component in report.manifest.components:
                if component.kind not in requested:
                    continue
                target = _contained_target(destination, component.unpack_path)
                source = (report.bundle_dir / component.blob_path).resolve()
                if not source.is_relative_to(report.bundle_dir):
                    raise BundleUnpackError(f"component {component.name!r} source escapes bundle root")
                files_to_write.append((target, source.read_bytes()))
            destination.mkdir(parents=True, exist_ok=True)
            written: list[Path] = []
            for target, data in files_to_write:
                if target.exists():
                    raise BundleUnpackError(f"refusing to overwrite existing unpack target: {target}")
                _ensure_parent_contained(destination, target.parent)
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("wb") as fh:
                    fh.write(data)
                    fh.flush()
                    os.fsync(fh.fileno())
                written.append(target)
            return BundleUnpackResult(destination=destination, files_written=tuple(written))
        except BundleUnpackError:
            raise
        except (BundleVerificationError, OSError, ValueError, TypeError) as exc:
            raise BundleUnpackError(str(exc)) from exc


def _normalize_kinds(values: tuple[AIBundleComponentKind, ...]) -> frozenset[AIBundleComponentKind]:
    normalized: set[AIBundleComponentKind] = {
        value if isinstance(value, AIBundleComponentKind) else AIBundleComponentKind(value) for value in values
    }
    return frozenset(normalized)


def _contained_target(root: Path, relative_path: str) -> Path:
    posix = PurePosixPath(relative_path)
    if posix.is_absolute() or ".." in posix.parts or not posix.parts:
        raise BundleUnpackError(f"unsafe unpack path: {relative_path!r}")
    first = posix.parts[0]
    if ":" in first or "\\" in relative_path or "\x00" in relative_path:
        raise BundleUnpackError(f"unsafe unpack path: {relative_path!r}")
    target = (root / Path(*posix.parts)).resolve()
    if not target.is_relative_to(root):
        raise BundleUnpackError(f"unpack path escapes destination: {relative_path!r}")
    return target


def _ensure_parent_contained(root: Path, parent: Path) -> None:
    current = parent
    while current != root and current != current.parent:
        if current.exists() and current.is_symlink():
            raise BundleUnpackError(f"unpack parent is a symlink: {current}")
        current = current.parent
    if not parent.resolve().is_relative_to(root):
        raise BundleUnpackError(f"unpack parent escapes destination: {parent}")


__all__ = ["AIBundleUnpacker", "BundleUnpackError", "BundleUnpackRequest", "BundleUnpackResult"]
