"""Check release source distributions for private workspace content."""

from __future__ import annotations

import argparse
import sys
import tarfile
from pathlib import Path

PRIVATE_TREES = (
    ".agents",
    ".ai-codex",
    ".claude",
    ".claire",
    ".codex",
    ".github",
    ".omc",
    ".playwright-mcp",
    "outputs",
    "private",
    "worktrees",
)


def check_manifest(path: Path) -> list[str]:
    """Return private trees missing from a MANIFEST.in prune list.

    Args:
        path: MANIFEST.in path to inspect.

    Returns:
        Private tree names without a matching prune directive.
    """
    text = path.read_text(encoding="utf-8")
    pruned = {
        parts[1].rstrip("/")
        for line in text.splitlines()
        if (parts := line.strip().split()) and len(parts) >= 2 and parts[0] == "prune"
    }
    return [tree for tree in PRIVATE_TREES if tree not in pruned]


def check_tarball(path: Path) -> list[str]:
    """Return private-tree paths found in a source distribution tarball.

    Args:
        path: A .tar.gz file, or a directory containing .tar.gz files.

    Returns:
        Tar member paths whose components include a private tree.
    """
    tarball = _resolve_tarball(path)
    matches: list[str] = []
    with tarfile.open(tarball, "r:gz") as archive:
        for member in archive.getmembers():
            parts = Path(member.name).parts
            if any(part in PRIVATE_TREES for part in parts):
                matches.append(member.name)
    return matches


def _resolve_tarball(path: Path) -> Path:
    if path.is_dir():
        tarballs = sorted(path.glob("*.tar.gz"), key=lambda item: item.stat().st_mtime, reverse=True)
        if not tarballs:
            msg = f"no .tar.gz files found in {path}"
            raise FileNotFoundError(msg)
        return tarballs[0]
    return path


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, help="MANIFEST.in file to check for private-tree prune directives.")
    parser.add_argument("--tarball", type=Path, help=".tar.gz sdist tarball, or a directory containing one.")
    args = parser.parse_args(argv)
    if args.manifest is None and args.tarball is None:
        parser.error("at least one of --manifest or --tarball is required")
    return args


def main(argv: list[str] | None = None) -> int:
    """Run the sdist private-content checker CLI.

    Args:
        argv: Optional argument vector for tests.

    Returns:
        Process exit code, where 0 means clean and 1 means failed.
    """
    try:
        args = _parse_args(argv)
        failed = False
        if args.manifest is not None:
            missing = check_manifest(args.manifest)
            if missing:
                print("Missing prune directives: " + ", ".join(missing))
                failed = True
        if args.tarball is not None:
            leaked = check_tarball(args.tarball)
            if leaked:
                print("Private paths found in sdist:")
                for path in leaked:
                    print(f"- {path}")
                failed = True
        return 1 if failed else 0
    except SystemExit:
        raise
    except Exception as exc:
        print(f"ERROR: checker failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
