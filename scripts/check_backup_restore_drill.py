"""Verify backup/restore drill manifests fail closed and accept good backups."""

from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from backup_restore_state import create_backup, restore_backup


@dataclass(frozen=True, slots=True)
class DrillFinding:
    code: str
    message: str
    path: str


def _is_relative_safe(value: str) -> bool:
    path = Path(value)
    return not path.is_absolute() and ".." not in path.parts


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_within(path: Path, root: Path) -> bool:
    resolved = path.resolve()
    resolved_root = root.resolve()
    return resolved == resolved_root or resolved_root in resolved.parents


def _expected_source(payload: dict[str, Any], label: str) -> Path | None:
    repo_root = payload.get("repo_root")
    user_root = payload.get("user_root")
    if not isinstance(repo_root, str) or not isinstance(user_root, str):
        return None
    roots = {
        "repo_state": Path(repo_root) / ".vetinari",
        "workbench_outputs": Path(repo_root) / "outputs" / "workbench",
        "release_outputs": Path(repo_root) / "outputs" / "release",
        "logs": Path(repo_root) / "logs",
        "user_config": Path(user_root) / "config.yaml",
    }
    return roots.get(label)


def _validate_jsonl(path: Path, item_path: str) -> list[DrillFinding]:
    findings: list[DrillFinding] = []
    data = path.read_text(encoding="utf-8")
    if data and not data.endswith("\n"):
        findings.append(DrillFinding("BRD013", "JSONL payload appears partially written", item_path))
    for line_number, line in enumerate(data.splitlines(), 1):
        if line.strip():
            try:
                json.loads(line)
            except json.JSONDecodeError as exc:
                findings.append(DrillFinding("BRD014", f"JSONL line {line_number} is corrupt: {exc}", item_path))
                break
    return findings


def _validate_payload_tree(path: Path, item_path: str) -> list[DrillFinding]:
    findings: list[DrillFinding] = []
    candidates = [path] if path.is_file() else list(path.rglob("*"))
    for candidate in candidates:
        if candidate.is_file() and candidate.suffix == ".jsonl":
            findings.extend(_validate_jsonl(candidate, item_path))
    return findings


def validate_backup_manifest(backup_dir: Path) -> list[DrillFinding]:
    """Return fail-closed findings for an unsafe or unrestorable backup."""
    findings: list[DrillFinding] = []
    manifest_path = backup_dir / "backup-manifest.json"
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return [DrillFinding("BRD001", "backup manifest is missing", str(manifest_path))]
    except (OSError, json.JSONDecodeError) as exc:
        return [DrillFinding("BRD002", f"backup manifest is unreadable or corrupt: {exc}", str(manifest_path))]

    if not isinstance(payload, dict):
        return [DrillFinding("BRD003", "backup manifest root must be an object", str(manifest_path))]
    if payload.get("schema_version") != 1:
        findings.append(DrillFinding("BRD004", "backup manifest schema_version must be 1", str(manifest_path)))
    if not isinstance(payload.get("repo_root"), str) or not isinstance(payload.get("user_root"), str):
        findings.append(DrillFinding("BRD011", "backup manifest requires repo_root and user_root", str(manifest_path)))
    items = payload.get("items")
    if not isinstance(items, list) or not items:
        findings.append(DrillFinding("BRD005", "backup manifest requires at least one item", str(manifest_path)))
        return findings

    backup_root = backup_dir.resolve()
    for index, item in enumerate(items):
        item_path = f"items[{index}]"
        if not isinstance(item, dict):
            findings.append(DrillFinding("BRD006", "backup item must be an object", item_path))
            continue
        rel = item.get("backup_relative")
        if not isinstance(rel, str) or not _is_relative_safe(rel):
            findings.append(
                DrillFinding("BRD007", "backup item path is absolute or traverses outside backup", item_path)
            )
            continue
        source = item.get("source")
        if not isinstance(source, str) or not Path(source).is_absolute():
            findings.append(DrillFinding("BRD008", "backup item source must be an absolute path", item_path))
            continue
        source_path = Path(source).resolve()
        label = item.get("label")
        if not isinstance(label, str):
            findings.append(DrillFinding("BRD012", "backup item label must be a string", item_path))
            continue
        expected_source = _expected_source(payload, label)
        if expected_source is None or source_path != expected_source.resolve():
            findings.append(DrillFinding("BRD012", "backup item source is not an allowed restore target", item_path))
        repo_root = Path(str(payload.get("repo_root", "")))
        user_root = Path(str(payload.get("user_root", "")))
        if not (_is_within(source_path, repo_root) or _is_within(source_path, user_root)):
            findings.append(DrillFinding("BRD012", "backup item source escapes trusted roots", item_path))
        exists = bool(item.get("exists"))
        payload_path = (backup_dir / rel).resolve()
        if backup_root not in (payload_path, *payload_path.parents):
            findings.append(DrillFinding("BRD009", "backup payload resolves outside backup root", item_path))
            continue
        if exists and not payload_path.exists():
            findings.append(DrillFinding("BRD010", "backup item is marked present but payload is missing", item_path))
            continue
        if exists:
            expected_sha = item.get("sha256")
            if payload_path.is_file() and (
                not isinstance(expected_sha, str) or _hash_file(payload_path) != expected_sha
            ):
                findings.append(DrillFinding("BRD015", "backup file payload checksum mismatch", item_path))
            findings.extend(_validate_payload_tree(payload_path, item_path))
    return findings


def _build_good_fixture(root: Path) -> Path:
    repo = root / "repo"
    user = root / "user"
    backup = root / "good-backup"
    (repo / ".vetinari").mkdir(parents=True)
    (repo / ".vetinari" / "state.json").write_text('{"state":"clean"}\n', encoding="utf-8")
    (repo / "outputs" / "workbench").mkdir(parents=True)
    (repo / "outputs" / "workbench" / "spine.jsonl").write_text("{}\n", encoding="utf-8")
    user.mkdir(parents=True)
    (user / "config.yaml").write_text("mode: local\n", encoding="utf-8")
    create_backup(backup, repo_root=repo, user_root=user)
    restore_backup(backup, dry_run=True, confirm_overwrite=True)
    return backup


def _build_bad_fixture(root: Path) -> Path:
    backup = root / "bad-backup"
    backup.mkdir(parents=True)
    payload: dict[str, Any] = {
        "schema_version": 1,
        "created_at": "2026-05-22T00:00:00Z",
        "repo_root": str((root / "repo").resolve()),
        "user_root": str((root / "user").resolve()),
        "dry_run": False,
        "items": [
            {
                "label": "repo_state",
                "source": str((root / "repo" / ".vetinari").resolve()),
                "backup_relative": "../escaped-state",
                "kind": "dir",
                "exists": True,
                "sha256": None,
            }
        ],
    }
    (backup / "backup-manifest.json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return backup


def _run_fixture(name: str) -> tuple[bool, list[DrillFinding], Path]:
    with tempfile.TemporaryDirectory(prefix="vetinari-backup-drill-") as tmp:
        root = Path(tmp)
        if name == "good-restorable-manifest":
            backup = _build_good_fixture(root)
            findings = validate_backup_manifest(backup)
            return not findings, findings, backup
        backup = _build_bad_fixture(root)
        findings = validate_backup_manifest(backup)
        return bool(findings), findings, backup


def _print_result(label: str, passed: bool, findings: list[DrillFinding]) -> None:
    status = "PASS" if passed else "FAIL"
    print(f"{status}: {label}")
    for finding in findings:
        print(f"{finding.code} {finding.path}: {finding.message}")


def run_self_test(fixture: str | None = None) -> int:
    names = [fixture] if fixture else ["bad-corrupt-manifest", "good-restorable-manifest"]
    all_passed = True
    for name in names:
        passed, findings, _backup = _run_fixture(name)
        all_passed = all_passed and passed
        _print_result(name, passed, findings)
    return 0 if all_passed else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backup-dir", type=Path)
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--fixture", choices=("bad-corrupt-manifest", "good-restorable-manifest"))
    args = parser.parse_args(argv)

    if args.self_test or args.fixture:
        return run_self_test(args.fixture)
    if args.backup_dir is not None:
        findings = validate_backup_manifest(args.backup_dir)
        _print_result(str(args.backup_dir), not findings, findings)
        return 0 if not findings else 1
    if args.strict:
        return run_self_test()
    parser.error("provide --self-test, --strict, --fixture, or --backup-dir")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
