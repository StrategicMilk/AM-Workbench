"""Back up and restore Vetinari operator-owned runtime state.

The script is intentionally small and filesystem-only: it copies known runtime
state directories into a timestamped backup with a manifest, then restores from
that manifest when explicitly requested.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import shutil
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from path_guard import REPO_ROOT, contain

logger = logging.getLogger(__name__)

try:
    from scripts.session_redaction import redact_text
except ImportError:  # pragma: no cover - direct script execution
    from session_redaction import redact_text

StateKind = Literal["file", "dir"]
REQUIRED_MANIFEST_KEYS = {"schema_version", "created_at", "repo_root", "user_root", "items"}
REQUIRED_ITEM_KEYS = {"label", "source", "backup_relative", "kind", "exists", "sha256"}


@dataclass(frozen=True)
class StateItem:
    label: str
    source: str
    backup_relative: str
    kind: StateKind
    exists: bool
    sha256: str | None = None


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _copy_item(source: Path, destination: Path) -> None:
    source = contain(source, REPO_ROOT) if source.exists() and str(source).startswith(str(REPO_ROOT)) else source
    destination = contain(destination, REPO_ROOT) if str(destination).startswith(str(REPO_ROOT)) else destination
    if source.is_dir():
        if destination.exists():
            shutil.rmtree(destination)
        shutil.copytree(source, destination)
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def _copy_user_config(source: Path, destination: Path) -> str:
    sanitized = redact_text(source.read_text(encoding="utf-8", errors="replace"))
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(sanitized, encoding="utf-8")
    try:
        destination.parent.chmod(0o700)
        destination.chmod(0o600)
    except OSError as exc:
        logger.debug("could not tighten backup config permissions for %s: %s", destination, exc)
    return _hash_file(destination)


def _state_sources(repo_root: Path, user_root: Path) -> list[tuple[str, Path, str]]:
    return [
        ("repo_state", repo_root / ".vetinari", "repo/.vetinari"),
        ("workbench_outputs", repo_root / "outputs" / "workbench", "repo/outputs/workbench"),
        ("release_outputs", repo_root / "outputs" / "release", "repo/outputs/release"),
        ("logs", repo_root / "logs", "repo/logs"),
        ("user_config", user_root / "config.yaml", "user/config.yaml"),
    ]


def create_backup(
    destination: Path,
    *,
    repo_root: Path,
    user_root: Path,
    dry_run: bool = False,
) -> dict[str, object]:
    """Create a manifest-backed backup of known runtime state."""
    destination = destination.resolve()
    items: list[StateItem] = []
    for label, source, rel in _state_sources(repo_root.resolve(), user_root.resolve()):
        exists = source.exists()
        kind: StateKind = "dir" if source.is_dir() else "file"
        sha256 = _hash_file(source) if exists and source.is_file() else None
        if label == "user_config" and exists and source.is_file() and not dry_run:
            sha256 = _copy_user_config(source, destination / rel)
        item = StateItem(
            label=label,
            source=str(source.resolve()),
            backup_relative=rel,
            kind=kind,
            exists=exists,
            sha256=sha256,
        )
        items.append(item)
        if exists and not dry_run:
            if label == "user_config" and source.is_file():
                continue
            _copy_item(source, destination / rel)

    manifest = {
        "schema_version": 1,
        "created_at": datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "repo_root": str(repo_root.resolve()),
        "user_root": str(user_root.resolve()),
        "dry_run": dry_run,
        "items": [asdict(item) for item in items],
    }
    if not dry_run:
        destination.mkdir(parents=True, exist_ok=True)
        (destination / "backup-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


def _validate_restore_manifest(backup_dir: Path, manifest: dict[str, object]) -> list[StateItem]:
    missing_manifest_keys = sorted(REQUIRED_MANIFEST_KEYS - set(manifest))
    if missing_manifest_keys:
        raise RuntimeError(f"backup manifest missing required keys: {', '.join(missing_manifest_keys)}")
    if manifest.get("schema_version") != 1:
        raise RuntimeError(f"unsupported backup manifest schema_version: {manifest.get('schema_version')!r}")

    raw_items = manifest.get("items")
    if not isinstance(raw_items, list):
        raise RuntimeError("backup manifest items must be a list")

    items: list[StateItem] = []
    for index, raw_item in enumerate(raw_items):
        if not isinstance(raw_item, dict):
            raise RuntimeError(f"backup manifest item {index} must be an object")
        missing_item_keys = sorted(REQUIRED_ITEM_KEYS - set(raw_item))
        if missing_item_keys:
            raise RuntimeError(f"backup manifest item {index} missing required keys: {', '.join(missing_item_keys)}")
        item = StateItem(**raw_item)
        if item.kind not in {"file", "dir"}:
            raise RuntimeError(f"backup manifest item {item.label} has invalid kind: {item.kind}")
        if not item.exists:
            items.append(item)
            continue

        backup_source = backup_dir / item.backup_relative
        if not backup_source.exists():
            raise RuntimeError(f"backup payload missing for {item.label}: {backup_source}")
        if item.kind == "dir" and not backup_source.is_dir():
            raise RuntimeError(f"backup payload for {item.label} is not a directory: {backup_source}")
        if item.kind == "file":
            if not backup_source.is_file():
                raise RuntimeError(f"backup payload for {item.label} is not a file: {backup_source}")
            if item.sha256 and _hash_file(backup_source) != item.sha256:
                raise RuntimeError(f"backup payload checksum mismatch before restore: {backup_source}")
        items.append(item)
    return items


def restore_backup(backup_dir: Path, *, dry_run: bool = False, confirm_overwrite: bool = False) -> dict[str, object]:
    """Restore runtime state from a backup manifest."""
    manifest_path = backup_dir / "backup-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    items = _validate_restore_manifest(backup_dir, manifest)
    restored: list[str] = []
    for item in items:
        if not item.exists:
            continue
        source = backup_dir / item.backup_relative
        destination = Path(item.source)
        if destination.exists() and not confirm_overwrite:
            raise RuntimeError(f"refusing to overwrite existing state without --yes: {destination}")
        restored.append(str(destination))
        if dry_run:
            continue
        if destination.exists():
            if destination.is_dir():
                shutil.rmtree(destination)
            else:
                destination.unlink()
        _copy_item(source, destination)
        if item.sha256 and _hash_file(destination) != item.sha256:
            raise RuntimeError(f"restored checksum mismatch: {destination}")
    return {"restored": restored, "dry_run": dry_run}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    backup = sub.add_parser("backup", help="Copy runtime state to a manifest-backed backup directory")
    backup.add_argument("destination", type=Path)
    backup.add_argument("--repo-root", type=Path, default=Path.cwd())
    backup.add_argument("--user-root", type=Path, default=Path.home() / ".vetinari")
    backup.add_argument("--dry-run", action="store_true")
    restore = sub.add_parser("restore", help="Restore runtime state from backup-manifest.json")
    restore.add_argument("backup_dir", type=Path)
    restore.add_argument("--dry-run", action="store_true")
    restore.add_argument("--yes", action="store_true", help="Allow overwriting existing state paths")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "backup":
        manifest = create_backup(
            args.destination, repo_root=args.repo_root, user_root=args.user_root, dry_run=args.dry_run
        )
        print(json.dumps(manifest, indent=2))
        return 0
    result = restore_backup(args.backup_dir, dry_run=args.dry_run, confirm_overwrite=args.yes)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
