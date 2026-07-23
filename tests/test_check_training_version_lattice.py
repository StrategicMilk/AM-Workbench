"""Contract tests for the coordinated training dependency lattice."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from scripts.check_training_version_lattice import LATTICE_DOC, check_lattice

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"


def _rules(path: Path) -> set[str]:
    return {finding.rule_id for finding in check_lattice(path)}


def test_known_good_and_live_lattices_pass() -> None:
    assert check_lattice(FIXTURES / "pyproject_lattice_known_good.toml") == []
    assert check_lattice(ROOT / "pyproject.toml") == []


def test_lattice_diagnostic_links_to_public_documentation() -> None:
    assert LATTICE_DOC.startswith("docs/reference/")
    assert (ROOT / LATTICE_DOC).is_file()


def test_known_bad_changes_a_real_shared_band() -> None:
    findings = check_lattice(FIXTURES / "pyproject_lattice_known_bad.toml")
    assert any(item.rule_id == "LATTICE-SHARED" and "trl" in item.location for item in findings)


@pytest.mark.parametrize(
    "content, expected",
    [
        ("not = [valid", "LATTICE-INPUT"),
        ("[project]\nname='missing-extras'\n", "LATTICE-CELLS"),
        ("[project.optional-dependencies]\ntraining=[]\ntraining-unsloth=[]\n", "LATTICE-MEMBER"),
    ],
)
def test_malformed_and_incomplete_inputs_fail_closed(tmp_path: Path, content: str, expected: str) -> None:
    path = tmp_path / "pyproject.toml"
    path.write_text(content, encoding="utf-8")
    assert expected in _rules(path)


def test_missing_path_fails_closed(tmp_path: Path) -> None:
    assert _rules(tmp_path / "absent.toml") == {"LATTICE-INPUT"}


def test_duplicate_unbounded_and_missing_marker_are_independently_reported(tmp_path: Path) -> None:
    good = (FIXTURES / "pyproject_lattice_known_good.toml").read_text(encoding="utf-8")
    changed = good.replace('"torch>=2.7.0,<2.11",', '"torch", "Torch>=2.7.0,<2.11",', 1)
    changed = changed.replace("; sys_platform != 'darwin' or platform_machine != 'arm64'", "")
    path = tmp_path / "pyproject.toml"
    path.write_text(changed, encoding="utf-8")
    rules = _rules(path)
    assert {"LATTICE-MEMBER", "LATTICE-ACCEL"} <= rules


@pytest.mark.parametrize(
    "old, new, expected",
    [
        ('"torch>=2.7.0,<2.11",', "\"torch>=2.7.0,<2.11; sys_platform == 'never'\",", "LATTICE-MARKER"),
        (
            "sys_platform != 'darwin' or platform_machine != 'arm64'",
            "sys_platform == 'darwin' or platform_machine == 'arm64'",
            "LATTICE-ACCEL",
        ),
    ],
)
def test_dependency_markers_cannot_disable_or_invert_lattice_cells(
    tmp_path: Path, old: str, new: str, expected: str
) -> None:
    good = (FIXTURES / "pyproject_lattice_known_good.toml").read_text(encoding="utf-8")
    path = tmp_path / "pyproject.toml"
    path.write_text(good.replace(old, new, 1), encoding="utf-8")
    assert expected in _rules(path)


def test_subprocess_exit_codes_and_diagnostics() -> None:
    script = ROOT / "scripts" / "check_training_version_lattice.py"
    bad = subprocess.run(
        [sys.executable, str(script), "--pyproject", str(FIXTURES / "pyproject_lattice_known_bad.toml")],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    good = subprocess.run(
        [sys.executable, str(script), "--pyproject", str(FIXTURES / "pyproject_lattice_known_good.toml")],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert bad.returncode == 1
    assert "LATTICE-SHARED" in bad.stderr
    assert good.returncode == 0
