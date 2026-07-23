# Crash Recovery Runbook

Use this when Vetinari, the Workbench UI, a background scheduler, or a local
training/evaluation process exits unexpectedly.

## Capture Before Repair

1. Record the failed command, exit code, timestamp, branch, and any active
   workspace path.
2. Capture health and state without mutating runtime data:

```bash
vetinari doctor --json > crash-doctor-before.json
vetinari status > crash-status-before.txt
python scripts/check_workbench_health.py > crash-workbench-health-before.txt
```

3. Copy `outputs/workbench/`, `outputs/release/`, and relevant receipt files to
   an operator-owned backup directory outside the repo before deletion,
   migration, or cleanup.

## Restore Workbench State

1. Stop every Vetinari process that can write Workbench state.
2. Move the suspected state directory aside instead of deleting it.
3. Restore the newest known-good backup to the original path.
4. Re-run:

```bash
vetinari doctor --json > crash-doctor-after.json
vetinari status > crash-status-after.txt
python scripts/check_workbench_health.py > crash-workbench-health-after.txt
```

5. Compare before and after outputs. If the after commands fail, keep both the
   moved-aside state and restored backup for diagnosis.

## Resume Durable Execution

1. Inspect the latest checkpoint or receipt for the interrupted plan id.
2. Resume only through the normal runtime entry point for that feature; do not
   edit checkpoint JSON by hand.
3. Verify that resumed work binds a fresh trace id and writes a new receipt.
4. Run the focused regression command for the affected subsystem.

## Crash Loop

If the same command crashes twice:

1. Disable automatic restart for that process.
2. Save the exact failing inputs and logs.
3. Create or update a regression test that reproduces the crash.
4. Fix the crash before restoring the process to normal scheduling.

## Closure Criteria

Crash recovery is complete only when:

- The restored runtime command exits 0.
- `vetinari status` and `scripts/check_workbench_health.py` pass.
- State backup, moved-aside state, and after-action evidence are recorded.
- The crash branch has a regression test or an explicit blocked tracker.

## RCG-0065-P01 Security Doc Evidence

- Pack: RCG-0065-P01.
- Scope review: no direct source row maps to this file in slice 01/02; retained as an affected surface for explicit no-change evidence.
- Validation command: `.venv312/Scripts/python.exe -m pytest tests/operator/test_security_doc_standards.py -m operator -n 0`.
