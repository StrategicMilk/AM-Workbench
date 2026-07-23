# Promotion Inbox Runbook

## Purpose

Promotion Inbox gates workbench proposals before they become accepted prompts,
models, datasets, adapters, or pipeline changes. It checks deterministic eval
evidence, provenance, rollback data, taint state, and planner feedback before
it records an approve or reject decision.

## Operator Flow

1. Open the Workbench surface that lists pending promotion proposals.
2. Review the proposal, affected assets, pre-promotion evals, provenance, and
   rollback plan.
3. Reject proposals with missing evidence or stale taint.
4. Approve only when the gate is clear and the rollback path is present.
5. Confirm the decision appears in the metadata spine and work receipts.

## Runtime Paths

- [`vetinari/workbench/promotion_inbox.py`](../../vetinari/workbench/promotion_inbox.py)
- [`vetinari/workbench/promotions/engine.py`](../../vetinari/workbench/promotions/engine.py)
- [`crates/amw-kernel/src/api/routes/workbench_domains.rs`](../../crates/amw-kernel/src/api/routes/workbench_domains.rs)
- [`vetinari/workbench/metadata_spine.py`](../../vetinari/workbench/metadata_spine.py)

## Troubleshooting

- `missing_provenance`: attach source trace, dataset, model, or prompt lineage.
- `missing_eval_evidence`: run the required deterministic eval before approval.
- `failing_eval_score`: reject or rerun after the proposed asset is corrected.
- `stale_asset_taint`: clear or replace tainted assets before promotion.
- `missing_rollback_plan`: add a rollback record before approval.
- `plan_feedback_refused`: resolve the planner rejection before re-submitting.

## Evidence Expectations

Every accepted promotion should have a proposal row, gate evidence, decision
transition, promotion record, and work receipt. Missing spine state is a block,
not an operator warning.
