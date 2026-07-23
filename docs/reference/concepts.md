# AM Workbench Concepts

This concept layer explains how the main product surfaces fit together. It
does not claim every surface is production-ready; current support still
comes from `config/support_matrix.yaml`, capability records, and focused
proof commands.

## Product Boundary

AM Workbench is the public product surface for the `vetinari` codebase. Its
scope is a local-first, evidence-driven workstation for AI/ML work. The
factory pipeline is one peer surface, not the whole product.

## Factory Pipeline

The runtime factory pipeline is:

```text
Foreman -> Worker -> Inspector
```

Foreman plans and coordinates, Worker executes, and Inspector evaluates.
Auxiliary provenance roles such as `TRAINING`, `RELEASE`, and `WORKBENCH`
label receipts outside that routing pipeline.

## Workbench Spine

Workbench artifacts, runs, traces, evaluations, proposals, leases, and
promotions are recorded through append-only spine-style stores and derived
indexes. Missing, corrupt, or unreadable evidence is not success; it blocks
promotion until repaired.

## Capability Maturity

Capability maturity is evidence based. A surface can be in product scope
while still unsupported or experimental. Promotion requires current records,
passing proof, and any tool evidence required by the task profile.

## Local-First Trust Posture

Local runtimes are the default trust posture for traces, datasets, prompts,
and audit evidence. Cloud providers are opt-in configured paths for reach and
cascade routing, not the default data path.

## Governance Documents

- `docs/product-thesis.md` defines product scope and current support posture.
- `docs/architecture/pipeline.md` defines the runtime agent architecture.
- `docs/reference/api.md` indexes live API modules.
- ADR JSON files under `adr/` record accepted architecture decisions.
