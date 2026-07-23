# AM Workbench

A local-first GenAI workbench for a single workstation. Run goals through
an agent pipeline, manage and train your models, build retrieval and
memory workflows, monitor your hardware and capabilities, debug what came
back, and promote what worked — all from one app, against your own data,
with nothing leaving your machine unless you choose.

AM Workbench is for one person doing serious GenAI work: tuning prompts,
testing decomposition strategies, comparing model behavior, training
adapters, building RAG pipelines, and tracking what actually worked. The
supported deployment is one Python venv, one local web app on
`127.0.0.1`, your choice of local or cloud inference backends, and your
own data on your own disk.

AM Workbench is private-by-default: models, memory, training data, run
traces, and approval decisions stay on your machine unless you explicitly
configure a cloud backend. It is also a workflow orchestrator — goals
decompose into typed task graphs that run through a three-agent pipeline
with evidence recorded at every step.

## The loop

The day-to-day flow:

1. **Run.** Submit a goal in Chat. The Foreman plans it, the Worker
   executes the tasks, the Inspector gates each result with mandatory
   pass/fail.
2. **Inspect.** Open Console for queryable run/trace/asset history. Open
   Mission Control for live in-project queue and lease state.
3. **Experiment.** Open Playground to replay any run with a different
   prompt, model, or tool. Edits are scratch — the source trace is never
   mutated.
4. **Promote.** Open Promotion Inbox to advance a winning prompt, model,
   dataset, adapter, or pipeline change through deterministic eval /
   provenance / rollback / taint gates.

Every transition writes evidence to the **metadata spine** (an append-only
JSONL log with a SQLite index). Console, Shell, Work Graph, and the rest
of the read-only views all rebuild from it.

## What's in the box

**Run agents.** Three-agent pipeline (Foreman → Worker → Inspector)
wrapped in an 8-stage assembly line and a parallel DAG executor. The
Foreman decomposes goals into a task graph; the Worker handles research,
architecture, build, and operations modes; the Inspector gates every
result with up to three correction rounds before escalating.

**Manage models.** Browse models across every configured backend
(llama-cpp GGUF files, vLLM-compatible OpenAI endpoints, Ollama tags,
faster-whisper CTranslate2 models, ComfyUI checkpoints, NeMo/Riva endpoints,
and hosted providers), search and pull from Hugging Face for the backends
that consume HF assets, configure defaults per task type, and promote new
defaults through deterministic gates. Cloud adapters for Anthropic, OpenAI,
Gemini, Cohere, HuggingFace, and Replicate sit alongside local llama.cpp,
vLLM-compatible endpoint, Ollama, faster-whisper, ComfyUI, and NeMo/Riva
backends.

**Train and adapt.** Configure QLoRA / DoRA training runs, manage idle and
kaizen training cycles, and feed quality scores back into Thompson
Sampling arms that learn which models perform best for which task types.

**Memory and retrieval.** Three-layer memory (session, long-term SQLite +
FTS5, embeddings) with deduplication, hash-chain integrity, and BM25
ranking. RAG Debugger records retrieval experiments — query, candidate
set, rerank breakdown, faithfulness verdict — for inspection.

**Operate.** Resource Cockpit watches GPU, RAM, CPU, and SSD utilization
with live lease and queued-job visibility. Status runs on-demand health
checks (cost overruns, credential expiry, scheduler lag, model
availability) with concrete fix actions. Readiness gates admission
against thirteen signal families and fails closed on unknown. VRAM
preflight checks and the optional Docker worker sandbox help keep local
runs inside the machine resources and isolation boundary you expect.

**Experiment and learn.** Playground replays any run with edits held in
scratch state. Method Library catalogs tested methods (prompts,
decomposition strategies, inference configs) by promotion status. Prompt
mutation runs eight deterministic operators through A/B tests with
statistical significance and shadow-test gates before promotion.

**Workbench surfaces.** Console, Shell, Promotion Inbox, Approval Chain,
Work Graph, Capability Packs, Local Runtime Onboarding —
keyboard-first, evidence-driven operator surfaces. Agent-to-agent handoff
metadata is exposed through A2A agent cards so local orchestration can be
inspected without making a cloud control plane the source of truth.

The broader app (Dashboard, Projects, Tasks, Models, Memory, Training,
Settings, Plan Builder, Output, Capabilities) wraps the workbench with
project management and global controls.

## Status

Honest snapshot of what's wired versus in-flight.

The AM Workbench full-migration program now has a Wave 1 contract-freeze
inventory that reconciles the post-remediation changed-file baseline and owns
known-limit remediation targets. The machine-readable inventory and remediation
ledger are maintainer release artifacts; the public status page is
[`docs/status/known-limitations.md`](docs/status/known-limitations.md).
Those artifacts do not make future migration work complete; they block later
packs from silently dropping remediation fixes or accepting unknown safety,
provenance, governance, dataset, live-action, training, or MCP transport state.

| Area | State |
|---|---|
| Agent pipeline + DAG executor + correction loop | Production |
| Workbench: Console, Shell, Playground, Promotion Inbox, Approval Chain, Readiness, Status, RAG Debugger, Local Runtime Onboarding, Work Graph, Evidence Assets, Metadata Spine | Production |
| Workbench: Method Library, Adaptive Tuning, Resource Cockpit, Capability Packs, Domain Kits, Workflow Builder, Channels, Benchmark Importer | [Backend-backed surfaces](docs/reference/workbench-wip-surfaces.md); routes, UI entry points, native-kernel bridge prefixes, and caveated operator semantics are present |
| Workbench: Migration Wizard, Habit Health, Extensions Marketplace | Route/backend proof is present; Migration Wizard is default-on, Habit Health and Extensions Marketplace remain explicit opt-in surfaces |
| Thompson Sampling model selection | Production, wired into the default path |
| Prompt mutation + A/B testing (8 operators, statistical gates) | Production, on by default (`PROMPT_EVOLVER_ENABLED`) |
| Best-of-N candidate generation | Production, wired |
| Cost prediction (calibrated regression after 50+ records) | Production, wired |
| Memory (session + SQLite/FTS5 + embeddings) | Production, wired |
| Cascade routing (cost-tiered escalation) | Production, default request path; explicit `use_cascade=False` is the direct-provider override |
| ImprovementLog / Kaizen PDCA | Propose/report path is wired; apply/revert is limited to registered applicators and is not in the default execution loop |
| MCP server + client (subprocess stdio + Litestar Streamable HTTP at `/mcp`, JSON-RPC at `/mcp/message`, HTTP+SSE resources at `/mcp/resources/stream`) | [Production for the documented transports](docs/reference/mcp-server.md); OAuth-backed marketplace rows support PKCE authorization request, token exchange, and bearer-authenticated install probing |

## Desktop App Architecture

**Rust/Tauri is the primary frontend host.** The desktop app is a Tauri shell
(`src-tauri/`) that calls into Vetinari functionality via native Tauri commands
(see `src-tauri/src/commands.rs`). The three entry-point commands are:

| Command | Description |
|---|---|
| `vetinari_status` | Returns runtime status and shell version |
| `vetinari_list_runs` | Returns recent Workbench run summaries |
| `vetinari_list_models` | Returns registered inference model summaries |

Litestar serves the HTTP IPC layer for non-desktop access and for command
handlers that delegate to Python implementations (agent pipeline, training,
memory, model management). The desktop frontend calls Tauri commands directly;
Tauri commands that need Python services call the Litestar IPC server at
`http://127.0.0.1:5000`. Python logic never owns the desktop window or the
Tauri application lifecycle.

## Install

Python 3.11+ on Windows, Linux, or macOS. The supported environment is a
project venv at `.venv312`.

```bash
git clone <repo> am-workbench
cd am-workbench
python -m venv .venv312
. .venv312/Scripts/activate     # Windows: .venv312\Scripts\activate
                                # *nix:    source .venv312/bin/activate
pip install -e ".[core,local]"
python -m vetinari init
```

`[local]` installs `llama-cpp-python` for GGUF inference. Add `[cloud]`
for Anthropic, OpenAI, or Gemini. Add `[vllm]` on Linux or macOS for
native vLLM. On Windows, vLLM runs through WSL — install vLLM inside an
Ubuntu WSL distro, start `vllm serve` on port 8000, and point AM Workbench
at it with `VETINARI_VLLM_ENDPOINT=http://localhost:8000`.

## Run

```bash
python -m vetinari start              # CLI + local API server on http://127.0.0.1:5000
python -m vetinari serve --port 5000  # API server only
python -m vetinari doctor --json      # health checks
python -m vetinari interactive        # REPL goal loop
```

`doctor` checks Python, GPU, llama-cpp-python, vLLM endpoint health,
SQLite, config files, agent wiring, memory store, disk space, and web
ports.

## Configure

Configuration lives in `config/` (YAML) and environment variables. Cold
reload only — changes require a restart.

| Variable | Purpose |
|---|---|
| `VETINARI_MODELS_DIR` | llama-cpp GGUF model directory — other backends ignore this (default: `~/.vetinari/models`) |
| `VETINARI_NATIVE_MODELS_DIR` | HuggingFace-format checkpoint root for local OpenAI-compatible model servers that consume safetensors or optional GPTQ/AWQ assets |
| `VETINARI_VLLM_ENDPOINT` | OpenAI-compatible endpoint when running vLLM |
| `VETINARI_WEB_PORT` | Web app port (default: 5000) |
| `VETINARI_ADMIN_TOKEN` | Required before exposing admin routes beyond local trusted use |
| `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` / `GEMINI_API_KEY` | Optional cloud backends |

## Limits

- **Single-user, localhost by default.** No multi-user auth, no per-user
  data isolation, no built-in TLS. Put a reverse proxy in front before
  exposing beyond localhost.
- **Cold config reload only.** Changes to `config/` and env vars require
  a restart.
- **vLLM on Windows runs through WSL**, not native — the supported
  pattern is a Windows-side AM Workbench process pointing at a WSL-hosted
  vLLM endpoint.
- **No automatic backups.** The spine, SQLite stores, and JSONL logs in
  your data directory are yours to back up.
- **Cloud adapters are minimally tested in production workflows.** The
  local path is the well-trodden one.

## Trust controls

AM Workbench is a local-first tool. The following controls govern what leaves your machine, what persists, and who can act:

**Network egress.** By default, nothing leaves the machine. Local GGUF inference stays fully on-device. AM Workbench can run in an air-gapped environment with no internet access when using only local backends. Cloud backends (Anthropic, OpenAI, Gemini) are opt-in — you supply the API key and nothing is sent unless a cloud adapter is explicitly configured. The `VETINARI_VLLM_ENDPOINT` setting can point at a WSL-local vLLM instance, which is still on-machine.

**Permissions and authorization.** The default server binds to `127.0.0.1` only. Set `VETINARI_ADMIN_TOKEN` before exposing admin routes beyond localhost. No multi-user RBAC is built in — see [Limits](#limits) for the single-user scope.

**Data retention and deletion.** All persistent state lives in your data directory: the metadata spine (append-only JSONL), SQLite stores (memory, runs, training records), and JSONL logs. Nothing is written outside that directory. There is no automatic backup or remote sync. You own the files; delete them to remove the data.

**Agent audit and session tracking.** Every agent run writes a trace to the metadata spine (append-only JSONL + SQLite index). Console and Work Graph rebuild from this evidence. No session data is sent to Anthropic or any third party unless you configure a cloud backend.

**Version and rollback evidence.** Prompt versions, promotion candidates, backend tuning overlays, upgrade snapshots, and rollback plans are local artifacts. The product claim is reviewable local evidence and operator-controlled rollback, not hosted enterprise audit-log service parity.

**MCP and extension auth.** Local MCP HTTP endpoints use the local-user guard documented in [`docs/reference/mcp-server.md`](docs/reference/mcp-server.md): loopback clients are allowed and remote clients require the configured admin token. Marketplace rows can carry OAuth metadata for PKCE authorization-url construction, token exchange, and bearer-authenticated MCP install probing; rows remain metadata/risk inputs that stay disabled by default until Workbench-owned checks allow selection.

**Provider disclosure.** If a cloud adapter is in use, prompt content goes to that provider under their terms. Local GGUF and local vLLM inference never leave the machine. The `doctor` command reports which backends are active.

**Observability exporters.** No telemetry is sent by default. No telemetry exporter is configured unless you explicitly set `OTEL_EXPORTER_OTLP_ENDPOINT` or follow the instrumentation examples in `vetinari/observability/tracing.py`. When opt-in is configured, pipeline spans — including truncated prompt and response content (capped at 8 KB per span attribute) — are sent to the destination you chose (e.g. Langfuse, Honeycomb, Grafana Cloud). Nothing exports until you configure it.

For the full data inventory see [`docs/security/data-inventory.md`](docs/security/data-inventory.md) and [`docs/security/route-auth-matrix.md`](docs/security/route-auth-matrix.md).

## Design position vs IDE-integrated tools

IDE-integrated coding agents (Cursor, GitHub Copilot, Windsurf, and similar) embed AI assistance inside the editor or inside the GitHub issue-to-PR workflow. AM Workbench deliberately does not do this.

**The intentional trade-off:** IDE integration means the tool lives where the code is and can propose and apply changes in-context. The cost is that prompts, context, and intermediate reasoning typically leave the machine and pass through a cloud service. For users who are fine with that trade-off, IDE-integrated tools are excellent.

AM Workbench makes the opposite bet: the full pipeline — models, memory, training data, run traces, approval decisions, and the model-selection reasoning itself — stays on your machine. You give up native IDE/GitHub workflow integration in exchange for:

- No vendor account or API key required for inference (local GGUF models)
- No prompt content leaving the machine unless you opt into a cloud backend
- Full audit trail ownership (spine JSONL, SQLite stores, JSONL logs — all local files you control)
- The ability to train and promote your own adapters using your own traces

If you want your models in your IDE, use Cursor or Copilot. If you want your data on your disk and your models under your control, AM Workbench is for that use case. These are not competing on the same axis.

The current product thesis and implemented evidence map live in
[`docs/product-thesis.md`](docs/product-thesis.md). That page distinguishes
implemented local governance, conversation export/search, prompt engineering,
MCP transports, and hybrid local/cloud configuration from still-open gaps such
as OAuth-backed MCP marketplace installation and public community distribution.

The public distribution boundary lives in
[`docs/public/release-and-distribution-readiness.md`](docs/public/release-and-distribution-readiness.md).
It provides category wording and a launch packet, but it is not proof of a
public release tag, community presence, survey recognition, or marketplace
listing.

**IDE integration:** The local HTTP server on `127.0.0.1:5000` exposes a REST API, and the local-first IDE submission service accepts goals only from loopback, a bound Workbench session, a trusted origin, and a matching CSRF token. This keeps editor-side trigger points local instead of adding a cloud control plane.

## Community

Community resources are at [`docs/community/README.md`](docs/community/README.md)
and available in the terminal with:

```bash
python -m vetinari community
```

- **GitHub Discussions** — Q&A, ideas, and workflow showcases:
  <https://github.com/StrategicMilk/Vetinari/discussions>
- **Issue tracker** — bug reports and feature requests:
  <https://github.com/StrategicMilk/Vetinari/issues>
- **Contributing guide** — [`CONTRIBUTING.md`](CONTRIBUTING.md)

## License

MIT. See [`LICENSE`](LICENSE).
