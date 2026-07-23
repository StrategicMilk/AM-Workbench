# Incident Response Runbook

This runbook is for production or operator-visible Vetinari failures: service
unavailable, unsafe action blocked incorrectly, data corruption suspected,
release artifact mismatch, or repeated Workbench health degradation.

## First Response

1. Stop any release, installer, migration, or destructive maintenance action
   until the incident owner records the affected surface.
2. Capture current state before restarting anything:

```bash
vetinari doctor --json > incident-doctor.json
vetinari status > incident-status.txt
python scripts/check_workbench_health.py > incident-workbench-health.txt
python scripts/check_release_claims_ledger.py --release-root outputs/release > incident-release-ledger.txt
```

3. Preserve local logs, receipts, and state directories in an operator-owned
   incident folder outside the repo. Redact secrets before sharing.
4. Classify the incident as one of: runtime outage, state corruption, release
   evidence failure, authorization/safety failure, or degraded observability.
5. Pick the matching recovery section below and keep every command output with
   the incident record.

## Runtime Outage

1. Run `vetinari doctor --json` and inspect failed probes.
2. If the web process is down, restart with the normal local command:

```bash
vetinari serve --host 127.0.0.1 --port 5000
```

3. Re-run `vetinari status` and `python scripts/check_workbench_health.py`.
4. Do not mark the outage resolved until both commands exit 0 or the remaining
   failure is linked to a tracked follow-up.

## Release Evidence Failure

1. Re-run the release proof checker:

```bash
python scripts/check_ci_release_proof.py
python scripts/check_release_artifacts.py --dist-dir dist
python scripts/check_release_claims_ledger.py --release-root outputs/release
```

2. If any command fails, stop the release and attach the failing output to the
   release evidence package.
3. Regenerate public export artifacts only after the private checkout passes
   publication-boundary checks.

## Safety Or Authorization Failure

1. Save the rejected request, admin-token state, policy verdict, and receipt id.
2. Re-run the guard-specific test or route proof before changing policy:

```bash
pytest tests/test_security_hardening.py tests/test_workbench_support_bundle.py -q
```

3. If the guard failed open, disable the affected action until a regression test
   proves the deny-before-use branch.

## Closure Criteria

An incident is closed only when:

- The affected runtime or release command exits 0.
- A regression test or checker covers the failed branch.
- The incident record contains before and after evidence.
- Any residual risk has a concrete owner and artifact path.
