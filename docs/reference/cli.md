# AM Workbench CLI Reference

**Entry point:** `python -m vetinari <command>` or `vetinari <command>` (if installed via pip)

---

## Quick Reference

| Command | Category | Summary |
|---|---|---|
| `run` | Core | Execute a goal or manifest task |
| `serve` | Core | Start the web dashboard |
| `start` | Core | Start AM Workbench |
| `status` | Core | Show system status |
| `health` | Core | Health check all providers |
| `interactive` | Core | Enter interactive REPL |
| `prompt` | Core | Manage agent prompt versions |
| `migrate` | Core | Apply database migrations |
| `upgrade` | DevOps | Check for model upgrades |
| `review` | DevOps | Run self-improvement agent |
| `benchmark` | DevOps | Run agent benchmarks |
| `mcp` | DevOps | Start MCP server |
| `diagnose` | DevOps | Trace project execution history |
| `drift-check` | DevOps | Run full drift audit |
| `kaizen` | Training | Continuous improvement reports |
| `train` | Training | Manage idle-time training |
| `watch` | Training | File watcher for @vetinari directives |
| `init` | Package | First-run setup wizard |
| `doctor` | Package | Run packaging and runtime-readiness diagnostic checks |
| `models` | Package | List models across all configured backends; download/scan operates on llama-cpp GGUF files (other backends resolve by HuggingFace repo ID or backend-native identifier) |
| `python -m vetinari.cli_backend_overlay` | Package utility | Validate and plan governed backend overlay patches |
| `forget` | Package | Purge learned data for a project |
| `config` | Package | Configuration management |
| `resume` | Package | Resume interrupted plan from checkpoint |
| `explain` | Package | Explain what a file does |
| `test` | Package | Generate tests for a file |
| `fix` | Package | Fix issues in a file |

---

## Global Flags

Applies to all commands.

| Flag | Type | Default | Description |
|---|---|---|---|
| `--config PATH` | path | `manifest/vetinari.yaml` | Manifest file path |
| `--mode {planning,execution,sandbox}` | choice | `execution` | Execution mode |
| `--verbose` / `-v` | flag | — | Enable debug logging |

---

## Core Commands

### `run`

Execute a goal or manifest task.

```bash
vetinari run --goal "Refactor the auth module"
vetinari run --task auth-refactor-001
```

| Flag | Type | Description |
|---|---|---|
| `--goal` / `-g` TEXT | str | High-level goal to execute |
| `--task` / `-t` TEXT | str | Specific manifest task ID |

---

### `serve`

Start the native Rust kernel API server for the migrated Workbench API surface.

```bash
vetinari serve
vetinari serve --port 8080 --web-host 0.0.0.0
vetinari serve --debug
```

| Flag | Type | Default | Description |
|---|---|---|---|
| `--port` INT | int | `5000` (via `VETINARI_WEB_PORT`) | Server port |
| `--web-host` TEXT | str | `127.0.0.1` | Bind address |
| `--debug` | flag | — | Enable debug mode |

---

### `start`

Start AM Workbench with the optional native Rust kernel API server. Running `vetinari` without a subcommand prints help and exits without starting services.

```bash
vetinari start
vetinari start --goal "Build the login page" --port 8080
vetinari start --no-dashboard --skip-preflight
```

| Flag | Type | Default | Description |
|---|---|---|---|
| `--goal` / `-g` TEXT | str | — | Execute goal on startup |
| `--task` / `-t` TEXT | str | — | Execute task on startup |
| `--port` INT | int | — | Dashboard port |
| `--no-dashboard` | flag | — | Disable web dashboard |
| `--skip-preflight` | flag | — | Skip dependency preflight check |

---

### `status`

Show human-readable system status including models, providers, and learning state. Takes no arguments. This is intended for operators, not as a strict automation health gate.

```bash
vetinari status
```

---

### `health`

Health check all configured providers and print diagnostics. Takes no arguments. The command is human-readable and may continue after degraded checks; use `doctor --json` when a machine-readable packaging diagnostic is required.

```bash
vetinari health
```

---

### `interactive`

Enter the interactive REPL for conversational task execution.

```bash
vetinari interactive
```

**Special REPL commands:**

| Command | Action |
|---|---|
| `/quit` | Exit the REPL |
| `/exit` | Exit the REPL |
| `/status` | Show current system status |
| `/review` | Trigger a review cycle |
| `/help` | Show available commands |

---

### `prompt`

Manage agent prompt versions — view history or roll back to a prior version.

```bash
vetinari prompt history WORKER
vetinari prompt rollback WORKER --version v3 --mode build
```

| Argument / Flag | Type | Required | Description |
|---|---|---|---|
| `action` | `history` or `rollback` | Yes | Operation to perform |
| `agent` | str | Yes | Agent type (e.g. `WORKER`, `FOREMAN`, `INSPECTOR`) |
| `--mode` TEXT | str | No (default: `build`) | Agent mode |
| `--version` TEXT | str | Yes for rollback | Version to roll back to |

`prompt history` and `prompt rollback` update version-history state on disk. They do not hot-reload an already running server process.

---

### `migrate`

Apply database migrations.

```bash
vetinari migrate
vetinari migrate --db-path /data/vetinari.db
```

| Flag | Type | Default | Description |
|---|---|---|---|
| `--db-path` TEXT | str | `VETINARI_DB_PATH` or `.vetinari/vetinari.db` | SQLite database path |

---

## DevOps Commands

### `upgrade`

Check for model upgrades by discovering available local models and comparing against current selections.

```bash
vetinari upgrade
```

---

### `review`

Run the self-improvement agent to generate performance recommendations.

```bash
vetinari review
```

---

### `benchmark`

Run agent benchmarks and report regressions.

```bash
vetinari benchmark
vetinari benchmark --agents WORKER INSPECTOR
vetinari benchmark --case toolbench:tb-l1-001
```

| Flag | Type | Description |
|---|---|---|
| `--agents` TEXT... | list[str] | Specific agent types to benchmark |
| `--case` TEXT | str | Run one benchmark case using `SUITE:CASE_ID` format |

The default command runs the legacy agent benchmark harness. `--case` uses the multi-suite benchmark runner; case IDs are the adapter's live IDs, for example `toolbench:tb-l1-001`, not `toolbench:tc001`.

---

### `mcp`

Start the MCP (Model Context Protocol) server for editor integration.

```bash
vetinari mcp
vetinari mcp --transport http
```

| Flag | Type | Default | Description |
|---|---|---|---|
| `--transport {stdio,http}` | choice | `stdio` | Transport mode |
| `--mcp-port` INT | int | `8765` | HTTP port (http transport only) |
| `--mcp-host` TEXT | str | `127.0.0.1` | HTTP bind address (http transport only) |

`--transport http` does not launch a standalone MCP HTTP server. HTTP MCP requests are served by the native Rust kernel API as JSON-RPC at `POST /mcp/message`; start the Workbench API host before using the HTTP transport.

---

### `diagnose`

Inspect available project files and recent event/SSE logs for a project. This is a diagnostic snapshot, not a full distributed execution-history timeline.

```bash
vetinari diagnose my-project-id
```

| Argument | Description |
|---|---|
| `PROJECT_ID` | ID of the project to trace |

---

### `drift-check`

Run a full drift audit comparing current agent behavior against the established baseline.

```bash
vetinari drift-check
vetinari drift-check --update
```

| Flag | Description |
|---|---|
| `--update` | Regenerate the drift baseline instead of checking against it |

---

## Training Commands

### `kaizen`

Continuous improvement commands.

```bash
vetinari kaizen report
vetinari kaizen gemba
```

| Subcommand | Description |
|---|---|
| `report` | Weekly summary of active improvement initiatives |
| `gemba` | On-demand execution review (Gemba Walk) |

---

### `train`

Manage idle-time training.

```bash
vetinari train status
vetinari train start --skill summarization
vetinari train run
vetinari train data
vetinari train seed
vetinari train curriculum
vetinari train history
```

| Subcommand | Description |
|---|---|
| `status` | Show training state and idle duration |
| `start` | Manually trigger a training cycle |
| `run` | Run the training pipeline now |
| `pause` | Legacy parser entry; returns nonzero unsupported because no local scheduler control is wired |
| `resume` | Legacy parser entry; returns nonzero unsupported because no local scheduler control is wired |
| `data` | Show training data statistics |
| `seed` | Download default training datasets |
| `curriculum` | Show the next scheduled training activity |
| `history` | Show past training runs per agent |

| Flag | Description |
|---|---|
| `--skill` TEXT | Train a specific skill (used with `start`) |

---

### `watch`

File watcher that monitors a directory for `@vetinari` directives and records scan/report output. `watch scan` is a one-shot detector; `watch start` runs until interrupted and only processes directives supported by the runtime watcher.

```bash
vetinari watch start
vetinari watch start --dir ./src --interval 5.0
vetinari watch report
vetinari watch scan
```

| Subcommand | Description |
|---|---|
| `start` | Start the file watcher |
| `report` | Show directive execution report |
| `scan` | Scan directory once for pending directives |

| Flag | Type | Default | Description |
|---|---|---|---|
| `--dir` TEXT | str | `.` | Directory to monitor |
| `--interval` FLOAT | float | `2.0` | Poll interval in seconds |
| `--no-directives` | flag | — | Disable directive scanning |

---

## Package Management Commands

### `init`

First-run setup wizard. Guides through model selection and initial configuration.

```bash
vetinari init
vetinari init --skip-download
vetinari init --dry-run --modality text
```

| Flag | Description |
|---|---|
| `--skip-download` | Skip model download and print the URL instead |
| `--dry-run` | Print setup recommendations without running the wizard or writing files |
| `--modality` TEXT | Comma-separated modalities for dry-run catalog recommendations |

---

### `doctor`

Run the packaging and runtime-readiness diagnostic inventory and report system health.

```bash
vetinari doctor
vetinari doctor --json
```

| Flag | Description |
|---|---|
| `--json` | Output results as machine-readable JSON |

**Checks performed:**

| # | Check |
|---|---|
| 1 | Python >= 3.10 |
| 2 | GPU detection |
| 3 | CUDA toolkit |
| 4 | llama-cpp-python |
| 5 | vLLM package |
| 6 | vLLM endpoint |
| 7 | NIM endpoint |
| 8 | Models directory |
| 9 | Model file header |
| 10 | SQLite database |
| 11 | Config files |
| 12 | Security module |
| 13 | Agent pipeline |
| 14 | Memory store |
| 15 | Disk space |
| 16 | Web port |
| 17 | Stale lock files |
| 18 | Thompson sampling state |
| 19 | Training data store |
| 20 | Episode memory |
| 21 | Rich pretty output |
| 22 | Dependency groups |
| 23 | Dependency readiness matrix |
| 24 | CUDA readiness |
| 25 | Backend registration |

---

### `models`

List configured models across all inference backends and manage on-disk assets.

`models list` enumerates every model the loaded config knows about, regardless of provider. `models scan` and `models download` operate on local llama-cpp GGUF files under `VETINARI_MODELS_DIR`; for vLLM/SGLang/NIM, point the backend at a HuggingFace repo ID via the relevant `VETINARI_*_MODEL` variable; for faster-whisper, use a CTranslate2 repo ID; for hosted providers (LiteLLM-fronted), set the API-key environment variable — no local files are required.

```bash
vetinari models list
vetinari models download --repo TheBloke/Mistral-7B-GGUF --filename mistral-7b.Q4_K_M.gguf
vetinari models remove --name mistral-7b
vetinari models info --name mistral-7b
vetinari models recommend
vetinari models scan
vetinari models check
```

| Subcommand | Description |
|---|---|
| `list` | Show all local models |
| `download` | Fetch a model from HuggingFace |
| `remove` | Delete a model file |
| `info` | Show model metadata |
| `recommend` | Suggest models for detected VRAM |
| `scan` | Discover `.gguf` / `.awq` files on disk |
| `check` | Check for newer, better-performing models |

`models list` is local GGUF artifact inventory under the configured model roots. It is not a live loaded-model or process-status view.

| Flag | Required for | Description |
|---|---|---|
| `--repo` TEXT | `download` | HuggingFace repo ID |
| `--filename` TEXT | `download` | GGUF filename within the repo |
| `--name` TEXT | `remove`, `info` | Model name to match |

---

### Backend overlay utility

Backend overlay planning is not a `vetinari` subcommand. Invoke the module
entry point directly:

```bash
python -m vetinari.cli_backend_overlay check
python -m vetinari.cli_backend_overlay dry-run --backend vllm
python -m vetinari.cli_backend_overlay apply-plan --backend vllm --approve
python -m vetinari.cli_backend_overlay rollback --backend vllm
python -m vetinari.cli_backend_overlay rebase-status --backend vllm
```

| Subcommand | Description |
|---|---|
| `check` | Validate overlay manifests without applying patches |
| `dry-run` | Print patch-check commands for one backend |
| `apply-plan` | Build an approval-gated apply plan |
| `rollback` | Print the recorded rollback command |
| `rebase-status` | Print the recorded overlay rebase status |

| Flag | Default | Description |
|---|---|---|
| `--manifest PATH` | `third_party_overlays/overlays.example.yaml` | Overlay manifest YAML path |
| `--pins PATH` | `config/backend_pins.yaml` | Backend pins YAML path |
| `--backend TEXT` | `vllm` | Backend key to inspect |
| `--approve` | off | Required by `apply-plan` before a ready apply plan is returned |

---

### `forget`

Purge all learned data (memory, training records, episodes) for a named project.

```bash
vetinari forget --project my-project
```

| Flag | Required | Description |
|---|---|---|
| `--project` TEXT | Yes | Project name to purge |

---

### `config`

Configuration management.

```bash
vetinari config reload
```

| Subcommand | Description |
|---|---|
| `reload` | Reload settings for this CLI invocation |

`config reload` is a one-shot settings reset in the current process. It does not hot-reload a separately running server.

---

### `resume`

Resume an interrupted plan from its last checkpoint.

```bash
vetinari resume <PLAN_ID>
```

| Argument | Description |
|---|---|
| `PLAN_ID` | ID of the plan to resume |

---

### `explain`

Explain what a file does in plain language.

```bash
vetinari explain vetinari/agents/inference.py
```

| Argument | Description |
|---|---|
| `FILE` | Path to the file to explain |

---

### `test`

Generate tests for a file.

```bash
vetinari test vetinari/token_compression.py
```

| Argument | Description |
|---|---|
| `FILE` | Path to the file to generate tests for |

---

### `fix`

Fix issues in a file.

```bash
vetinari fix vetinari/adapters/litellm_adapter.py
```

| Argument | Description |
|---|---|
| `FILE` | Path to the file to fix |
