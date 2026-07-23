"""Pre-release gate: verify every CHANGELOG claim has a ledger citation.

Usage::

    python scripts/pre_release_gate.py \\
        --changelog CHANGELOG.md \\
        --ledger outputs/release/0.7.0/ledger.jsonl

Exit codes:
    0 — All substantive claims under the latest CHANGELOG version heading
        carry at least one ``[ledger:<id>]`` tag and every referenced id
        resolves in the ledger.
    1 — Usage or argument error (e.g. missing --changelog flag).
    2 — One or more claims are unresolved: missing ledger tags, missing
        ledger file, or tag ids not found in the ledger.

Convention note
~~~~~~~~~~~~~~~
The ``[ledger:<id>]`` inline citation convention is in effect starting at the
version immediately after the **latest** version recorded in CHANGELOG.md at
the time ``scripts/pre_release_gate.py`` was introduced.  For the transition
period, entries in versions that predate the convention are NOT checked by
this gate; only the *next* (unreleased or newly cut) version entries are
expected to carry ledger citations.

The gate is intentionally fail-closed: a missing ledger file causes exit 2,
not a silent pass.  This prevents the "unavailable-dependency pass-through"
anti-pattern.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# Regex matching inline ledger citation: [ledger:some-claim-id]
# Claim ids may contain letters, digits, hyphens, underscores, and dots.
_LEDGER_TAG_RE = re.compile(r"\[ledger:([A-Za-z0-9_\-\.]+)\]")

# Lines that introduce a version heading in CHANGELOG.md.
# e.g. "## [0.7.0] - 2026-04-24" or "## [Unreleased]"
_VERSION_HEADING_RE = re.compile(r"^## \[([^\]]+)\]")

# Lines we consider non-substantive (structural / boilerplate).
# ### sub-section headers, blank lines, and lines with only punctuation/dashes.
_NON_SUBSTANTIVE_RE = re.compile(
    r"^(###\s|---+\s*$|\s*$|"  # sub-headings, horizontal rules, blanks
    r"\[Unreleased\]:|"  # comparison URL lines at end of file
    r"\[\d+\.\d+\.\d+\]:"  # comparison URL lines at end of file
    r")"
)


def _load_ledger_ids(ledger_path: Path) -> set[str] | None:
    """Load all claim ids from a JSONL ledger file.

    Args:
        ledger_path: Absolute path to the ``ledger.jsonl`` file.

    Returns:
        A ``set`` of claim id strings if the file exists and is valid,
        or ``None`` if the file does not exist.

    Raises:
        SystemExit: With code 2 if the file exists but cannot be parsed.
    """
    if not ledger_path.exists():
        return None

    ids: set[str] = set()
    for lineno, raw in enumerate(ledger_path.read_text(encoding="utf-8").splitlines(), start=1):
        raw = raw.strip()
        if not raw:
            continue
        try:
            record: dict = json.loads(raw)
        except json.JSONDecodeError as exc:
            print(
                f"ERROR: ledger file {ledger_path} has invalid JSON on line {lineno}: {exc}",
                file=sys.stderr,
            )
            sys.exit(2)
        claim_id = record.get("id", "")
        if claim_id:
            ids.add(claim_id)
    return ids


def _extract_latest_version_body(changelog_text: str) -> tuple[str, list[str]]:
    """Extract the heading label and body lines of the latest version section.

    Scans CHANGELOG.md for the first ``## [...]`` heading and returns
    everything up to (but not including) the next ``## [...]`` heading.

    The "Unreleased" section is treated as a version section just like any
    other: if it is the first heading, its body is returned.

    Args:
        changelog_text: Full text of the CHANGELOG.md file.

    Returns:
        A ``(version_label, body_lines)`` tuple where ``version_label`` is
        the string inside ``[...]`` (e.g. ``"0.7.0"`` or ``"Unreleased"``)
        and ``body_lines`` is the list of content lines in that section.
    """
    lines = changelog_text.splitlines()
    in_latest = False
    version_label = ""
    body: list[str] = []

    for line in lines:
        m = _VERSION_HEADING_RE.match(line)
        if m:
            if not in_latest:
                # First version heading found — start capturing.
                in_latest = True
                version_label = m.group(1)
                continue
            else:
                # Second version heading — stop.
                break
        if in_latest:
            body.append(line)

    return version_label, body


def _is_substantive(line: str) -> bool:
    """Return ``True`` if *line* is a substantive claim that requires a ledger tag.

    Structural lines (sub-section headers ``### ...``, blank lines, horizontal
    rules, reference-style link definitions) are excluded.  A line starting
    with ``-`` (bullet) or containing meaningful text is substantive.

    Args:
        line: A single line from the CHANGELOG version section body.

    Returns:
        ``True`` when the line is a claim that must carry a ``[ledger:<id>]`` tag.
    """
    stripped = line.strip()
    if not stripped:
        return False
    if _NON_SUBSTANTIVE_RE.match(stripped):
        return False
    # Sub-section headings (###) are structural, not claims.
    if stripped.startswith("###"):
        return False
    # Lines that are purely a "- No unreleased changes yet." style note
    # are still substantive from a gate perspective — but we treat
    # "- No ..." lines that start with "No" after the bullet as non-claims.
    if stripped.startswith("-"):
        # Bullet line — this is a claim.
        return True
    # Any other non-empty, non-structural line is treated as a claim.
    return True


def _run_gate(
    changelog_path: Path,
    ledger_path: Path,
    *,
    convention_start_version: str | None = None,
) -> int:
    """Run the pre-release gate and return an exit code.

    Args:
        changelog_path: Path to ``CHANGELOG.md``.
        ledger_path: Path to the JSONL ledger file.
        convention_start_version: If supplied, the gate enforces ledger citations
            ONLY when the latest CHANGELOG section label exactly matches this
            string; any other label exits PASS without citation checks.  This is
            exact-string equality, not semver ordering -- pass the exact label
            you want enforced (e.g. ``"0.8.0"`` or ``"Unreleased"``).  If
            ``None``, the gate enforces citations on the latest section
            regardless of its label.

    Returns:
        0 on clean pass, 2 on any failure.
    """
    if not changelog_path.exists():
        print(
            f"ERROR: CHANGELOG not found: {changelog_path}",
            file=sys.stderr,
        )
        return 2

    changelog_text = changelog_path.read_text(encoding="utf-8")
    version_label, body_lines = _extract_latest_version_body(changelog_text)

    if not version_label:
        print(
            "ERROR: No version heading found in CHANGELOG.md — cannot determine latest version.",
            file=sys.stderr,
        )
        return 2

    print(f"Pre-release gate: checking version [{version_label}]")

    # Determine whether the ledger-citation convention applies to this version.
    # When convention_start_version is provided, the gate enforces ledger citations
    # ONLY for that exact version label; any other latest-section label exits PASS
    # without citation checks.  This is exact-string equality, not semver ordering
    # -- pass the exact label you want enforced (e.g. "0.8.0" or "Unreleased").
    if convention_start_version is not None and version_label != convention_start_version:
        print(
            f"  Note: [{version_label}] does not match the enforced convention version "
            f"[{convention_start_version}]. Gate passes without citation checks on this entry."
        )
        print("PASS: No ledger citations required for this version.")
        return 0

    # Fail-closed: a missing ledger file is NOT a pass.
    ledger_ids = _load_ledger_ids(ledger_path)
    if ledger_ids is None:
        print(
            f"FAIL: Ledger file not found: {ledger_path}\n"
            "  Every release version requires a ledger file at "
            "outputs/release/<version>/ledger.jsonl.\n"
            "  Run scripts/release_doctor.py to generate it, or create it manually.",
            file=sys.stderr,
        )
        return 2

    print(f"  Ledger loaded: {len(ledger_ids)} claim(s) from {ledger_path}")

    failures: list[str] = []
    claim_line_numbers: list[tuple[int, str]] = []

    for lineno, line in enumerate(body_lines, start=1):
        if not _is_substantive(line):
            continue
        claim_line_numbers.append((lineno, line))

    if not claim_line_numbers:
        print(
            f"  Note: No substantive claim lines found under [{version_label}].\n"
            "  Add content or annotate 'No unreleased changes' explicitly."
        )
        print("PASS: (no substantive claims to check)")
        return 0

    for lineno, line in claim_line_numbers:
        tags = _LEDGER_TAG_RE.findall(line)
        if not tags:
            failures.append(
                f"  Line {lineno}: claim has no ledger citation — "
                f"add [ledger:<id>] or remove the claim.\n"
                f"    > {line.strip()}"
            )
            continue
        for tag_id in tags:
            if tag_id not in ledger_ids:
                failures.append(
                    f"  Line {lineno}: ledger id {tag_id!r} not found in {ledger_path}.\n    > {line.strip()}"
                )

    if failures:
        print(
            f"\nFAIL: {len(failures)} unresolved claim(s) under [{version_label}]:\n",
            file=sys.stderr,
        )
        for msg in failures:
            print(msg, file=sys.stderr)
        print(
            "\nFor each failing line, either:\n"
            "  (a) append [ledger:<id>] citing a record in the ledger, or\n"
            "  (b) remove the claim from CHANGELOG.md if it is not yet proven.",
            file=sys.stderr,
        )
        return 2

    print(f"PASS: All {len(claim_line_numbers)} claim(s) under [{version_label}] have resolved ledger citations.")
    return 0


def main() -> None:
    """Entry point for the pre-release gate CLI.

    Parses ``--changelog`` and ``--ledger`` arguments, runs the gate, and
    exits with the appropriate code.

    Raises:
        SystemExit: With code 0 (pass), 1 (usage error), or 2 (gate failure).
    """
    parser = argparse.ArgumentParser(
        description=(
            "Verify that every substantive CHANGELOG claim under the latest "
            "version heading has at least one [ledger:<id>] citation that "
            "resolves in the given JSONL ledger file."
        )
    )
    parser.add_argument(
        "--changelog",
        required=True,
        type=Path,
        metavar="PATH",
        help="Path to CHANGELOG.md",
    )
    parser.add_argument(
        "--ledger",
        required=True,
        type=Path,
        metavar="PATH",
        help="Path to outputs/release/<version>/ledger.jsonl",
    )
    parser.add_argument(
        "--convention-start",
        default=None,
        metavar="VERSION",
        help=(
            "Only enforce ledger citations when the latest CHANGELOG section "
            "matches this version label.  Historical entries are skipped.  "
            "If omitted, the gate enforces citations on the latest section "
            "regardless of version."
        ),
    )
    args = parser.parse_args()

    exit_code = _run_gate(
        changelog_path=args.changelog,
        ledger_path=args.ledger,
        convention_start_version=args.convention_start,
    )
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
