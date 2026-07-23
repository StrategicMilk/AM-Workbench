# Third-party Backend Overlays

This directory is the governance lane for local backend overlay manifests. An
overlay is a reviewed patch queue against an upstream backend dependency. This
lane records why an overlay exists, how to prove it is still needed, how to
prove it still works, how to roll it back, and whether it rebases against the
currently pinned upstream backend version.

The checker and CLI are read-only by default. They validate manifests and plan
commands, but they do not apply patches to `vetinari/adapters/`,
`vetinari/inference/`, or third-party source trees unless a caller explicitly
requests an approval-gated apply plan.

Required manifest fields:

- `backend`: backend key from `config/backend_pins.yaml`.
- `upstream_version`: upstream version or pin the overlay was last checked
  against.
- `patch_queue_path`: path to the patch queue or patch file.
- `purpose`: short reason the overlay exists.
- `known_bad_repro_command`: command that reproduces the upstream defect.
- `known_good_proof_command`: command that proves the overlay fixes it.
- `benchmark_evidence`: benchmark or performance evidence reference.
- `rollback_command`: command an operator can run to remove the overlay.
- `approval_status`: `approved`, `pending`, or `rejected`.
- `approval_actor`: approving human or governance lane.
- `approval_timestamp`: UTC ISO-8601 timestamp.
- `rebase_status`: `clean`, `failed`, `pending`, or `unknown`.
- `last_checked_upstream_version`: upstream version checked most recently.

The example manifest is intentionally non-applying documentation. It validates
the metadata contract without carrying or applying a speculative backend patch.
