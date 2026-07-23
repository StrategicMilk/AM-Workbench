#!/usr/bin/env python3
"""Fail closed when built release artifacts are oversized or include internal-only trees."""

from __future__ import annotations

import argparse
import importlib.util
import sys
import tarfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).parent
_release_certifier: Any | None = None


def _load_release_certifier():
    spec = importlib.util.spec_from_file_location("release_certifier", SCRIPT_DIR / "release_certifier.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load release_certifier.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _get_release_certifier() -> Any:
    global _release_certifier
    if _release_certifier is None:
        _release_certifier = _load_release_certifier()
    return _release_certifier


DEFAULT_MAX_WHEEL_BYTES = 100 * 1024 * 1024
DEFAULT_MAX_SDIST_BYTES = 100 * 1024 * 1024
SAMPLE_LIMIT = 3


@dataclass(frozen=True)
class ArtifactSummary:
    path: Path
    kind: str
    size_bytes: int
    member_count: int


@dataclass(frozen=True)
class ArtifactFinding:
    path: Path
    check: str
    message: str

    def format(self) -> str:
        return f"{self.path}: {self.message}"


def _kind_for(path: Path) -> str | None:
    if path.name.endswith(".whl"):
        return "wheel"
    if path.name.endswith(".tar.gz") or path.name.endswith(".zip"):
        return "sdist"
    return None


def _discover_artifacts(dist_dir: Path) -> list[Path]:
    artifacts = sorted(dist_dir.glob("*.whl"))
    artifacts.extend(sorted(dist_dir.glob("*.tar.gz")))
    artifacts.extend(sorted(path for path in dist_dir.glob("*.zip") if path.name.endswith(".zip")))
    return artifacts


def _archive_members(path: Path) -> list[str]:
    if path.name.endswith(".whl") or path.name.endswith(".zip"):
        with zipfile.ZipFile(path) as archive:
            return [info.filename for info in archive.infolist() if info.filename]
    if path.name.endswith(".tar.gz"):
        with tarfile.open(path, mode="r:*") as archive:
            return [member.name for member in archive.getmembers() if member.name]
    raise ValueError(f"unsupported artifact type: {path.name}")


def _summarise_manifest_failures(path: Path, failures: tuple[Any, ...]) -> list[ArtifactFinding]:
    grouped: dict[str, list[str]] = {}
    for failure in failures:
        reason, _, detail = failure.message.partition(": ")
        grouped.setdefault(reason, [])
        if detail:
            grouped[reason].append(detail)

    findings: list[ArtifactFinding] = []
    for reason, details in grouped.items():
        unique_details = list(dict.fromkeys(details))
        if unique_details:
            samples = ", ".join(unique_details[:SAMPLE_LIMIT])
            suffix = f" ({len(unique_details)} entries; sample: {samples})"
        else:
            suffix = ""
        findings.append(ArtifactFinding(path=path, check="artifact-boundary", message=f"{reason}{suffix}"))
    return findings


def _certification_entries(kind: str, members: list[str]) -> list[str]:
    """Drop distribution-generated metadata that is not source/workspace content."""
    if kind != "sdist":
        return members

    filtered: list[str] = []
    for member in members:
        parts = [part for part in member.replace("\\", "/").split("/") if part]
        if len(parts) >= 2 and parts[1] == "vetinari.egg-info":
            continue
        filtered.append(member)
    return filtered


def inspect_artifacts(
    dist_dir: Path,
    *,
    max_wheel_bytes: int = DEFAULT_MAX_WHEEL_BYTES,
    max_sdist_bytes: int = DEFAULT_MAX_SDIST_BYTES,
) -> tuple[list[ArtifactSummary], list[ArtifactFinding]]:
    summaries: list[ArtifactSummary] = []
    findings: list[ArtifactFinding] = []

    if not dist_dir.exists():
        return summaries, [ArtifactFinding(dist_dir, "artifact-discovery", "artifact directory does not exist")]

    artifacts = _discover_artifacts(dist_dir)
    if not artifacts:
        return summaries, [ArtifactFinding(dist_dir, "artifact-discovery", "no wheel or sdist artifacts found")]

    has_wheel = any(_kind_for(path) == "wheel" for path in artifacts)
    has_sdist = any(_kind_for(path) == "sdist" for path in artifacts)
    if not has_wheel:
        findings.append(ArtifactFinding(dist_dir, "artifact-discovery", "release build is missing a wheel artifact"))
    if not has_sdist:
        findings.append(ArtifactFinding(dist_dir, "artifact-discovery", "release build is missing an sdist artifact"))

    for path in artifacts:
        kind = _kind_for(path)
        if kind is None:
            continue

        try:
            size_bytes = path.stat().st_size
        except OSError as exc:
            findings.append(ArtifactFinding(path, "artifact-size", f"could not stat artifact: {exc}"))
            continue

        try:
            members = _archive_members(path)
        except (OSError, tarfile.TarError, zipfile.BadZipFile, ValueError) as exc:
            findings.append(ArtifactFinding(path, "artifact-archive", f"could not inspect archive members: {exc}"))
            continue

        summaries.append(ArtifactSummary(path=path, kind=kind, size_bytes=size_bytes, member_count=len(members)))

        max_bytes = max_wheel_bytes if kind == "wheel" else max_sdist_bytes
        if size_bytes > max_bytes:
            findings.append(
                ArtifactFinding(
                    path=path,
                    check="artifact-size",
                    message=f"artifact size {size_bytes} bytes exceeds {kind} limit {max_bytes} bytes",
                )
            )

        manifest_result = _get_release_certifier().certify_release_manifest(_certification_entries(kind, members))
        findings.extend(_summarise_manifest_failures(path, manifest_result.failures))

    return summaries, findings


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Fail closed on oversized or polluted built release artifacts.")
    parser.add_argument("--dist-dir", type=Path, default=Path("dist"), help="Directory containing built artifacts.")
    parser.add_argument("--max-wheel-bytes", type=int, default=DEFAULT_MAX_WHEEL_BYTES)
    parser.add_argument("--max-sdist-bytes", type=int, default=DEFAULT_MAX_SDIST_BYTES)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    summaries, findings = inspect_artifacts(
        args.dist_dir,
        max_wheel_bytes=args.max_wheel_bytes,
        max_sdist_bytes=args.max_sdist_bytes,
    )

    if findings:
        print(f"Release artifact hygiene check failed with {len(findings)} finding(s):", file=sys.stderr)
        for finding in findings:
            print(f"- {finding.format()}", file=sys.stderr)
        return 1

    print("Release artifact hygiene check passed.")
    for summary in summaries:
        print(f"- {summary.path}: {summary.kind}, {summary.size_bytes} bytes, {summary.member_count} archive entries")
    return 0


if __name__ == "__main__":
    sys.exit(main())
