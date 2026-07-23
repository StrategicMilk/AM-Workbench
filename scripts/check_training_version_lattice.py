"""Validate the coordinated vanilla and Unsloth training dependency lattice."""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

import tomllib
from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name

REPO_ROOT = Path(__file__).resolve().parents[1]
LATTICE_DOC = "docs/reference/training-version-lattice.md"

SHARED_BANDS = {
    "torch": ">=2.7.0,<2.11",
    "datasets": ">=3.4.1,<4.4",
    "peft": ">=0.19,<1.0",
    "trl": ">=0.24,<0.25",
    "bitsandbytes": ">=0.49,<0.50",
    "huggingface-hub": ">=0.36,<2.0",
}
TRANSFORMERS_BANDS = {"training": ">=4.57,<5.0", "training-unsloth": ">=5.1,<5.5"}
UNSLOTH_BAND = ">=2026.5.9,<2027.0"
UNSLOTH_MARKER = 'sys_platform != "darwin" or platform_machine != "arm64"'
REQUIRED_MEMBERS = frozenset({*SHARED_BANDS, "transformers"})


@dataclass(frozen=True, slots=True)
class LatticeFinding:
    """One stable, path-attributed lattice violation."""

    rule_id: str
    location: str
    message: str

    def render(self) -> str:
        """Render a deterministic CLI diagnostic."""
        return f"{self.rule_id} [{self.location}]: {self.message}"


def _normalized_specifier(requirement: Requirement) -> str:
    return str(requirement.specifier)


def _parse_cell(name: str, values: object) -> tuple[dict[str, Requirement], list[LatticeFinding]]:
    findings: list[LatticeFinding] = []
    requirements: dict[str, Requirement] = {}
    if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
        return {}, [LatticeFinding("LATTICE-CELLS", name, "extra must be an array of requirement strings")]
    for index, raw in enumerate(values):
        try:
            requirement = Requirement(raw)
        except InvalidRequirement as exc:
            findings.append(LatticeFinding("LATTICE-MEMBER", f"{name}[{index}]", f"invalid requirement: {exc}"))
            continue
        package = canonicalize_name(requirement.name)
        if package in requirements:
            findings.append(LatticeFinding("LATTICE-MEMBER", name, f"duplicate normalized package {package!r}"))
            continue
        requirements[package] = requirement
    return requirements, findings


def _load_cells(path: Path) -> tuple[dict[str, dict[str, Requirement]], list[LatticeFinding]]:
    try:
        with path.open("rb") as handle:
            document = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        return {}, [LatticeFinding("LATTICE-INPUT", str(path), f"cannot read valid TOML: {exc}")]
    extras = document.get("project", {}).get("optional-dependencies")
    if not isinstance(extras, dict):
        return {}, [LatticeFinding("LATTICE-CELLS", str(path), "project.optional-dependencies is missing")]
    cells: dict[str, dict[str, Requirement]] = {}
    findings: list[LatticeFinding] = []
    for cell_name in ("training", "training-unsloth"):
        if cell_name not in extras:
            findings.append(LatticeFinding("LATTICE-CELLS", cell_name, "required training cell is missing"))
            continue
        cells[cell_name], cell_findings = _parse_cell(cell_name, extras[cell_name])
        findings.extend(cell_findings)
    return cells, findings


def check_lattice(path: Path) -> list[LatticeFinding]:
    """Return ordered findings for *path*; an empty list is a pass."""
    cells, findings = _load_cells(path)
    if findings and not cells:
        return findings
    for cell_name, requirements in cells.items():
        missing = sorted(REQUIRED_MEMBERS - requirements.keys())
        for package in missing:
            findings.append(
                LatticeFinding("LATTICE-MEMBER", cell_name, f"required bounded member {package!r} is missing")
            )
        for package in sorted(REQUIRED_MEMBERS & requirements.keys()):
            if requirements[package].marker is not None:
                findings.append(
                    LatticeFinding("LATTICE-MARKER", f"{cell_name}.{package}", "core dependency must be unconditional")
                )
            specifier = _normalized_specifier(requirements[package])
            if ">=" not in specifier or "<" not in specifier:
                findings.append(
                    LatticeFinding(
                        "LATTICE-MEMBER", f"{cell_name}.{package}", "requirement must have lower and upper bounds"
                    )
                )

    vanilla = cells.get("training", {})
    accelerated = cells.get("training-unsloth", {})
    if "unsloth" in vanilla:
        findings.append(LatticeFinding("LATTICE-VANILLA", "training.unsloth", "vanilla cell must exclude Unsloth"))
    unsloth = accelerated.get("unsloth")
    if unsloth is None:
        findings.append(LatticeFinding("LATTICE-ACCEL", "training-unsloth", "accelerated cell must include Unsloth"))
    else:
        if _normalized_specifier(unsloth) != _normalized_specifier(Requirement(f"unsloth{UNSLOTH_BAND}")):
            findings.append(
                LatticeFinding("LATTICE-ACCEL", "training-unsloth.unsloth", f"expected band {UNSLOTH_BAND}")
            )
        expected_marker = str(Requirement(f"unsloth{UNSLOTH_BAND}; {UNSLOTH_MARKER}").marker)
        if str(unsloth.marker or "") != expected_marker:
            findings.append(
                LatticeFinding(
                    "LATTICE-ACCEL", "training-unsloth.unsloth", "required platform marker is missing or changed"
                )
            )

    for package, expected in SHARED_BANDS.items():
        expected_spec = _normalized_specifier(Requirement(f"{package}{expected}"))
        for cell_name, requirements in cells.items():
            requirement = requirements.get(package)
            if requirement is not None and _normalized_specifier(requirement) != expected_spec:
                findings.append(
                    LatticeFinding("LATTICE-SHARED", f"{cell_name}.{package}", f"expected coordinated band {expected}")
                )
        if (
            package in vanilla
            and package in accelerated
            and _normalized_specifier(vanilla[package]) != _normalized_specifier(accelerated[package])
        ):
            findings.append(LatticeFinding("LATTICE-SHARED", package, "shared dependency bands differ across cells"))

    for cell_name, expected in TRANSFORMERS_BANDS.items():
        requirement = cells.get(cell_name, {}).get("transformers")
        expected_spec = _normalized_specifier(Requirement(f"transformers{expected}"))
        if requirement is not None and _normalized_specifier(requirement) != expected_spec:
            findings.append(
                LatticeFinding(
                    "LATTICE-TRANSFORMERS", f"{cell_name}.transformers", f"expected intentional cell band {expected}"
                )
            )
    return sorted(findings, key=lambda item: (item.rule_id, item.location, item.message))


def run_checks(path: Path) -> int:
    """Print diagnostics and return the checker exit code."""
    findings = check_lattice(path)
    if findings:
        for finding in findings:
            print(finding.render(), file=sys.stderr)
        print(f"See {LATTICE_DOC} for the coordinated bump procedure.", file=sys.stderr)
        return 1
    print(f"training version lattice valid: {path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pyproject", type=Path, default=REPO_ROOT / "pyproject.toml")
    args = parser.parse_args(argv)
    return run_checks(args.pyproject)


if __name__ == "__main__":
    raise SystemExit(main())
