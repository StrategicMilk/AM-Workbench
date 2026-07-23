# Upgrade, Migration, and Rollback Runbook

Use this runbook before changing the AM Workbench application version, applying
database migrations, changing model/runtime backends, or rolling back a failed
local upgrade. It is written for the supported single-workstation deployment:
one checkout, one `.venv312`, one local Rust kernel API server, and operator-owned
state on disk.

## When To Use This

Use this runbook for:

- `git pull`, branch switch, or release package replacement before restarting
  the local server
- dependency refresh with `python -m pip install -e ...`
- `python -m vetinari migrate`
- model backend changes that can alter runtime state
- rollback after `doctor`, `status`, Workbench health, or user workflows fail

Do not use this runbook to repair crash loops or suspected data corruption
while the process is still failing. Start with
[Crash Recovery](crash-recovery.md) for unexpected exits and
[Incident Response](incident-response.md) for operator-visible incidents.

## Pre-Upgrade Snapshot

Run these commands from the repo root before changing files or state:

```powershell
python -m vetinari doctor --json > upgrade-doctor-before.json
python -m vetinari status > upgrade-status-before.txt
python scripts/check_workbench_health.py > upgrade-workbench-health-before.txt
git rev-parse HEAD > upgrade-git-head-before.txt
git status --short > upgrade-git-status-before.txt
```

Stop every running AM Workbench process before copying or replacing persistent
state. Preserve these locations in an operator-owned backup folder outside the
repo when they exist:

- `.vetinari/`
- `outputs/workbench/`
- `outputs/release/`
- `logs/`
- `~/.vetinari/config.yaml`
- model backend config files referenced by `VETINARI_MODELS_DIR`,
  `VETINARI_NATIVE_MODELS_DIR`, `VETINARI_VLLM_ENDPOINT`, or
  `VETINARI_NIM_ENDPOINT`

Do not delete the original state after copying it. Move it aside only when a
rollback or recovery step explicitly needs to restore a known-good copy.

## Upgrade Procedure

1. Confirm the worktree state is intentional. Do not upgrade over unrelated
   local edits unless the operator has decided to carry them forward.
2. Run `python -m vetinari upgrade` before schema migrations first when the
   target release requires a runtime upgrade step.
2. Update the application source or checkout the target release.
3. Reinstall dependencies through the project interpreter:

```powershell
python -m pip install -e ".[dev,local]"
```

Use a larger optional group only when that workflow is required, for example
`.[dev,local,ml,search,notifications]` for a full local operator workstation.

4. Run migrations while the server is stopped:

```powershell
python -m vetinari migrate
```

5. Start the local server on loopback:

```powershell
$env:VETINARI_ADMIN_TOKEN = "<choose-a-local-admin-token>"
python -m vetinari serve --host 127.0.0.1 --port 5000
```

## Post-Upgrade Validation

In a second terminal, run:

```powershell
python -m vetinari doctor --json > upgrade-doctor-after.json
python -m vetinari status > upgrade-status-after.txt
python scripts/check_workbench_health.py > upgrade-workbench-health-after.txt
python -m pytest tests/operator/test_docs_config_and_readme_hygiene.py -q
```

The upgrade is acceptable only when the after-action diagnostics exit 0 or the
operator records a specific degraded subsystem with a follow-up owner. A clean
`pip install` is not enough proof because migrations, model discovery, and
Workbench state can fail after installation succeeds.

## Rollback Procedure

Use rollback when the upgraded runtime cannot pass health checks, user actions
fail, or the operator decides to return to the prior version.

1. Stop every AM Workbench process.
2. Restore the prior source revision or release package recorded in
   `upgrade-git-head-before.txt`.
3. Reinstall dependencies from that restored revision.
4. Move the upgraded persistent state aside. Do not delete it.
5. Copy the pre-upgrade backup of `.vetinari/`, `outputs/workbench/`,
   `outputs/release/`, `logs/`, and `~/.vetinari/config.yaml` back to their
   original locations.
6. Start the local server on loopback and rerun the same `doctor`, `status`,
   and Workbench health commands.

Rollback is complete only when the restored runtime command starts, the health
commands pass, and the operator has preserved both the failed-upgrade state and
the restored state evidence.

## Migration Failure Handling

If `python -m vetinari migrate` fails:

1. Keep the failing stdout/stderr with the upgrade evidence.
2. Do not retry after manually editing SQLite, JSONL, or migration marker files.
3. Confirm no AM Workbench process is still writing state.
4. Restore the pre-upgrade backup if the runtime must return to service.
5. File an issue with the migration command, exit code, sanitized traceback,
   pre-upgrade commit, target commit, and whether `.vetinari/` or
   `outputs/workbench/` changed before failure.

## Support Evidence To Attach

Before escalating, collect:

- `upgrade-git-head-before.txt` and the target revision
- `upgrade-doctor-before.json` and `upgrade-doctor-after.json`
- `upgrade-status-before.txt` and `upgrade-status-after.txt`
- `upgrade-workbench-health-before.txt` and
  `upgrade-workbench-health-after.txt`
- migration stdout/stderr and exit code
- sanitized server logs for the failed startup or failed request

Do not attach raw JSONL stores, SQLite databases, model weights, prompt text,
or secrets to public issues.
