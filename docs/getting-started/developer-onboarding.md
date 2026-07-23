# Developer Onboarding

> This guide is for developers contributing to Vetinari. For operator setup,
> see [operator-guide.md](operator-guide.md).

## Purpose

Use this path when you need to edit source, run tests, or understand the
Workbench architecture before making a contribution.

## Repository Tour

- `vetinari/` contains production Python code.
- `tests/` mirrors production behavior with pytest coverage.
- `docs/` contains operator, developer, reference, and internal workflow docs.
- `scripts/` contains repeatable validation and maintenance entry points.
- `ui/svelte/` contains the Workbench frontend.
- `src-tauri/` contains the desktop shell.
- `crates/amw-kernel/` contains the Rust Axum runtime API host.

## Setup

```powershell
python -m venv .venv312
.\.venv312\Scripts\Activate.ps1
python.cmd -m pip install -e ".[dev,local,ml,search,notifications]"
```

On macOS or Linux, activate with `source .venv312/bin/activate`.

## Verify

```powershell
python.cmd -m vetinari doctor --json
python.cmd scripts/run_tests.py
python.cmd -m ruff check vetinari/
python.cmd scripts/check_vetinari_rules.py
```

## First Change

Read the relevant module and tests before editing. Keep behavior wired through
the runtime, CLI, API, or documented invocation path, then run the smallest
focused validation before broader checks.

For documentation freshness work, update the executable checker or test that
guards the claim first, then update prose to cite that proof.
