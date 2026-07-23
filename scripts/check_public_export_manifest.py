#!/usr/bin/env python3
"""Verify a public export manifest without importing product code."""

from __future__ import annotations

import argparse
import hashlib
import json
import stat
import sys
from pathlib import Path
from typing import Any

from public_export_path_contract import PATH_CONTRACT_VERSION, validate_public_paths

MANIFEST_NAME = "PUBLIC_EXPORT_MANIFEST.json"
MANIFEST_VERSION = 2
DIGEST_SPEC = "sha256:path-length,path,git-mode,byte-length,content-sha256:v2"
API_ALLOWLIST_PATH = "scripts/_all_exports_allowlist.py"
API_CHECKER_PATH = "scripts/check_all_exports.py"
REQUIRED_PATHS = (
    "README.md",
    "LICENSE",
    "NOTICE",
    "pyproject.toml",
    "requirements.txt",
    ".github/workflows/public-export-contract.yml",
    ".github/workflows/public-export-provenance.yml",
    "config/support_matrix.yaml",
    API_ALLOWLIST_PATH,
    API_CHECKER_PATH,
    "scripts/check_public_export_manifest.py",
    "scripts/public_export_path_contract.py",
    "ui/svelte/package.json",
    "ui/svelte/package-lock.json",
)


def _is_hex(value: object, length: int) -> bool:
    return (
        isinstance(value, str) and len(value) == length and all(character in "0123456789abcdef" for character in value)
    )


def _public_paths(root: Path) -> tuple[list[str], list[str]]:
    raw_paths: list[str] = []
    errors: list[str] = []
    for full_path in root.rglob("*"):
        relative = full_path.relative_to(root)
        if ".git" in relative.parts or not (full_path.is_file() or full_path.is_symlink()):
            continue
        path = relative.as_posix()
        if full_path.is_symlink():
            errors.append(f"symlink is not allowed: {path}")
            continue
        raw_paths.append(path)
    path_errors = validate_public_paths(raw_paths)
    errors.extend(f"{path!r}: {message}" for path, message in path_errors)
    invalid_paths = {path for path, _message in path_errors}
    return sorted(path for path in raw_paths if path not in invalid_paths), errors


def _payload_digest(root: Path, paths: list[str], executable_paths: set[str]) -> str:
    digest = hashlib.sha256()
    digest.update(DIGEST_SPEC.encode("ascii"))
    digest.update(b"\0")
    for path in paths:
        if path == MANIFEST_NAME:
            continue
        data = (root / path).read_bytes()
        path_bytes = path.encode("utf-8")
        digest.update(len(path_bytes).to_bytes(8, "big"))
        digest.update(path_bytes)
        digest.update(b"100755" if path in executable_paths else b"100644")
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(hashlib.sha256(data).digest())
    return digest.hexdigest()


def _load_manifest(root: Path) -> tuple[dict[str, Any] | None, list[str]]:
    path = root / MANIFEST_NAME
    if not path.is_file() or path.is_symlink():
        return None, [f"required regular file is missing: {MANIFEST_NAME}"]
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return None, [f"manifest is unreadable or invalid JSON: {exc}"]
    if not isinstance(payload, dict):
        return None, ["manifest root must be an object"]
    return payload, []


def verify_public_export(
    root: Path,
    *,
    expected_source_commit: str | None = None,
    expected_source_tree: str | None = None,
    expected_payload_sha256: str | None = None,
    require_external_provenance: bool = False,
) -> list[str]:
    """Return fail-closed validation errors for one public export tree."""
    root = root.resolve()
    paths, errors = _public_paths(root)
    path_set = set(paths)
    for required_path in (*REQUIRED_PATHS, MANIFEST_NAME):
        if required_path not in path_set:
            errors.append(f"required regular file is missing: {required_path}")

    manifest, manifest_errors = _load_manifest(root)
    errors.extend(manifest_errors)
    if manifest is None:
        return errors

    if manifest.get("manifest_version") != MANIFEST_VERSION:
        errors.append(f"manifest_version must be {MANIFEST_VERSION}")
    if manifest.get("path_contract_version") != PATH_CONTRACT_VERSION:
        errors.append(f"path_contract_version must be {PATH_CONTRACT_VERSION}")
    if manifest.get("export_digest_spec") != DIGEST_SPEC:
        errors.append(f"export_digest_spec must be {DIGEST_SPEC!r}")
    if manifest.get("source_tracked_dirty") is not False:
        errors.append("source_tracked_dirty must be false")
    if not _is_hex(manifest.get("source_commit"), 40):
        errors.append("source_commit must be a full lowercase SHA-1 commit ID")
    if not _is_hex(manifest.get("source_tree"), 40):
        errors.append("source_tree must be a full lowercase SHA-1 tree ID")
    if not _is_hex(manifest.get("export_tree_sha256"), 64):
        errors.append("export_tree_sha256 must be a lowercase SHA-256 digest")
    if manifest.get("source_root_redacted") is not True or manifest.get("target_root_redacted") is not True:
        errors.append("private root redaction flags must be true")

    payload_paths = [path for path in paths if path != MANIFEST_NAME]
    executable_paths_raw = manifest.get("executable_paths")
    if (
        not isinstance(executable_paths_raw, list)
        or any(not isinstance(item, str) for item in executable_paths_raw)
        or len(set(executable_paths_raw)) != len(executable_paths_raw)
        or executable_paths_raw != sorted(executable_paths_raw)
    ):
        errors.append("executable_paths must be a sorted unique list of public paths")
        executable_paths: set[str] = set()
    else:
        executable_paths = set(executable_paths_raw)
        unknown_executable_paths = sorted(executable_paths - set(payload_paths))
        if unknown_executable_paths:
            errors.append(
                "executable_paths contains paths outside the payload: " + ", ".join(unknown_executable_paths[:10])
            )
    if sys.platform != "win32":
        actual_executable_paths = {
            path
            for path in payload_paths
            if (root / path).stat().st_mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        }
        if actual_executable_paths != executable_paths:
            errors.append("executable_paths does not match checkout file modes")
    copied_count = manifest.get("copied_count")
    if isinstance(copied_count, bool) or not isinstance(copied_count, int) or copied_count != len(payload_paths):
        errors.append(f"copied_count must equal actual payload file count {len(payload_paths)}")
    actual_payload_sha256 = _payload_digest(root, paths, executable_paths)
    if manifest.get("export_tree_sha256") != actual_payload_sha256:
        errors.append("export_tree_sha256 does not match the exported payload")

    for path_field, digest_field, expected_path in (
        ("api_allowlist_path", "api_allowlist_sha256", API_ALLOWLIST_PATH),
        ("api_checker_path", "api_checker_sha256", API_CHECKER_PATH),
    ):
        if manifest.get(path_field) != expected_path:
            errors.append(f"{path_field} must be {expected_path!r}")
            continue
        expected_digest = manifest.get(digest_field)
        if not _is_hex(expected_digest, 64):
            errors.append(f"{digest_field} must be a lowercase SHA-256 digest")
            continue
        asset = root / expected_path
        if asset.is_file() and hashlib.sha256(asset.read_bytes()).hexdigest() != expected_digest:
            errors.append(f"{digest_field} does not match {expected_path}")

    external_values = (expected_source_commit, expected_source_tree, expected_payload_sha256)
    if require_external_provenance and not all(external_values):
        errors.append("protected expected source commit, source tree, and payload digest are required")
    if expected_source_commit is not None and manifest.get("source_commit") != expected_source_commit:
        errors.append("source_commit does not match protected expected provenance")
    if expected_source_tree is not None and manifest.get("source_tree") != expected_source_tree:
        errors.append("source_tree does not match protected expected provenance")
    if expected_payload_sha256 is not None and actual_payload_sha256 != expected_payload_sha256:
        errors.append("payload digest does not match protected expected provenance")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--expected-source-commit")
    parser.add_argument("--expected-source-tree")
    parser.add_argument("--expected-payload-sha256")
    parser.add_argument("--require-external-provenance", action="store_true")
    args = parser.parse_args(argv)
    errors = verify_public_export(
        args.root,
        expected_source_commit=args.expected_source_commit,
        expected_source_tree=args.expected_source_tree,
        expected_payload_sha256=args.expected_payload_sha256,
        require_external_provenance=args.require_external_provenance,
    )
    if errors:
        print(f"Public export manifest verification failed with {len(errors)} error(s):", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print("Public export manifest verification passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
