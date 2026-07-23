# Vetinari Product Thesis

This thesis describes the product boundary for Vetinari (released publicly as
AM Workbench) from the live code and support artifacts in this checkout. It
is intentionally evidence-first: planning documents, historical release
notes, and broad README statements are not treated as current support proof
unless they are backed by live contracts, tests, or configuration in the
repository.

Decision record: ADR-0161 (`adr/ADR-0161-am-workbench-product-framing.json`)
sets this full-suite AM Workbench framing and keeps ADR-0061 as the runtime
agent-factory architecture record.

The thesis distinguishes two layers:

- **Scope and intent.** What AM Workbench is being built to be. Used as the
  baseline that audit and brainstorm skills evaluate proposals against.
- **Current support posture.** What is currently provable in the checked
  workspace, governed by the support matrix and the capability-record
  contract.

A proposal that fits the scope but is not yet supported is a maturity gap,
not an out-of-scope item.

## What Vetinari Is

Vetinari, released publicly as AM Workbench, is a local-first,
evidence-driven, full-suite workstation for AI/ML work that is designed to
get better at the user's work over time. Three nested framings, in priority
order:

1. **Self-improving AI/ML work — purpose.** The unifying purpose is that the
   suite feeds a system that improves prompts, models, adapters, methods,
   retrieval, datasets, and operator defaults over time. Kaizen is the spine
   that ties capabilities together.
2. **Evidence-driven — mechanism.** Append-only JSONL spines, SQLite indexes,
   trace records, capability evidence, and promotion gates exist so
   improvement is measurable, replayable, and qualifiable. Unknown
   confidence, missing capability records, and unreadable stores fail closed.
3. **Local-first sovereign — trust posture.** Traces, adapters, methods,
   datasets, evaluations, and audit trails stay on the operator's machine by
   default. Cloud paths are configured opt-in capabilities, not the default
   data path.

The codebase is named `vetinari`; the public release name and surface
branding is AM Workbench. Existing routing, documentation, and configuration
refer to both names depending on audience.

### Capability Surfaces (peers, not extensions)

AM Workbench is full-suite. The Foreman -> Worker -> Inspector factory
pipeline (ADR-0061) is one capability surface among several peers, not a
spine that other capabilities hang off. Surfaces include:

- Agent factory (Foreman -> Worker -> Inspector with the eight-stage
  assembly line, DAG execution, and correction loop).
- Training (QLoRA / DoRA, kaizen cycles, idle training, Thompson Sampling
  selection).
- Prompt engineering (mutation operators, A/B comparison, statistical gates).
- Evaluation, benchmarking, and model comparison.
- Dataset workflows (curation, labeling, synthetic data, versioning,
  lineage).
- Memory and retrieval (session, SQLite / FTS5, embeddings, RAG building).
- Model lab (browse, search, pull, quantize, compare, profile).
- Capability discovery (Method Library, Capability Packs, Domain Kits,
  Workflow Builder, Extensions surfaces).
- Operator surfaces (Console, Shell, Playground, Promotion Inbox, Approval
  Chain, Work Graph, RAG Debugger, Readiness, Status, Resource Cockpit,
  Mission Control).
- Observability, cost accounting, and usage tracking across backends.

Inference backends -- GGUF / llama-cpp, explicit server backends, OpenAI-compatible local servers, and cloud providers
(Anthropic, OpenAI, Gemini, and similar) -- are peer inference paths. Local
backends are load-bearing for the local-first trust posture; cloud backends
are load-bearing for cascade routing and reach. The product does not anchor
on a single backend.

The factory pipeline is one generator of improvable behaviors and one
consumer of improved ones; it is not the sole owner of the kaizen spine.

### Current Product Evidence Map

The following product-facing claims are backed by live source surfaces in
this checkout:

| Claim | Implemented surface | Current limit |
|---|---|---|
| Local governance and audit visibility are first-class product value. | README trust controls, append-only metadata spine, Work Graph, Console, Approval Chain, Readiness, Status, and evidence-asset views. | This is local product positioning, not proof of hosted enterprise governance deployment. |
| Version history, rollback, and audit-log ownership are local governance surfaces. | Prompt version history / rollback CLI paths, promotion rollback gates, backend-overlay rollback commands, upgrade snapshots, and metadata spine records. | This is local evidence ownership; it does not claim hosted enterprise audit-log service parity. |
| Conversation history can be exported and searched. | `GET /api/v1/chat/export/{project_id}` and `GET /api/v1/chat/search` in `vetinari/web/litestar_chat_api.py`. | Local-user guarded; responses redact sensitive values and read project conversation JSON files. |
| Named prompt-library workflows exist alongside prompt optimization. | `GET/POST/DELETE /api/v1/system-prompts`, Method Library prompt-method cards, and Workbench Prompt Engineering routes. | `/api/v1/system-prompts` is a reusable prompt-template store; live runtime prompt promotion still requires approval and promotion gates. |
| Prompt engineering is more than a single global prompt. | `POST /api/workbench/prompt-engineering/mutations`, `POST /api/workbench/prompt-engineering/search`, and experiment history routes call `PromptMutator` and `PromptOptimizer`. | Runtime promotion still goes through approval and promotion gates; this is not a free-form prompt registry. |
| External MCP clients can discover and call the documented server transports. | `docs/reference/mcp-server.md`, `/mcp/tools`, `/mcp/message`, `/mcp/resources/stream`, and the `vetinari mcp` CLI path. | Hosted marketplace publication and dynamic client registration are not claimed; Streamable HTTP single-endpoint server interoperability still requires runtime proof before support is claimed. |
| Extension and MCP marketplace metadata fails closed under Workbench authority. | `config/workbench/extension_marketplace.yaml`, extension risk verdicts, admin-gated extension API, PKCE authorization request, token exchange, bearer-authenticated install probing, and plugin registration checks. | Marketplace rows remain risk inputs, not public marketplace presence or an external listing. |
| Hybrid local/cloud routing is configured as an opt-in path. | Local GGUF, OpenAI-compatible server, cloud provider adapter configuration, and the Workbench remote-control companion gate are documented in README and config references. | Tailscale Serve loopback binding is the supported tailnet-facing pattern; arbitrary remote exposure still requires operator networking and auth decisions. |
| RAG debugging includes rerank and quality signals. | RAG Debugger records query, candidate set, rerank breakdown, and faithfulness / relevance / recall / precision scores. | Quality claims still require eval evidence; a debugger verdict is not a promotion by itself. |
| Scheduled and automated work is represented in Workbench surfaces. | Workflow Builder, runtime scheduler, Training next-schedule state, Channels, and Run Kernel surfaces exist. | Generic cron-expression automation authoring is not a universal UI across every workflow. |

These claims are deliberately narrower than the product ambition. Missing
dynamic client registration, hosted marketplace parity, Streamable HTTP
single-endpoint server proof, and hosted governance positioning are
product/runtime gaps unless a later release adds those concrete artifacts.
Missing market distribution and public community presence remain separately
tracked in private release-planning records; they are not product/runtime known
limitations.

### Market Positioning Commitments

The May 2026 market-reality audit narrows the current story instead of
turning competitor signals into unsupported product claims:

- AM Workbench should describe its category as a **local-first AI workstation**.
  Developer surveys and community demand show a gap for
  local/self-hosted agent work, but this checkout does not yet include a
  public community program, distribution campaign, or survey submission. It
  also does not prove a current public release tag or external marketplace listing.
  Those are go-to-market evidence gaps, not product/runtime limitations; their
  evidence remains in private release-planning records.
- Governance messaging should lead with local data residency, immutable evidence spine records,
  mandatory promotion gates, and training-loop
  integration. Generic "audit trail" claims are no longer enough because
  IDE-integrated competitors are adding webhook and session-audit features.
  The local remediation is positioning clarity in docs/config; it is not a current-fact claim about market leadership.
- Trust messaging should emphasize human-in-the-loop promotion gates for
  production monitoring and project-planning work. AM Workbench should not
  claim autonomous production promotion as a supported default.
- Devstral Small 2 references must use audited benchmark figures from primary model
  documentation when cited. If citing the Ollama library page specifically,
  preserve the source-qualified 65.8% SWE-bench Verified figure rather than
  generalizing it as a current market-leadership claim. Model-license and
  release-approval review remain separate gates.
- EU AI Act language must not use stale August 2026 high-risk standalone system urgency framing.
  The supported product claim is local-first governance evidence and operator-controlled
  data flow, not GPAI-provider compliance coverage.

### Public Distribution Readiness Boundary

The strongest repo-local remediation for zero discoverability is a
public-safe category and launch packet, not a claim that launch already
happened. The maintained packet is
`docs/public/release-and-distribution-readiness.md`.

That page is allowed to claim:

- the category phrase **local-first AI workstation**;
- the implemented facts listed in this thesis, README, reference docs, and
  support config;
- the release checks required before publishing an export; and
- the exact non-claims that remain open.

It is not allowed to claim a public release tag, community presence, survey
recognition, marketplace distribution, dynamic OAuth client registration, or
hosted MCP marketplace presence unless those external/runtime artifacts exist
and are cited directly.

### Capability And Governance Closure Boundary

`CAPGAP-R5-001`, `CAPMAT-R5-001`, and `GT-R5-001` started as cluster-level
evidence-assembly blockers. Their old closure notes are not terminal evidence
by themselves. Terminal closure for those cluster rows is justified only when
each member FSA row is resolved with substantive source, test, docs, config,
UI, script, ADR, or README evidence outside workflow/status artifacts.

The current cluster boundary is:

| Blocker | Member findings | Product/runtime evidence now required for closure |
|---|---|---|
| `CAPGAP-R5-001` | `FSA-00030`, `FSA-00032`, `FSA-00033`, `FSA-00034`, `FSA-00035`, `FSA-00036`, `FSA-00037`, `FSA-00038` | Conversation export/search, prompt engineering surfaces, MCP runtime paths, analytics cost wiring, experiment-lab automation, arena evaluation, and cron workflow-builder evidence must remain resolved in the global full-spectrum closure file with live source/test refs. |
| `CAPMAT-R5-001` | `FSA-00039`, `FSA-00041`, `FSA-00042`, `FSA-00043` | RAG debugger scoring, durable execution recovery, prompt-security guardrails, and learning/persistence feedback-loop evidence must remain resolved with live source/test refs. |
| `GT-R5-001` | `FSA-00110`, `FSA-00111`, `FSA-00113`, `FSA-00116` | Planner/researcher verifier behavior, Inspector fail-closed verification, and OWASP LLM01 prompt-injection guardrail wiring must remain resolved with branch-discriminating tests or runtime source evidence. |

`config/support_matrix.yaml` records the same member-finding contract under
`audit_cluster_closure_readiness`, and
`tests/operator/test_docs_config_and_readme_hygiene.py` enforces that these blockers do
not terminally close on closure notes, verdicts, ledgers, or other
workflow-only evidence.

### Migration Contract Freeze Boundary

The private AM Workbench full-migration program freezes the post-remediation
baseline. Its Wave 1 inventory is the binding program-time source of truth for:

- language/runtime disposition of the 1,936-path post-remediation delta;
- Workbench surface maturity, default-state targets, and live-action ownership;
- MCP HTTP+SSE resource-streaming transport status;
- Training CLI server controls and dataset remote backend ownership; and
- known-limit remediation ownership.

Those artifacts are not product proof by themselves. They prevent false
closure: later packs must preserve or explicitly supersede remediation changes
with substantive source, test, docs, config, UI, schema, or workflow-script
evidence before this thesis or README can remove a current limitation.

### Audit and Brainstorm Baseline

Audit and brainstorm skills evaluate proposals against the full-suite scope
above, not against a factory-centric subset. A proposal targeting dataset
workflows, evaluation, prompt engineering, model lab, or capability
discovery is first-class scope. The lens for "gap" versus "non-goal" is
whether the proposal fits the self-improving, evidence-driven, local-first
frame -- not whether it routes through the factory pipeline.

## Capabilities

Capability maturity is evidence-record based. The live contract is
`vetinari/models/capability.py`, where `CapabilityMaturity` records use
`model_id`, `task_profile`, `basis`, `evidence`, `samples`, `pass_rate`, and
`last_validated_at_utc`. Promotion requires a fresh record, sufficient
`pass_rate`, a passing outcome, and tool evidence when the task profile
requires it.

No persisted `outputs/models/capability.jsonl` records were present when the
source audit was written, so this thesis does not claim any model or task
profile is promotion eligible by default. The support matrix may mark
selected workflows as validated when they have explicit proof commands, but
local inference backends, local training, and automatic self-improvement
remain bounded by their recorded support rows and current evidence.

Current capability claims should be read as conditional on configured
providers, available local runtimes, and the matching proof command or
capability record. Missing, stale, or unreadable capability state fails
closed and is not success.

A capability surface listed in "What Vetinari Is" is in scope regardless of
its current capability-record status. The support matrix and capability
JSONL determine current maturity; scope is not gated by current evidence.

## Non-Goals

The non-goal contract is project scoped. `NonGoalStore` persists records
under `outputs/projects/{project_id}/non_goals.jsonl`, and override appeals
under `outputs/projects/{project_id}/override_appeals.jsonl`. A `NonGoal`
can be an ordinary revision signal or a `hard_refuse` boundary.
`hard_refuse=True` requires a human-attested source such as `human:`,
`user:`, or `maintainer:`.

There is no global non-goals register in the live contract. Project-specific
non-goal records were not present in the checked workspace when the source
audit was written, so this thesis does not invent concrete project
prohibitions. The durable product boundary is that non-goals are explicit,
stored, and checked through deterministic match rules.

Product-level non-goals beyond the live store:

- AM Workbench is not positioned as a fully hosted SaaS, a hands-off
  autonomous production operator, or a substitute for human judgment on
  promotion, deployment, or destructive operations.
- Cloud-only operation is a configured mode, not the default trust posture.
- A capability being absent from the factory pipeline does not make it a
  non-goal; non-goal status requires a `hard_refuse` record or an explicit
  maintainer decision.

These product-level non-goals are framing statements, not durable refuse
rules. A maintainer who needs one enforced should add a `hard_refuse` record
with a `maintainer:` source so that `check_non_goals()` will reject matching
work.

## Support Boundaries

The support matrix in `config/support_matrix.yaml` is the authority for
hardware, operating system, and workflow support posture. It distinguishes
unsupported, experimental, validated, and promotion-eligible states, and
uses explicit known limitation names when a workflow lacks current runner
proof or a verified backend.

Windows 11 CLI, native kernel/Svelte workbench reachability, and the agent pipeline have
selected validation rows in the matrix. Local training, many local inference
backend combinations, and non-Windows host combinations remain limited
unless their matrix row and capability evidence say otherwise. Cloud
fallback and provider routing are configuration-dependent; they should not
be described as universal behavior.

Capability surfaces outside the matrix axes (training, prompt engineering,
dataset workflows, evaluation, model lab, capability discovery, operator
surfaces, observability) are governed by their own capability-record proof
and runbooks, not by the host / workflow matrix. Their absence from the
matrix is not a scope statement; it means the matrix is not the right
authority for that surface.

## Maturity Key

Unsupported means the current evidence does not justify use for that
workflow or host combination. Experimental means the surface may be
reachable but lacks enough current proof for production reliance. Validated
means the matrix points to a concrete proof command or equivalent evidence
for that surface. Promotion-eligible means a current capability record or
support row provides the extra evidence needed to promote beyond ordinary
validation.

All maturity labels are evidence statuses, not static enum promises. If the
support matrix, capability JSONL, or required proof artifact is missing or
corrupt, the correct interpretation is unavailable or blocked, not
implicitly validated.

Maturity applies independently per capability surface. An advanced agent
pipeline does not imply validated training, and a validated model lab
workflow does not imply validated automatic self-improvement.

## Training Flywheel

Vetinari closes a local-first training loop between everyday use and model
improvement. The flywheel runs:

1. The user interacts with an agent (planning, code, review, research).
2. The outcome — including manual feedback, automated quality scores, and
   downstream task results — is recorded in the training collector.
3. The synthetic data generator turns the recorded interactions into
   training pairs, redacting PII per the GDPR Art. 17 retention contract
   before the data is staged.
4. The training pipeline runs fine-tuning during idle periods. Adapters
   produced this way stay on disk; nothing is uploaded.
5. The model registry promotes the new artifact through shadow tests and
   promotion gates before it serves real traffic.

The loop matters because every byte stays on the user's machine. Cloud
assistants close their flywheel with telemetry routed to third parties.
Vetinari closes the same loop without leaving the workstation, so the
quality improvements compound for the user without sending interaction
logs anywhere.

The Plan-Do-Check-Act discipline from kaizen drives the flywheel cadence:
each weekly cycle plans the next experiment, the training pipeline runs
the change, the verification gate checks the result, and the kaizen
review acts on the evidence.

## Self-Improvement via Kaizen

The kaizen subsystem under `vetinari/kaizen/` runs a continuous PDCA cycle
against the running system. It is not a vanity dashboard — it is the
control loop that turns repeated mistakes into product fixes.

- **Plan:** Every Monday a fresh weekly digest is generated. The digest
  captures session pathology metrics (rough-rate, repeat-read counts,
  worst-session token usage) and proposes a single concrete improvement
  bet for the week.
- **Do:** The improvement bet is wired into the next week's runtime
  configuration. If it changes a model selection, the change goes through
  the model registry; if it touches prompts, the change goes through the
  prompt evolver with a shadow test.
- **Check:** The verification compliance audit grades the prior week's
  bet against measured metrics. A null metric value triggers a WARNING
  naming the upstream tool that failed, so the bet is never silently
  ungraded.
- **Act:** The defect tracker collects findings from the audit and from
  full-spectrum audit runs. Each finding is closed by code change,
  documentation update, or explicit waiver — never silently dropped.

Kaizen connects to the training flywheel through the defect tracker: any
recurring defect class is eligible for fine-tuning targeting. Local
fine-tunes that close a defect class are recorded as kaizen wins.

## Multi-Model Adapter Pool

Vetinari serves multiple local models concurrently. The adapter pool
maintains warm instances of every active GGUF or HF checkpoint without
forcing the user to restart the server when they swap models.

- **Concurrent instances:** The pool sizes each model against the
  workstation's memory budget. Models that exceed the budget are paged
  out to disk-resident cold instances and warmed back on demand.
- **Thompson Sampling routing:** Every task records a quality score
  against the model that served it. Thompson Sampling uses the running
  posterior over those scores to pick the model that is most likely to
  do best on the next task of the same shape, while still occasionally
  exploring alternatives so the routing does not lock in.
- **Hot swap:** New GGUF files dropped into the configured models
  directory are picked up by the model discovery pipeline without a
  restart. The model is verified, registered, and added to the pool's
  candidate set with an empty routing prior so it earns its way into
  production through real measurement.

## Episodic Memory and Framework Comparison

Vetinari's episodic memory is a first-class capability — sessions, tasks,
and outcomes are stored as searchable, addressable episodes rather than
opaque chat logs.

- **Episode store:** The unified memory store records one episode per
  durable task with provenance, retrieved context, generated output,
  and downstream feedback. The store is local-only.
- **Recall:** Agents look up prior episodes by semantic similarity, by
  goal class, and by recency. The recall path is the same one that
  feeds the synthetic data generator, so improvement bets that depend
  on better recall improve the training flywheel as well.
- **Framework comparison:** Hosted frameworks such as LangGraph and
  LlamaIndex assume their persistence layers will run in a cloud or
  managed service. Vetinari's episodic memory is intentionally
  workstation-shaped: SQLite-backed, encrypted at rest, with the same
  GDPR retention rules as the rest of the local data stack. The
  user trades framework-vendor integrations for full data locality.

## Durable Execution and Checkpointer

Long-running pipelines — training jobs, multi-stage agent plans,
release operations — survive process restarts through a checkpoint store.

- **Checkpointer:** Every durable workflow writes a checkpoint after each
  stage. Restart re-reads the most recent checkpoint, re-validates its
  inputs, and resumes from the next stage. Workflows do not roll back to
  the start of the pipeline on crash.
- **Idempotency:** Each stage names its inputs and outputs explicitly so
  a re-run of the same checkpoint is deterministic. Stages that touch
  external state (model downloads, file writes) use atomic-replace
  patterns so partial work is never visible.
- **Damaged state behavior:** If a checkpoint cannot be deserialized —
  schema drift, corrupted bytes, missing dependency — the workflow halts
  with a typed failure and a named blocker, not a silent retry.

## OpenTelemetry Configuration

OpenTelemetry instrumentation is optional and off by default. Local
development never pays the OTel cost; production deployments may opt in
by installing the `opentelemetry` package and configuring exporters
through environment variables.

Configuration:

- `VETINARI_TRACE_PII_TTL_DAYS` — retention window (default 30 days) for
  PII-tagged span attributes under the `pii.` prefix. The GDPR Art. 17
  retention contract enforces this window.
- `VETINARI_LOG_PII_TTL_DAYS` — same window for log records that carry
  request bodies or user-identifying fields.
- `OTEL_EXPORTER_OTLP_ENDPOINT` — endpoint for the chosen OTLP exporter.
  Unset means traces stay in the local `NoOpSpan` log-only path.
- `OTEL_SERVICE_NAME` — service name reported on every span. Defaults to
  `vetinari` when OpenTelemetry is enabled.

The instrumentor installs span hooks for the pipeline, stages, agents,
and LLM calls. When OpenTelemetry is not installed, the same call sites
fall through to `NoOpSpan` so the application does not need a separate
configuration path.

## Skills and Portable Naming

The skills system under `vetinari/skills/` loads named workflows from a
disk-resident catalog. Portable naming applies: a skill name is the same
across every host that loads it. The catalog loader rejects names that
collide with built-ins or that violate the portable-naming contract so
two skills cannot register the same identifier.

<!-- identity-correction:identity-backend-sglang-sample -->
### Identity Correction: identity-backend-sglang-sample

- Summary: Record SGLang as a canonical backend surface in project identity materials.
- Source gap: The user-surfaced canonical-source gap is that the thesis can omit SGLang while the snapshot backend roster includes it.
- Propagation: SGLang is part of the canonical AM Workbench backend roster and must stay visible in product-thesis backend language.
