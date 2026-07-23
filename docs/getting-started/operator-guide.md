# Operator Guide

> This guide is for operators running Vetinari. For developer setup, see
> [developer-onboarding.md](developer-onboarding.md).

## Prerequisites

- Python 3.10 or newer.
- `.venv312` created from the repository root.
- Vetinari installed in editable mode.
- `config/` present with model and inference configuration files.

## Starting The Server

Windows PowerShell:

```powershell
.\.venv312\Scripts\Activate.ps1
python -m vetinari serve --host 127.0.0.1 --port 8000
```

Unix/macOS:

```bash
source .venv312/bin/activate
python -m vetinari serve --host 127.0.0.1 --port 8000
```

## Verifying The Server Is Running

Open `http://localhost:8000/health` or run a local HTTP client against it. A
healthy response should report the application as reachable and should either
name a loaded model path/backend or name the specific degraded subsystem.

## First Run Checklist

- Confirm the configured model loads or the backend endpoint is reachable.
- Submit a small test task.
- Inspect the Workbench run surface.
- Check logs for authorization, model, or readiness warnings.
- Confirm the training pipeline is idle unless you intentionally started it.

## Next Steps

- [Inspecting Run Results](../guides/inspect-run-results.md)
- [Troubleshooting](../troubleshooting.md)
- [Configuration Keys](../reference/config-keys.md)
