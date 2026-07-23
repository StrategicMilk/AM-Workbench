# Contributing to Vetinari

This guide covers the local development setup and the quality gates you must
pass before opening a pull request.

## Prerequisites

- Python 3.12 for the local `.venv312` developer environment.
- CI also exercises Python 3.11, 3.12, and 3.13 on Linux, plus Python 3.12 on
  Windows. Do not treat the local venv pin as the full support matrix.
- Git 2.35+

## Environment Setup

Clone the repository and create the virtual environment using the pinned
interpreter:

```bash
git clone <repo-url>
cd Vetinari
python3.12 -m venv .venv312
```

Activate and install all development dependencies:

```bash
# Windows (PowerShell)
.venv312\Scripts\Activate.ps1
pip install -e ".[dev,all]"

# macOS / Linux
source .venv312/bin/activate
pip install -e ".[dev,all]"
```

All automation in this project invokes the venv interpreter explicitly via
`python` from the activated project environment (Windows or POSIX)
activate path on other platforms.  Never use the system `python` — it may be a
different version and will produce false positive test results.

## Pre-commit Hooks

Install the pre-commit hooks once after cloning:

```bash
pre-commit install
```

The hooks run automatically on `git commit`.  To run them manually against all
files:

```bash
pre-commit run --all-files
```

Key hooks enforced at commit time:

| Hook | What it checks |
|------|----------------|
| `ruff` | Style, imports, and lint rules (including T20 print-detection) |
| `ruff-format` | Code formatting |
| `pyright` | Type correctness — runs against each changed `.py`/`.pyi` file |
| `check-vetinari-rules` | Project-specific VET rules (unwired code, enum literals, docstrings, …) |
| `bandit` | Security patterns |

## Running Tests

Run the full test suite:

```bash
python -m pytest tests/ -x -q
```

Run tests for a specific module (mirrors the source path):

```bash
python -m pytest tests/test_preflight.py -x -q
```

The project uses `.venv312` for all test runs.  The `python.cmd` wrapper at the
repo root also works:

```bash
.\python.cmd -m pytest tests/ -x -q
```

## Linting

Check for lint errors in the `vetinari/` package:

```bash
python -m ruff check vetinari/
```

Apply auto-fixable rules in place:

```bash
python -m ruff check vetinari/ --fix
python -m ruff format vetinari/
```

## Project Rule Checker

The VET rule checker enforces project-specific conventions beyond what ruff
covers — unwired public functions, same-name classes, missing `from __future__
import annotations`, deprecated code patterns, and more:

```bash
python scripts/check_vetinari_rules.py
```

This must exit with zero errors before any work is considered done.  Warnings
are informational; errors block the commit gate.

## Commit Message Format

This project follows [Conventional Commits](https://www.conventionalcommits.org/):

```
<type>(<scope>): <short summary>

[optional body]

[optional footers]
```

Common types:

| Type | When to use |
|------|-------------|
| `feat` | New feature or capability |
| `fix` | Bug fix |
| `refactor` | Code restructuring with no behavior change |
| `test` | Adding or updating tests |
| `docs` | Documentation only |
| `chore` | Tooling, CI, dependency updates |

When a commit implements an Architecture Decision Record, append a trailer:

```
Decision-Ref: ADR-NNNN
```

Examples:

```
feat(preflight): add CUDA toolkit detection for NIM backend
fix(adapters): reuse LocalInferenceAdapter instead of creating per-request
docs(contributing): add venv setup and pre-commit instructions
```

## Architecture Decision Records (ADRs)

Significant decisions — new modules, API changes, security choices, technology
adoptions — must be documented as ADRs stored in `adr/` before the code lands.
Architecture decisions are recorded in the private development checkout; public
changes should explain their design impact in the pull request.

## Code Style Highlights

- `from __future__ import annotations` at the top of every `vetinari/` file.
- Use `datetime.now(timezone.utc)` — never `datetime.now()` or `datetime.utcnow()`.
- Use `pathlib.Path` for file paths — never `os.path.join()`.
- Always pass `encoding="utf-8"` to `open()`.
- No `print()` in `vetinari/` library modules — use `logger = logging.getLogger(__name__)`.
- No hardcoded `temperature=0.3` or `max_tokens=N` — use `InferenceConfigManager`.

Repository automation enforces the applicable contribution rules in CI.
