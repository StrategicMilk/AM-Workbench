#!/usr/bin/env python3
"""Verify the vendored llama.cpp tree against its immutable upstream revision."""

from __future__ import annotations

import argparse
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

import tomllib

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_VENDOR = ROOT / "crates" / "amw-engine" / "vendor" / "llama.cpp"
DEFAULT_MANIFEST = ROOT / "crates" / "amw-engine" / "Cargo.toml"
DEFAULT_MAX_FILES = 5_000
DEFAULT_MAX_BYTES = 256 * 1024 * 1024
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")


class VendorVerificationError(ValueError):
    """Raised when vendored source provenance is incomplete or inconsistent."""


@dataclass(frozen=True)
class VendorTreeEvidence:
    """Deterministic identity and bounded size evidence for one source tree."""

    revision: str
    normalized_sha256: str
    file_count: int
    total_bytes: int


def normalized_tree_digest(
    root: Path,
    *,
    max_files: int = DEFAULT_MAX_FILES,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> tuple[str, int, int]:
    """Hash a source tree deterministically after CRLF normalization.

    Args:
        root: Source-tree root to hash.
        max_files: Maximum regular files admitted to the tree.
        max_bytes: Maximum aggregate pre-normalization bytes admitted.

    Returns:
        Normalized SHA-256 digest, file count, and aggregate source bytes.

    Raises:
        VendorVerificationError: If the tree is missing, unsafe, or exceeds a bound.
    """
    if not root.is_dir():
        raise VendorVerificationError(f"source tree is missing: {root}")
    paths = sorted(root.rglob("*"), key=lambda path: path.relative_to(root).as_posix())
    if any(path.is_symlink() for path in paths):
        raise VendorVerificationError(f"source tree contains a symbolic link: {root}")
    files = [path for path in paths if path.is_file()]
    if not files:
        raise VendorVerificationError(f"source tree contains no files: {root}")
    if len(files) > max_files:
        raise VendorVerificationError(f"source tree contains {len(files)} files; limit is {max_files}")
    digest = hashlib.sha256()
    total_bytes = 0
    for path in files:
        data = path.read_bytes()
        total_bytes += len(data)
        if total_bytes > max_bytes:
            raise VendorVerificationError(f"source tree exceeds the {max_bytes}-byte limit")
        relative_path = path.relative_to(root).as_posix().encode("utf-8")
        normalized_content = data.replace(b"\r\n", b"\n")
        digest.update(relative_path)
        digest.update(b"\0")
        digest.update(hashlib.sha256(normalized_content).digest())
    return digest.hexdigest(), len(files), total_bytes


def verify_vendor_tree(
    vendor_root: Path,
    upstream_root: Path,
    manifest_path: Path,
    *,
    revision: str,
    expected_digest: str,
) -> VendorTreeEvidence:
    """Prove that metadata, vendored bytes, and fetched upstream bytes agree.

    Args:
        vendor_root: Checked-in llama.cpp source tree.
        upstream_root: Fresh source tree fetched from the immutable revision.
        manifest_path: Engine Cargo manifest carrying the recorded revision.
        revision: Expected full upstream Git commit.
        expected_digest: Pinned normalized tree SHA-256.

    Returns:
        Verified vendor-tree identity and size evidence.

    Raises:
        VendorVerificationError: If any provenance surface disagrees.
    """
    if _COMMIT_RE.fullmatch(revision) is None:
        raise VendorVerificationError("llama.cpp revision must be a lowercase full Git commit")
    if _SHA256_RE.fullmatch(expected_digest) is None:
        raise VendorVerificationError("expected llama.cpp tree digest must be a lowercase SHA-256")
    try:
        manifest = tomllib.loads(manifest_path.read_text(encoding="utf-8"))
        recorded_revision = manifest["package"]["metadata"]["am_engine"]["libllama_rev"]
    except (OSError, KeyError, TypeError, tomllib.TOMLDecodeError) as exc:
        raise VendorVerificationError(f"engine manifest provenance is unreadable: {manifest_path}") from exc
    if recorded_revision != revision:
        raise VendorVerificationError(
            f"engine manifest revision {recorded_revision!r} does not match requested revision {revision!r}"
        )
    vendor_digest, vendor_file_count, vendor_bytes = normalized_tree_digest(vendor_root)
    upstream_digest, upstream_file_count, _upstream_bytes = normalized_tree_digest(upstream_root)
    if vendor_digest != expected_digest:
        raise VendorVerificationError(
            f"vendored llama.cpp digest {vendor_digest} does not match pinned digest {expected_digest}"
        )
    if upstream_digest != expected_digest:
        raise VendorVerificationError(
            f"upstream llama.cpp digest {upstream_digest} does not match pinned digest {expected_digest}"
        )
    if upstream_file_count != vendor_file_count:
        raise VendorVerificationError("vendored and upstream llama.cpp file counts disagree")
    return VendorTreeEvidence(revision, vendor_digest, vendor_file_count, vendor_bytes)


def main(argv: list[str] | None = None) -> int:
    """Run vendored llama.cpp provenance verification.

    Args:
        argv: Optional command-line arguments.

    Returns:
        Zero after complete verification; invalid evidence raises an error.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vendor", type=Path, default=DEFAULT_VENDOR)
    parser.add_argument("--upstream", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--expected-digest", required=True)
    args = parser.parse_args(argv)
    evidence = verify_vendor_tree(
        args.vendor,
        args.upstream,
        args.manifest,
        revision=args.revision,
        expected_digest=args.expected_digest,
    )
    print(
        f"verified llama.cpp {evidence.revision}: "
        f"{evidence.normalized_sha256} ({evidence.file_count} files, {evidence.total_bytes} bytes)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
