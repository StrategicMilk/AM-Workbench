#!/usr/bin/env python3
"""Verify checked release claim ledgers and their evidence digests."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from vetinari.release.claims_ledger import ClaimsLedger

ROOT = Path(__file__).resolve().parents[1]
SAFE_RELEASE_VERSION = re.compile(r"^[A-Za-z0-9._-]+$")


def iter_ledgers(release_root: Path) -> list[Path]:
    """Return versioned release ledger files under ``release_root``."""
    if not release_root.exists():
        return []
    return sorted(path for path in release_root.glob("*/ledger.jsonl") if path.is_file())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release-root", type=Path, default=ROOT / "outputs" / "release")
    parser.add_argument("--repo-root", type=Path, default=ROOT)
    parser.add_argument(
        "--require-ledger",
        action="store_true",
        help="Fail when no certified release ledger exists (for an actual pre-release boundary).",
    )
    parser.add_argument(
        "--release-version",
        help="Require and verify the ledger for this exact release version (for example 0.9.0).",
    )
    args = parser.parse_args(argv)

    release_root = args.release_root if args.release_root.is_absolute() else args.repo_root / args.release_root
    repo_root = args.repo_root.resolve()
    if args.release_version:
        if SAFE_RELEASE_VERSION.fullmatch(args.release_version) is None or args.release_version in {".", ".."}:
            print(
                f"Release claims ledger check failed: unsafe release version {args.release_version!r}", file=sys.stderr
            )
            return 1
        expected = release_root.resolve() / args.release_version / "ledger.jsonl"
        ledgers = [expected] if expected.is_file() else []
    else:
        ledgers = iter_ledgers(release_root.resolve())
    if not ledgers:
        if args.require_ledger or args.release_version:
            expected_note = f" for release {args.release_version}" if args.release_version else ""
            print(
                f"Release claims ledger check failed: no ledger.jsonl file{expected_note} under {release_root}",
                file=sys.stderr,
            )
            return 1
        print(
            f"No certified release claims ledger is checked in under {release_root}; integrity check is not applicable."
        )
        return 0

    failures: list[str] = []
    for ledger in ledgers:
        report = ClaimsLedger.verify_all(ledger, repo_root=repo_root)
        if report.passed:
            print(f"{ledger}: passed ({report.ok}/{report.total})")
            continue
        failures.append(f"{ledger}: {report.ok}/{report.total} passed")
        failures.extend(f"  - {failure}" for failure in report.failures)

    if failures:
        print("Release claims ledger check failed:", file=sys.stderr)
        for failure in failures:
            print(failure, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
