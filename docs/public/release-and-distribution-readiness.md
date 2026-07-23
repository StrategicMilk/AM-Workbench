# Release and Distribution Readiness

This page is the public-facing distribution boundary for AM Workbench. It is
not a launch announcement, community program, marketplace listing, or release
tag. It records what this checkout can prove before publication and what still
requires external action.

External publication, community, survey, and listing work remains in private
release-planning records. These rows are go-to-market evidence gaps, not
product/runtime known limitations. OAuth token exchange and
bearer-authenticated install probing are implemented product surfaces; hosted
marketplace presence and dynamic OAuth client registration are separate gaps.

## Why This Exists

The May 2026 market-reality audit found two related gaps:

| Audit row | Market gap | What this repo can honestly remediate |
|---|---|---|
| `FSA-00135` | AM Workbench has no proven external community presence even though local-first agent users are asking for this category. | Provide discoverable, public-safe positioning and distribution-readiness artifacts. Do not claim community presence until a real external post, listing, survey entry, or audience signal exists. |
| `FSA-00417` | Developer survey evidence shows local/self-hosted AI workstations are not yet a recognized category, and AM Workbench is absent from those survey results. | Define the local-first AI workstation category and the factual launch packet needed for survey/listing submissions. Do not claim survey recognition from this repo. |

Status as of 2026-07-01: `FSA-00135` and `FSA-00417` remain open as
external-evidence gaps. This page supplies the current status language and the
local launch-packet boundary; it does not claim public community presence,
survey recognition, or external listing proof.

Terminal closure for those rows requires evidence beyond local documentation if
the closure claim is "public/community presence now exists." Local docs can
close the weaker documentation and launch-packet gap, not the external presence
gap by themselves. For `RCG-0004-P05`, local closure is limited to fail-closed
schema validation proof and release-boundary wording; it does not close any
external distribution or community-presence claim.

## Public Category Statement

AM Workbench is a local-first AI workstation for operators who want the full
AI/ML work loop on their own machine: run agent goals, inspect evidence,
compare and tune prompts/models, build retrieval workflows, train adapters,
and promote only the changes that pass deterministic gates.

The short category phrase is:

> Local-first AI workstation

The longer distribution phrase is:

> AM Workbench is a local-first AI workstation for self-improving GenAI and ML
> work, built around local data residency, an immutable evidence spine,
> human-in-the-loop promotion gates, and optional cloud backends only when the
> operator configures them.

## Implemented Public Facts

These are the strongest claims that can be made from the current source tree
and product docs:

| Fact | Evidence surface | Boundary |
|---|---|---|
| Local-first is the default posture. | README trust controls, `config/cloud_providers.yaml`, and local GGUF/vLLM/OpenAI-compatible backend docs. | Cloud providers are available only by explicit configuration. |
| Agent work writes local evidence. | Metadata spine records, Console, Work Graph, Approval Chain, Readiness, and evidence-asset surfaces. | This is local evidence ownership, not a hosted enterprise audit-log service. |
| Promotion is gated. | Prompt, backend-overlay, training, and upgrade/rollback docs describe reviewable local artifacts and rollback paths. | No autonomous production promotion is claimed as the default. |
| MCP transports exist for documented local paths. | `docs/reference/mcp-server.md`, `/mcp`, `/mcp/tools`, `/mcp/message`, `/mcp/resources/stream`, and `vetinari mcp`. | Marketplace OAuth metadata can drive PKCE authorization requests, token exchange, and bearer-authenticated install probes; hosted marketplace publication and dynamic client registration are separate go-to-market/provider-integration gaps. |
| WIP surfaces are labelled. | README status table and `docs/reference/workbench-wip-surfaces.md`. | Visible UI or config metadata is not treated as completed end-to-end workflow proof. |
| Release proof is gated. | `docs/reference/release-license-evidence.md`, public export checks, CI release proof checks, and publication-boundary checks. | Passing a local checklist is not the same as a published release tag. |

Unknown, missing, stale, or unreadable evidence remains a release blocker. The
readiness gate must not convert empty evidence directories, absent claim-ledger
artifacts, or unreadable proof paths into a passing release status.

## Release-Readiness Artifacts

These artifacts are appropriate to prepare before a public push or survey/list
submission:

| Artifact | Location | Required status before external use |
|---|---|---|
| Product thesis and category statement | `docs/product-thesis.md` and this page | Must distinguish implemented facts from market ambition. |
| Public export boundary proof | `scripts/build_public_export.py` and `scripts/check_publication_boundary.py` | Must pass against the exact export tree being pushed. |
| Release license evidence | `docs/reference/release-license-evidence.md` | Must have no unresolved release-blocking row for the shipped artifact. |
| WIP surface reference | `docs/reference/workbench-wip-surfaces.md` | Must label partial Workbench surfaces without claiming production completion. |
| Support and known-limitations page | `docs/getting-started/faq-known-limitations.md` | Must name limitations, escalation evidence, and non-publication boundaries. |
| Support matrix | `config/support_matrix.yaml` | Must remain fresh and treat unsupported or unproved distribution states as unsupported. |
| Engine size baselines | `docs/reference/engine-size-baselines.json` | All four legs require measured bytes, canonical ceilings, and immutable hosted-run URLs; current null rows block publication. |
| CUDA runtime certification | Protected `cuda-certification` workflow job and same-run `engine-cuda-certification` artifact | Requires the hash-pinned independently governed verifier on the approved GPU runner to bind the exact source and Actions run to concrete device identity and a positive pinned-model offload memory assertion. The committed blocked example is not a publication input. |

## Not Proven By This Repo

The following facts cannot be created by editing this private checkout:

| External fact | Current local status | What would count as evidence |
|---|---|---|
| Public release tag | Not proven. | A real public Git tag, matching release artifact, and passing release proof for that tag. |
| AM Engine CUDA release | Blocked. | A governed GPU certification run proving device discovery and actual model offload for the exact release source. Standard hosted compiler runners do not qualify. |
| Public community presence | Not proven. | A maintained public GitHub project presence, forum post, community discussion, launch page, or similar external artifact. |
| Developer survey recognition | Not proven. | Inclusion in a survey, tooling index, public category list, or submitted listing with durable source reference. |
| Marketplace listing | Not proven. | An external listing page or marketplace entry under the actual publication account. |
| Hosted OAuth-backed MCP marketplace presence | Runtime and external proof required. | Dynamic client registration, hosted marketplace publication, provider-specific install docs, and external listing evidence. |

## Submission Packet

When the project is ready for external category-building, use this minimum
packet rather than inventing unsupported claims:

| Field | Value |
|---|---|
| Name | AM Workbench |
| Category | Local-first AI workstation |
| One-line description | Local-first AI workstation for self-improving GenAI and ML work on a single operator machine. |
| Trust posture | Local data by default; cloud backends only by explicit operator configuration. |
| Differentiators | Immutable local evidence spine, human-in-the-loop promotion gates, evaluate-to-retrain loop, local model and retrieval workflows. |
| Non-claims | No hosted SaaS, no current multi-user RBAC, no public community proof from the private checkout, no hosted MCP marketplace presence. |
| Release gate | Public export boundary, license evidence, release tag proof, and support matrix freshness must pass for the exact artifact. |

## Terminal Closure Guidance

This page supports documentation-level remediation for `FSA-00135` and
`FSA-00417` because it makes the product category, launch packet, and
publication boundary explicit. Terminal closure is justified only for the
local docs/config/test portion of those rows.

As long as the row's unresolved claim is actual external/community presence,
survey or tooling-list recognition, public release availability, or marketplace
distribution, the row remains a go-to-market evidence gap from this private checkout. Close it only after the external artifact exists and the closure row cites that artifact directly. Dynamic OAuth client registration and hosted
marketplace presence are separate support claims and close only when the
implementation, external artifact, and proof exist.

Terminal closure is not justified for actual external/community presence,
survey recognition, public release tags, or marketplace distribution until
those external artifacts exist and are cited directly. Those items belong in
the internal go-to-market tracker, not in the known-limitations register.
