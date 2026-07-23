# AM Workbench — Quick Start Guide

Get from zero to a running system in under 10 minutes.

---

## Prerequisites

| Requirement | Minimum | Check |
|---|---|---|
| Python | 3.11+ | `python --version` |
| Git | any | `git --version` |
| Inference backend | At least one of: llama-cpp (GGUF), vLLM, NIM, SGLang, faster-whisper, ComfyUI, an OpenAI-compatible local server, or a cloud provider via LiteLLM | Provision via `vetinari init` — the wizard probes hardware and configures whichever backends are available |

`vetinari init` selects the primary backend based on detected hardware: NVIDIA + CUDA prefers NIM/vLLM, other GPUs prefer vLLM, CPU-only falls back to llama-cpp. Hosted providers (Anthropic, OpenAI, Gemini, Cohere, HuggingFace, Replicate) route through LiteLLM and need no local server. GGUF is one file format used by the llama-cpp adapter only; vLLM/SGLang use safetensors with optional GPTQ/AWQ quantization, faster-whisper uses CTranslate2 models, and ComfyUI uses safetensors/ckpt.

---

## 1. Clone and install

```bash
git clone https://github.com/StrategicMilk/AM-Workbench.git
cd AM-Workbench

python -m venv .venv312
source .venv312/bin/activate
# Windows PowerShell: .venv312\Scripts\Activate.ps1

python -m pip install -e ".[dev]"
```

Use optional groups only for the workflow you are setting up:

| Workflow | Install |
|---|---|
| Contributor tests and lint | `python -m pip install -e ".[dev]"` |
| Local inference (llama-cpp GGUF + cloud-via-LiteLLM) | `python -m pip install -e ".[dev,local]"` |
| Search and notification integrations | `python -m pip install -e ".[dev,search,notifications]"` |
| Embedding/vector workflows | `python -m pip install -e ".[dev,ml]"` |
| Full local operator workstation | `python -m pip install -e ".[dev,local,ml,search,notifications]"` |

`uv` users can substitute `uv pip install -e ".[dev]"` or the matching
optional-group set from the table. Add `.[training]` only when you need
fine-tuning, and `.[vllm]` only when you intend to run the vLLM backend. The
`pyproject.toml` is the authoritative source of dependencies.

---

## 2. First-run setup

Run the interactive setup wizard. It detects your hardware, recommends models, and writes `~/.vetinari/config.yaml`:

```bash
python -m vetinari init
```

The wizard will:

- Detect available CPU/GPU resources
- Recommend a model path appropriate for your hardware
- Offer to download a model from HuggingFace if you do not already have one
- Write initial configuration to `~/.vetinari/config.yaml`

Place model assets in the directory matching the backend you intend to use:

- **llama-cpp**: GGUF files under `VETINARI_MODELS_DIR` (default `./models`)
- **vLLM / SGLang / NIM**: HuggingFace-format checkpoints (safetensors, optional GPTQ/AWQ) under `VETINARI_NATIVE_MODELS_DIR` (default `./models/native`), or reference them by Hugging Face repo ID and let the backend download on first use
- **faster-whisper**: CTranslate2 model directories, downloaded automatically by repo ID on first use
- **ComfyUI**: safetensors/ckpt under the ComfyUI checkpoints directory
- **Hosted providers (Anthropic/OpenAI/Gemini/Cohere/HuggingFace/Replicate)**: no local files — set the relevant API key

For private or gated Hugging Face models, set `HF_TOKEN` before running the
download step and make sure the account has accepted the model license. A 401
from Hugging Face usually means the token is missing, expired, or not allowed
for that repository.

### Manual config (optional)

If you prefer to skip the wizard, set the runtime environment variables directly:

```bash
export VETINARI_MODELS_DIR=./models
export VETINARI_NATIVE_MODELS_DIR=./models/native
export VETINARI_WEB_PORT=5000
export VETINARI_ADMIN_TOKEN=<choose-a-local-admin-token>
```

Key variables to set:

```
VETINARI_MODELS_DIR=./models      # path to llama-cpp GGUF model files (one backend among many)
VETINARI_NATIVE_MODELS_DIR=./models/native
VETINARI_GPU_LAYERS=-1            # GPU layers to offload (-1 = auto-detect)
VETINARI_CONTEXT_LENGTH=8192      # context window size
VETINARI_VLLM_ENDPOINT=http://localhost:8000
VETINARI_VLLM_SETUP_MODE=guided   # manual, guided, or auto
VETINARI_VLLM_MODEL=              # Hugging Face ID or container-visible model path
VETINARI_NIM_ENDPOINT=http://localhost:8001
VETINARI_NIM_SETUP_MODE=guided    # manual, guided, or auto
VETINARI_NIM_IMAGE=               # NGC NIM image, required for guided/auto container setup
VETINARI_ADMIN_TOKEN=             # required for project and workbench mutation APIs
HF_TOKEN=                         # required for private or gated Hugging Face models
```

On Windows + WSL, see the WSL setup section in [`README.md`](../../README.md) for the exact `vllm` install and endpoint-export commands.

---

## 3. Verify the installation

```bash
python -c "import vetinari; print('OK')"
python -m pytest tests/ -x -q
python -m ruff check vetinari/
python scripts/check_vetinari_rules.py
```

All four checks should pass with zero errors before you proceed.

---

## 4. Start the system

Start AM Workbench with the local CLI and API server:

```bash
python -m vetinari start
```

The API server listens on `http://localhost:5000` by default. It serves the REST API and Workbench API routes; a browser dashboard is not mounted by this command in the current build.

If project creation or workbench actions return `401`, restart the server with
`VETINARI_ADMIN_TOKEN` set and send that token in the admin authorization
header. This is the expected fail-closed behavior for mutation routes.

To start the API server on a different port without an active goal:

```bash
python -m vetinari serve --port 5000
```

---

## 5. Run your first goal

Pass a goal directly on the command line:

```bash
python -m vetinari start --goal "Summarise the key points of the README and write them to summary.md"
```

AM Workbench decomposes the goal through its three-agent factory pipeline:

1. **Foreman** — breaks the goal into a structured plan
2. **Worker** — executes each task in the plan using the most appropriate local model
3. **Inspector** — reviews outputs for quality and completeness

The Worker handles four broad mode groups (research, architecture, build, operations); the actual mode is selected automatically based on the task type and the models available. See [`docs/architecture/pipeline.md`](../architecture/pipeline.md) for details.

If the first run fails with an out-of-memory message, retry with a smaller
quantization (a lower GGUF quant for llama-cpp; GPTQ/AWQ INT4 for vLLM/SGLang;
a smaller variant for any other backend), lower `VETINARI_CONTEXT_LENGTH`,
reduce GPU layers, or switch to a configured backend with enough memory (e.g.
a cloud provider via LiteLLM, or llama-cpp on CPU for smaller models). These
same recovery actions are surfaced in API error details for project output
and review screens.

---

## 6. Check system status

```bash
.venv312/Scripts/python.exe -m vetinari status      # Summary of agents, models, and active work
.venv312/Scripts/python.exe -m vetinari health      # Pass/fail health check (useful for CI and scripts)
.venv312/Scripts/python.exe -m vetinari doctor      # Full diagnostic report, including backend checks
```

Use `doctor` when troubleshooting. It checks model loading, memory, config validity, and more.

---

## 7. Manage models

```bash
.venv312/Scripts/python.exe -m vetinari models list                        # Show loaded models and their status across all configured backends
.venv312/Scripts/python.exe -m vetinari models scan                        # Discover llama-cpp GGUF files under VETINARI_MODELS_DIR
.venv312/Scripts/python.exe -m vetinari models download --repo <hf-repo-id> --filename <file.gguf>  # Download a GGUF file for llama-cpp
```

For other backends, point them at a HuggingFace repo ID instead of downloading a file:

```bash
# vLLM / SGLang / NIM use the HF repo directly:
export VETINARI_VLLM_MODEL=meta-llama/Meta-Llama-3.1-8B-Instruct

# faster-whisper resolves CT2 models by repo ID:
export VETINARI_WHISPER_MODEL=Systran/faster-whisper-large-v3

# Hosted providers need only an API key, no model files at all.
```

---

## 8. Recover from common first-run errors

Run `doctor` first, but do not stop there. Match the failing branch to a
specific recovery action:

| Symptom | Recovery |
|---|---|
| `Model not found` or no local inference | Identify which backend was requested. For llama-cpp: run `models scan` and confirm `VETINARI_MODELS_DIR` contains `.gguf` files. For vLLM/SGLang/NIM: check that `VETINARI_VLLM_ENDPOINT` (or `VETINARI_NIM_ENDPOINT`) is reachable and the requested HF repo is downloadable. For hosted providers: confirm the API key environment variable is set. |
| Hugging Face download returns `401` | Set `HF_TOKEN`, confirm the token has access, and accept the model license on the Hugging Face account. |
| Port `5000` already in use | Restart with `python -m vetinari serve --port 5001` or stop the process holding the port. |
| Startup import error | Reinstall with the optional group used by your workflow, then rerun `python -c "import vetinari; print('OK')"`. |
| Workbench action returns `401` | Set `VETINARI_ADMIN_TOKEN` before server startup and send the matching admin proof from the client. |
| Out of memory on the first goal | Use a smaller quantization for whichever backend you are on (lower GGUF quant for llama-cpp; GPTQ/AWQ INT4 for vLLM/SGLang; smaller variant for any other backend), lower `VETINARI_CONTEXT_LENGTH`, reduce GPU layers, or switch to a configured backend with enough memory (cloud provider via LiteLLM is always a valid fallback). |

Preserve the failing command, exit code, and diagnostic outputs before
changing persistent state:

```bash
.venv312/Scripts/python.exe -m vetinari doctor --json > doctor.json
.venv312/Scripts/python.exe -m vetinari status > status.txt
.venv312/Scripts/python.exe scripts/check_workbench_health.py > workbench-health.txt
```

Use [Troubleshooting](../troubleshooting.md) for symptom-specific recovery and
[FAQ, Known Limitations, and Support](faq-known-limitations.md) before filing
an issue.

---

## Key entry points

| What | Command / URL |
|---|---|
| CLI help | `.venv312/Scripts/python.exe -m vetinari --help` |
| Full start with local API server | `.venv312/Scripts/python.exe -m vetinari start` |
| Goal-based execution | `.venv312/Scripts/python.exe -m vetinari start --goal "..."` |
| API server only | `.venv312/Scripts/python.exe -m vetinari serve --host 127.0.0.1 --port 5000` |
| System status | `.venv312/Scripts/python.exe -m vetinari status` |
| Health check | `.venv312/Scripts/python.exe -m vetinari health` |
| Diagnostics | `.venv312/Scripts/python.exe -m vetinari doctor` |
| First-run wizard | `.venv312/Scripts/python.exe -m vetinari init` |
| MCP tool surface | `.venv312/Scripts/python.exe -m vetinari mcp` |
| REST API | `http://localhost:5000/api` |

---

## Next steps

- [`docs/getting-started/onboarding.md`](onboarding.md) — full onboarding walkthrough
- [`docs/architecture/pipeline.md`](../architecture/pipeline.md) — pipeline internals and agent conventions
- [`docs/reference/production.md`](../reference/production.md) — production deployment checklist
- [`docs/getting-started/faq-known-limitations.md`](faq-known-limitations.md) — support URL, FAQ, and known limitations
- [`docs/runbooks/upgrade-migration-rollback.md`](../runbooks/upgrade-migration-rollback.md) — safe upgrade and rollback path
- [`docs/reference/mcp-server.md`](../reference/mcp-server.md) — MCP server setup and troubleshooting

## Frontend navigation note

The frontend does not use a SvelteKit `src/routes/` tree. Add user-facing
pages through the existing `ui/svelte/src/views/` patterns and navigation
drawer registration, then verify the route in the running app.

## Documentation Freshness Guard

`tests/test_frontend_package_boundary.py` rejects the old placeholder clone URL
and stale Python 3.10 prerequisite claim so the quick-start path stays aligned
with the public AM Workbench repository and `pyproject.toml`.
