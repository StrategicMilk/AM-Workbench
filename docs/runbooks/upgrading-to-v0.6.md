# Upgrading to v0.6 - AM Workbench Migration

## What Changed

Version 0.6 reframed the product around AM Workbench as the primary interaction
surface, consistent with ADR-0161. The factory pipeline remains the execution
engine, but Workbench is now where operators inspect runs, approvals, assets,
and recovery evidence.

Key changes:

- Workbench is now the primary interaction surface, replacing project-centric UI
  framing.
- The session kernel manages task execution lifecycles.
- Run results are stored in the Workbench spine JSONL plus the SQLite index.

## Affected Configuration Keys

No configuration key changes in v0.6.

## Migration Steps

1. Back up the `outputs/` directory before upgrading.
2. Run `python scripts/sync_workflow_artifacts.py` to update workflow metadata.
3. Verify the native kernel API surface with `cargo test -p amw-kernel api::routes::workbench_domains::tests::kernel_request_dispatches_migration_owned_routes_without_http_proxy`.
4. Start the desktop Workbench and verify the run list loads from the native
   kernel-backed Workbench routes.

## Rollback

Use [Upgrade, Migration, and Rollback](upgrade-migration-rollback.md) before
restoring application version, migration state, or persistent state.

## Support

Start with [Troubleshooting](../troubleshooting.md) and the repository
[AGENTS.md](../../AGENTS.md) operating contract.
