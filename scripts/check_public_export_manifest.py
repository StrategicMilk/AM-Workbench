#!/usr/bin/env python3
"""Verify a public export manifest without importing product code."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import stat
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from public_export_path_contract import PATH_CONTRACT_VERSION, validate_public_paths
from public_export_scope import load_public_export_scope

MANIFEST_NAME = "PUBLIC_EXPORT_MANIFEST.json"
MANIFEST_VERSION = 3
DIGEST_SPEC = "sha256:path-length,path,git-mode,byte-length,content-sha256:v2"
SCOPE_POLICY_PATH = "config/public_export_scope.toml"
TRUST_ROOT_DIGEST_SPEC = "sha256:path-length,path,git-mode,byte-length,content-sha256:v2"
TRUST_ROOT_PATHS = (
    ".github/workflows/public-export-provenance.yml",
    "config/public_export_scope.toml",
    "scripts/check_public_export_manifest.py",
    "scripts/public_export_path_contract.py",
    "scripts/public_export_scope.py",
)
TRUST_ROOT_LOCAL_MODULES = {
    "public_export_path_contract": "scripts/public_export_path_contract.py",
    "public_export_scope": "scripts/public_export_scope.py",
}
TRUST_ROOT_STDLIB_MODULES = frozenset({
    "__future__",
    "argparse",
    "ast",
    "dataclasses",
    "datetime",
    "hashlib",
    "json",
    "pathlib",
    "re",
    "stat",
    "sys",
    "tomllib",
    "typing",
    "unicodedata",
})
_TRUST_ROOT_FORBIDDEN_DYNAMIC_CALLS = frozenset({
    "__import__",
    "compile",
    "eval",
    "exec",
    "getattr",
    "import_module",
})
_TRUST_ROOT_FORBIDDEN_DYNAMIC_METHODS = frozenset({
    "__import__",
    "eval",
    "exec",
    "import_module",
    "popen",
    "run_module",
    "run_path",
    "system",
})
_TRUST_ROOT_WORKFLOW_LOCAL_REFERENCE = re.compile(
    r"(?<![A-Za-z0-9_.-])(?:\./)?(?P<checkout>trusted|candidate)/"
    r"(?P<path>[A-Za-z0-9_.\-/]+)"
)
_TRUST_ROOT_WORKFLOW_LOCAL_ACTION = re.compile(
    r"(?m)^\s*uses:\s*[\"']?(?:\./)?(?P<checkout>trusted|candidate)/(?P<path>[^@#\s\"']+)"
)
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
    "scripts/public_export_scope.py",
    SCOPE_POLICY_PATH,
    "ui/svelte/package.json",
    "ui/svelte/package-lock.json",
)
MANIFEST_KEYS = frozenset({
    "api_allowlist_path",
    "api_allowlist_sha256",
    "api_checker_path",
    "api_checker_sha256",
    "copied_count",
    "executable_paths",
    "export_digest_spec",
    "export_tree_sha256",
    "generated_at",
    "manifest_version",
    "path_contract_version",
    "scope_policy_path",
    "scope_policy_sha256",
    "scope_policy_version",
    "skipped_count",
    "skipped_summary",
    "source_commit",
    "source_root_redacted",
    "source_tracked_dirty",
    "source_tree",
    "target_root_redacted",
})


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


def _trust_root_digest(root: Path) -> tuple[str | None, list[str]]:
    digest = hashlib.sha256()
    digest.update(TRUST_ROOT_DIGEST_SPEC.encode("ascii"))
    digest.update(b"\0")
    errors: list[str] = []
    for path in TRUST_ROOT_PATHS:
        asset = root / path
        if not asset.is_file() or asset.is_symlink():
            errors.append(f"required trust-root file is missing or not regular: {path}")
            continue
        try:
            data = asset.read_bytes()
        except OSError as exc:
            errors.append(f"trust-root file is unreadable: {path}: {exc}")
            continue
        path_bytes = path.encode("utf-8")
        digest.update(len(path_bytes).to_bytes(8, "big"))
        digest.update(path_bytes)
        digest.update(_file_mode_class(asset).encode("ascii"))
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(hashlib.sha256(data).digest())
    return (None if errors else digest.hexdigest()), errors


def _file_mode_class(path: Path) -> str:
    return "100755" if path.stat().st_mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH) else "100644"


def _local_module_paths(root: Path, module: str) -> set[str]:
    """Return checkout-local files that can satisfy a dotted module name."""
    module_path = Path(*module.split("."))
    candidates = (
        root / module_path.with_suffix(".py"),
        root / module_path / "__init__.py",
        root / "scripts" / module_path.with_suffix(".py"),
        root / "scripts" / module_path / "__init__.py",
    )
    return {
        candidate.relative_to(root).as_posix()
        for candidate in candidates
        if candidate.is_file() and not candidate.is_symlink()
    }


def _trust_root_dependency_errors(root: Path) -> list[str]:
    """Prove the permanent five-file trust root has no undeclared local dependency."""
    errors: list[str] = []
    verifier_path = root / "scripts" / "check_public_export_manifest.py"
    try:
        verifier_tree = ast.parse(verifier_path.read_text(encoding="utf-8"), filename=str(verifier_path))
    except (OSError, UnicodeDecodeError, SyntaxError) as exc:
        return [f"trust-root verifier dependency contract is unreadable: {exc}"]
    declared_paths: tuple[str, ...] | None = None
    for node in verifier_tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if not any(isinstance(target, ast.Name) and target.id == "TRUST_ROOT_PATHS" for target in targets):
            continue
        try:
            raw_paths = ast.literal_eval(node.value)
        except (ValueError, TypeError):
            break
        if isinstance(raw_paths, tuple) and all(isinstance(item, str) for item in raw_paths):
            declared_paths = raw_paths
        break
    if declared_paths != TRUST_ROOT_PATHS:
        errors.append("candidate verifier must preserve the permanent five-file TRUST_ROOT_PATHS contract")

    allowed_local_paths = set(TRUST_ROOT_PATHS)
    for relative in TRUST_ROOT_PATHS:
        if not relative.endswith(".py"):
            continue
        source = root / relative
        try:
            tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        except (OSError, UnicodeDecodeError, SyntaxError) as exc:
            errors.append(f"trust-root Python source is unreadable: {relative}: {exc}")
            continue
        imported_modules: list[tuple[str, int]] = []
        imported_members: list[tuple[str, int]] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_modules.extend((alias.name, node.lineno) for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    errors.append(f"trust-root Python source uses a relative import: {relative}:{node.lineno}")
                    continue
                if node.module:
                    imported_modules.append((node.module, node.lineno))
                    imported_members.extend(
                        (f"{node.module}.{alias.name}", node.lineno) for alias in node.names if alias.name != "*"
                    )
            elif isinstance(node, ast.Call):
                function = node.func
                if (isinstance(function, ast.Name) and function.id in _TRUST_ROOT_FORBIDDEN_DYNAMIC_CALLS) or (
                    isinstance(function, ast.Attribute) and function.attr in _TRUST_ROOT_FORBIDDEN_DYNAMIC_METHODS
                ):
                    errors.append(f"trust-root Python source uses dynamic code execution: {relative}:{node.lineno}")
        for module, line in imported_modules:
            local_paths = _local_module_paths(root, module)
            if local_paths:
                if not local_paths.issubset(allowed_local_paths):
                    errors.append(
                        f"trust-root Python source imports undeclared local module {module!r}: {relative}:{line}"
                    )
            elif module not in TRUST_ROOT_STDLIB_MODULES:
                errors.append(f"trust-root Python source imports undeclared module {module!r}: {relative}:{line}")
        for module, line in imported_members:
            local_paths = _local_module_paths(root, module)
            if local_paths and not local_paths.issubset(allowed_local_paths):
                errors.append(f"trust-root Python source imports undeclared local module {module!r}: {relative}:{line}")
    workflow_path = root / ".github" / "workflows" / "public-export-provenance.yml"
    try:
        workflow_text = workflow_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        errors.append(f"trust-root provenance workflow is unreadable: {exc}")
        return errors
    if re.search(r"(?mi)^\s*working-directory\s*:", workflow_text):
        errors.append("trust-root provenance workflow must not change its working directory")
    if re.search(r"(?mi)(?:^|[|>&;]\s*)(?:cd|pushd)\s+(?:trusted|candidate)(?:\s|$)", workflow_text):
        errors.append("trust-root provenance workflow must not change into a checkout directory")
    for match in _TRUST_ROOT_WORKFLOW_LOCAL_ACTION.finditer(workflow_text):
        errors.append(
            "trust-root provenance workflow must not use checkout-local actions: "
            f"{match.group('checkout')}/{match.group('path')}"
        )
    permitted_workflow_references = {("trusted", "scripts/check_public_export_manifest.py")}
    for match in _TRUST_ROOT_WORKFLOW_LOCAL_REFERENCE.finditer(workflow_text):
        reference = (match.group("checkout"), match.group("path").rstrip(".,;:"))
        if reference not in permitted_workflow_references:
            errors.append(
                f"trust-root provenance workflow references undeclared local dependency: {reference[0]}/{reference[1]}"
            )
    return errors


def verify_trust_root_transition(
    candidate_root: Path,
    *,
    trusted_base_root: Path,
    expected_trust_root_sha256: str | None,
    expected_source_commit: str | None = None,
    expected_source_tree: str | None = None,
    expected_payload_sha256: str | None = None,
    expected_manifest_sha256: str | None = None,
) -> list[str]:
    """Verify an externally authorized, self-consistent trust-root transition."""
    candidate_root = candidate_root.resolve()
    trusted_base_root = trusted_base_root.resolve()
    candidate_paths, errors = _public_paths(candidate_root)
    trusted_paths, trusted_errors = _public_paths(trusted_base_root)
    errors.extend(f"trusted base: {error}" for error in trusted_errors)
    errors.extend(_trust_root_dependency_errors(candidate_root))
    trust_paths = set(TRUST_ROOT_PATHS)
    transition_metadata = {MANIFEST_NAME}
    candidate_non_trust = set(candidate_paths) - trust_paths - transition_metadata
    trusted_non_trust = set(trusted_paths) - trust_paths - transition_metadata
    added = sorted(candidate_non_trust - trusted_non_trust)
    removed = sorted(trusted_non_trust - candidate_non_trust)
    if added:
        errors.append("trust-root transition adds non-trust-root paths: " + ", ".join(added[:20]))
    if removed:
        errors.append("trust-root transition removes non-trust-root paths: " + ", ".join(removed[:20]))
    for path in sorted(candidate_non_trust & trusted_non_trust):
        candidate = candidate_root / path
        trusted = trusted_base_root / path
        try:
            if candidate.read_bytes() != trusted.read_bytes() or _file_mode_class(candidate) != _file_mode_class(
                trusted
            ):
                errors.append(f"trust-root transition modifies non-trust-root path: {path}")
        except OSError as exc:
            errors.append(f"trust-root transition could not compare {path}: {exc}")
    actual_digest, digest_errors = _trust_root_digest(candidate_root)
    errors.extend(digest_errors)
    if not _is_hex(expected_trust_root_sha256, 64):
        errors.append("protected expected next trust-root digest is required")
    elif actual_digest != expected_trust_root_sha256:
        errors.append("candidate trust-root digest does not match protected expected transition")
    errors.extend(
        verify_public_export(
            candidate_root,
            expected_source_commit=expected_source_commit,
            expected_source_tree=expected_source_tree,
            expected_payload_sha256=expected_payload_sha256,
            expected_manifest_sha256=expected_manifest_sha256,
            require_external_provenance=True,
            trusted_scope_path=candidate_root / SCOPE_POLICY_PATH,
            # The candidate trust root is authorized above by a protected,
            # externally supplied digest. Passing it here activates the
            # ordinary verifier's full manifest/path/provenance checks without
            # comparing the authorized next root to the old root.
            trusted_base_root=candidate_root,
        )
    )
    return errors


def _load_manifest(root: Path) -> tuple[dict[str, Any] | None, list[str]]:
    path = root / MANIFEST_NAME
    if not path.is_file() or path.is_symlink():
        return None, [f"required regular file is missing: {MANIFEST_NAME}"]
    if _file_mode_class(path) != "100644":
        return None, [f"{MANIFEST_NAME} must be a non-executable regular file"]
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
    expected_manifest_sha256: str | None = None,
    require_external_provenance: bool = False,
    trusted_scope_path: Path | None = None,
    trusted_base_root: Path | None = None,
) -> list[str]:
    """Return fail-closed validation errors for one public export tree."""
    root = root.resolve()
    paths, errors = _public_paths(root)
    path_set = set(paths)
    errors.extend(_trust_root_dependency_errors(root))
    if require_external_provenance and trusted_base_root is None:
        errors.append("trusted base root is required for protected external provenance")
    scope_path = trusted_scope_path or (Path(__file__).resolve().parents[1] / SCOPE_POLICY_PATH)
    try:
        trusted_scope = load_public_export_scope(scope_path)
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        return [*errors, f"trusted public-export scope is unavailable or invalid: {exc}"]
    errors.extend(f"{path!r}: {message}" for path, message in trusted_scope.public_path_errors(paths))
    if trusted_base_root is not None:
        trusted_digest, trusted_errors = _trust_root_digest(trusted_base_root.resolve())
        candidate_digest, candidate_errors = _trust_root_digest(root)
        errors.extend(f"trusted base: {error}" for error in trusted_errors)
        errors.extend(candidate_errors)
        if trusted_digest is not None and candidate_digest != trusted_digest:
            errors.append("ordinary payload must not modify protected trust-root files")
    for required_path in (*REQUIRED_PATHS, MANIFEST_NAME):
        if required_path not in path_set:
            errors.append(f"required regular file is missing: {required_path}")

    manifest, manifest_errors = _load_manifest(root)
    errors.extend(manifest_errors)
    if manifest is None:
        return errors
    actual_manifest_sha256 = hashlib.sha256((root / MANIFEST_NAME).read_bytes()).hexdigest()

    unknown_manifest_keys = sorted(set(manifest) - MANIFEST_KEYS)
    missing_manifest_keys = sorted(MANIFEST_KEYS - set(manifest))
    if unknown_manifest_keys:
        errors.append("manifest has unexpected keys: " + ", ".join(unknown_manifest_keys))
    if missing_manifest_keys:
        errors.append("manifest is missing required keys: " + ", ".join(missing_manifest_keys))
    if manifest.get("manifest_version") != MANIFEST_VERSION:
        errors.append(f"manifest_version must be {MANIFEST_VERSION}")
    if manifest.get("path_contract_version") != PATH_CONTRACT_VERSION:
        errors.append(f"path_contract_version must be {PATH_CONTRACT_VERSION}")
    if manifest.get("scope_policy_path") != SCOPE_POLICY_PATH:
        errors.append(f"scope_policy_path must be {SCOPE_POLICY_PATH!r}")
    if manifest.get("scope_policy_version") != trusted_scope.version:
        errors.append(f"scope_policy_version must be {trusted_scope.version}")
    if manifest.get("scope_policy_sha256") != trusted_scope.sha256:
        errors.append("scope_policy_sha256 does not match the protected-base scope policy")
    candidate_scope = root / SCOPE_POLICY_PATH
    if candidate_scope.is_file() and hashlib.sha256(candidate_scope.read_bytes()).hexdigest() != trusted_scope.sha256:
        errors.append("candidate scope policy bytes do not match the protected-base scope policy")
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
    skipped_count = manifest.get("skipped_count")
    skipped_summary = manifest.get("skipped_summary")
    if isinstance(skipped_count, bool) or not isinstance(skipped_count, int) or skipped_count < 0:
        errors.append("skipped_count must be a non-negative integer")
    if not isinstance(skipped_summary, dict) or set(skipped_summary) != {"by_check"}:
        errors.append("skipped_summary must contain exactly the by_check object")
    else:
        by_check = skipped_summary.get("by_check")
        if (
            not isinstance(by_check, dict)
            or any(not isinstance(key, str) or not key for key in by_check)
            or any(isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in by_check.values())
        ):
            errors.append("skipped_summary.by_check must map non-empty check names to positive integers")
        elif (
            isinstance(skipped_count, int)
            and not isinstance(skipped_count, bool)
            and sum(by_check.values()) != skipped_count
        ):
            errors.append("skipped_summary.by_check counts must sum to skipped_count")
    generated_at = manifest.get("generated_at")
    try:
        parsed_generated_at = datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
        if parsed_generated_at.tzinfo is None:
            raise ValueError
    except (AttributeError, TypeError, ValueError):
        errors.append("generated_at must be an ISO-8601 timestamp with timezone")
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

    external_values = (
        expected_source_commit,
        expected_source_tree,
        expected_payload_sha256,
        expected_manifest_sha256,
    )
    if require_external_provenance and not all(external_values):
        errors.append("protected expected source commit, source tree, payload digest, and manifest digest are required")
    if expected_source_commit is not None and manifest.get("source_commit") != expected_source_commit:
        errors.append("source_commit does not match protected expected provenance")
    if expected_source_tree is not None and manifest.get("source_tree") != expected_source_tree:
        errors.append("source_tree does not match protected expected provenance")
    if expected_payload_sha256 is not None and actual_payload_sha256 != expected_payload_sha256:
        errors.append("payload digest does not match protected expected provenance")
    if expected_manifest_sha256 is not None and actual_manifest_sha256 != expected_manifest_sha256:
        errors.append("manifest digest does not match protected expected provenance")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--expected-source-commit")
    parser.add_argument("--expected-source-tree")
    parser.add_argument("--expected-payload-sha256")
    parser.add_argument("--expected-manifest-sha256")
    parser.add_argument("--trusted-base-root", type=Path)
    parser.add_argument("--trust-root-mode", choices=("payload", "transition"), default="payload")
    parser.add_argument("--expected-next-trust-root-sha256")
    parser.add_argument("--require-external-provenance", action="store_true")
    args = parser.parse_args(argv)
    if args.trust_root_mode == "transition":
        if args.trusted_base_root is None:
            errors = ["--trusted-base-root is required for trust-root transitions"]
        else:
            errors = verify_trust_root_transition(
                args.root,
                trusted_base_root=args.trusted_base_root,
                expected_trust_root_sha256=args.expected_next_trust_root_sha256,
                expected_source_commit=args.expected_source_commit,
                expected_source_tree=args.expected_source_tree,
                expected_payload_sha256=args.expected_payload_sha256,
                expected_manifest_sha256=args.expected_manifest_sha256,
            )
    else:
        errors = verify_public_export(
            args.root,
            expected_source_commit=args.expected_source_commit,
            expected_source_tree=args.expected_source_tree,
            expected_payload_sha256=args.expected_payload_sha256,
            expected_manifest_sha256=args.expected_manifest_sha256,
            require_external_provenance=args.require_external_provenance,
            trusted_base_root=args.trusted_base_root,
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
