# Model Configuration and Routing

This guide covers how AM Workbench discovers, loads, and selects models. It explains the tier system, cascade routing, Thompson Sampling, inference profiles, and how to add new models.

Model names, provider examples, context windows, and prices in this guide are operational examples, not compliance evidence. Do not make license, commercial-suitability, attribution, or NOTICE claims from this page unless the exact model ID, revision, model-card/license URL, checked date, and attribution duty are recorded with the deployment configuration.

---

## Overview

AM Workbench uses a local-first, multi-tier model system. The two primary config files are:

- `config/models.yaml` — model definitions, hardware profile, routing policy, and backend settings
- `config/task_inference_profiles.json` — per-task-type sampling parameters (temperature, max tokens, etc.)

The routing pipeline runs in this order:

```
Task arrives
    ↓
DynamicModelRouter: select the cheapest adequate tier (Thompson Sampling)
    ↓
CascadeRouter: run inference → if confidence < threshold → escalate to next tier
    ↓
Return best response seen across the chain
```

---

## Model Tiers

Models are grouped into five tiers by capability and cost. Routing starts at the cheapest tier that satisfies the task requirements and escalates upward only when confidence is low.

| Tier | Name | Size | Preferred Tasks | Cost | Latency |
|------|------|------|-----------------|------|---------|
| 0 | tiny | 1–3B | classification, extraction, triage, routing | $0 | fast |
| 1 | small | 7–8B | coding, quick_review, documentation | $0 | fast |
| 2 | medium | 14–32B | reasoning, planning, analysis, general | $0 | medium |
| 3 | large | 70B+ | complex_reasoning, research, security_audit | $0 | slow |
| 4 | cloud | varies | vision, long_context, creative, overflow | >$0 | varies |

Tier 3 models exceed 32 GB VRAM and require CPU offload (`requires_cpu_offload: true`). Tier 4 cloud models are added automatically when the relevant API key is present.

---

## config/models.yaml Structure

### Hardware Profile

The `hardware` section describes the local machine. `VRAMManager` and routing decisions read these values to determine which models fit in memory.

```yaml
hardware:
  gpu_vram_gb: 32
  system_ram_gb: 64
  cpu_offload_enabled: true
  max_cpu_offload_gb: 30        # RAM available for CPU-offloaded layers
  throughput_estimates:
    tiny_1b_3b: 120             # tokens/sec — Q4 on RTX 5090
    small_7b_8b: 70
    medium_14b_15b: 45
    large_30b_32b: 25
    xlarge_70b_72b: 10          # partial CPU offload required
```

### Local Inference Settings

```yaml
local_inference:
  models_dir: "./models"        # override with VETINARI_MODELS_DIR
  gpu_layers: -1                # -1 = all layers to GPU; override with VETINARI_GPU_LAYERS
  context_length: 8192          # default context window; override with VETINARI_CONTEXT_LENGTH
  ram_budget_gb: 30             # CPU RAM for offloaded layers
  cpu_offload_enabled: true     # allow GPU + CPU split-loading for oversized models
```

### Speculative Decoding

Speculative decoding reduces latency for structured output tasks (Inspector validation, Worker code generation) by predicting multiple tokens per step.

```yaml
speculative_decoding:
  strategy: "auto"              # "draft_model", "prompt_lookup", "auto", "disabled"
  num_pred_tokens: 10           # tokens predicted per step (prompt lookup)
  draft_model_id: null          # auto-detected if null
  prioritize_for:
    - "inspector"               # structured JSON — high acceptance rate
    - "worker"                  # code generation has repetitive patterns
```

### Model Routing Mode

```yaml
model_routing:
  model_router: "internal"      # "internal" = AM Workbench manages VRAM directly
                                # "llama-swap" = delegate to external llama-swap server
  router_url: ""                # e.g. "http://localhost:8081" (llama-swap only)
```

### Inference Backends

```yaml
inference_backend:
  selection_policy: "hardware_aware"
  fallback_order: ["nim", "vllm", "llama_cpp"]
```

Supported backends and their characteristics:

| Backend | Formats | CPU Offload | GPU Required | Use Case |
|---------|---------|-------------|--------------|----------|
| `llama_cpp` | GGUF | Yes | No | Explicit preference, GGUF-only models, weak/no server setup, CPU/RAM+VRAM offload, oversized local models, recovery fallback |
| `vllm` | safetensors, AWQ, GPTQ | No | Yes | High-throughput GPU server |
| `sglang` | safetensors, AWQ, GPTQ | No | Yes | Structured-generation GPU server, fast constrained decoding |
| `nim` | safetensors, GGUF, AWQ, GPTQ | No | Yes | Preferred native backend on NVIDIA/CUDA hardware when reachable |
| `comfyui` | safetensors, ckpt | No | Yes (typically) | Image diffusion and ComfyUI workflow graphs |
| `faster_whisper` | CTranslate2 model directories | Yes | Optional | Local Whisper transcription |
| `litellm` | Cloud APIs (no local files) | N/A | N/A | OpenAI, Anthropic, Cohere, Gemini, HuggingFace, Replicate fallback |

GGUF is the file format for `llama_cpp` and some NIM deployments; it is not the format for vLLM, SGLang, ComfyUI, faster-whisper, or any hosted provider. Choose the backend by capability and hardware fit; do not assume GGUF is the universal model format. vLLM and NIM are GPU-server backends. For models larger than VRAM or workflows that require CPU/RAM+VRAM offload, use `llama_cpp` with `cpu_offload_enabled: true`.

### Model Definition Fields

Each entry under `models:` follows this structure:

```yaml
- model_id: "qwen2.5-coder-7b"         # matches GGUF filename stem
  provider: "local"
  display_name: "Qwen 2.5 Coder 7B"
  capabilities:
    - coding
    - fast
  context_window: 32768
  latency_hint: "fast"                  # "fast", "medium", "slow"
  privacy_level: "local"               # "local" or "public"
  memory_requirements_gb: 6
  quantization: "q4_k_m"
  status: "available"                  # "available" or "example"
  preferred_for:
    - code_generation
    - quick_review
    - classification
```

Set `status: "example"` for models not yet downloaded. Change to `"available"` once the asset is present on disk or the remote endpoint is reachable.

The `model_id` resolution depends on the `provider`:

| Provider | What `model_id` must match |
|---|---|
| `local` (llama-cpp) | Filename stem in `models_dir`. For `qwen2.5-coder-7b.Q4_K_M.gguf`, use `qwen2.5-coder-7b`. |
| `vllm`, `sglang`, `nim` | HuggingFace repo ID (e.g. `meta-llama/Meta-Llama-3.1-8B-Instruct`) that the backend can resolve at load time. |
| `faster_whisper` | HuggingFace repo ID for the CTranslate2 model (e.g. `Systran/faster-whisper-large-v3`). |
| `comfyui` | Filename or workflow node identifier the ComfyUI graph references. |
| `litellm` and other hosted providers (`openai`, `anthropic`, `cohere`, `gemini`, `huggingface`, `replicate`) | Hosted-API model identifier the provider accepts (e.g. `gpt-4o`, `claude-opus-4-8`, `command-r-plus`). |

### Task Defaults

Maps task types to default model IDs. Used when no explicit model is selected. The fallback chain is:

```
task default → agent default → global default → any available model
```

```yaml
task_defaults:
  coding: "qwen2.5-coder-7b"
  research: "qwen2.5-72b"
  reasoning: "qwen2.5-72b"
  planning: "qwen2.5-72b"
  review: "qwen2.5-coder-7b"
  security: "qwen2.5-72b"
  documentation: "qwen2.5-coder-7b"
  creative: "qwen2.5-72b"
  classification: "llama-3.2-1b"
  general: "qwen2.5-72b"
```

### Routing Policy

```yaml
policy:
  local_first: true
  privacy_weight: 1.0           # higher = more important in scoring
  latency_weight: 0.5
  cost_weight: 0.3
  max_cost_per_1k_tokens: null  # set a positive value to cap cloud spend
  preferred_providers:
    - local
    - claude
    - gemini
    - huggingface
    - replicate
  allow_cloud_fallback: true
  cloud_fallback_trigger: "local_unavailable"
  cloud_egress_mode: "local_first_with_fallback"
```

`cloud_egress_mode` is the product-level exit policy for cloud use:

| Mode | Behavior |
|------|----------|
| `local_only` | Cloud routing is disabled; use this for offline/private deployments. |
| `local_first_with_fallback` | Local models stay preferred; cloud is limited to the configured fallback trigger. |
| `explicit_cloud` | Cloud is available only when the caller deliberately selects a cloud route. |

`cloud_fallback_trigger` is schema-constrained to `disabled`, `local_unavailable`,
`explicit_request`, or `policy_approved`; arbitrary provider strings are invalid.

---

## Cascade Routing

`CascadeRouter` (`vetinari/cascade_router.py`) implements the confidence-based escalation strategy.

### How It Works

1. A task arrives — routing starts with the cheapest adequate model in the chain.
2. Inference runs and a confidence score is computed for the response.
3. If `confidence >= CASCADE_CONFIDENCE_THRESHOLD` → return the response (done, cheap path).
4. If `confidence < CASCADE_CONFIDENCE_THRESHOLD` → escalate to the next tier.
5. Repeat up to `CASCADE_MAX_ESCALATIONS` times.
6. The caller always receives a response — the best one seen across the chain.

### Wiring CascadeRouter Manually

```python
from vetinari.cascade_router import CascadeRouter, get_cascade_router

cr = get_cascade_router()
cr.add_tier("llama-3.2-1b", cost_per_1k=0.0, priority=0)
cr.add_tier("qwen2.5-coder-7b", cost_per_1k=0.0, priority=1)
cr.add_tier("qwen2.5-72b", cost_per_1k=0.0, priority=2)

response, used_model = cr.route(request, adapter_fn=my_adapter)
```

### Environment Variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `CASCADE_CONFIDENCE_THRESHOLD` | `0.7` | Minimum confidence score to accept a response |
| `CASCADE_MAX_ESCALATIONS` | `2` | Maximum number of escalation steps per request |
| `CASCADE_ENABLED` | `1` | Set to `0` to disable cascade routing entirely |

---

## Thompson Sampling

`DynamicModelRouter` (`vetinari/models/dynamic_model_router.py`) uses Bayesian bandits to learn which model works best per task type over time. This balances exploration (trying less-proven models) against exploitation (using models with the best track record).

### How It Works

Each `(model_id, task_type)` pair maintains a Beta distribution with parameters `alpha` (successes) and `beta` (failures):

1. On task start: sample from each model's Beta distribution for the task type.
2. The model with the highest sample is selected.
3. On task complete: if the result is good, increment `alpha`; if poor, increment `beta`.
4. Over time, consistently good models rise and consistently poor ones are deprioritized — without being permanently excluded.

Models start with informed priors seeded by `BenchmarkSeeder` rather than a flat Beta(1, 1), so the sampler starts with reasonable capability-aware estimates.

Thompson Sampling tracks the **actual model used** (from the inference response), not the originally requested model. This ensures feedback loops close correctly.

---

## Task Inference Profiles

`config/task_inference_profiles.json` defines sampling parameters per task type. All inference parameters flow through this system — they are never hardcoded at call sites.

### Core Profiles

| Profile | Temperature | Top-p | Max Tokens | JSON Output | Notes |
|---------|-------------|-------|-----------|-------------|-------|
| `coding` | 0.10 | 0.92 | 16384 | No | Deterministic code; large output for multi-function implementations |
| `code_review` | 0.20 | 0.88 | 8192 | Yes | Structured findings with evidence and fixes |
| `planning` | 0.35 | 0.90 | 8192 | Yes | DAG decomposition, 5–15 tasks |
| `security_audit` | 0.05 | 0.82 | 8192 | Yes | Very precise; severity and remediation required |
| `analysis` | 0.25 | 0.88 | 8192 | Yes | Structured, precise, detailed reasoning |
| `research` | 0.40 | 0.92 | 8192 | No | Broader exploration, synthesis |
| `documentation` | 0.40 | 0.92 | 8192 | No | Natural language, comprehensive coverage |
| `debugging` | 0.15 | 0.85 | 8192 | Yes | Precise diagnosis with root cause |
| `refactoring` | 0.10 | 0.88 | 16384 | No | Preserve semantics; large output |
| `testing` | 0.15 | 0.90 | 8192 | No | Deterministic, multi-case test suites |
| `summarization` | 0.30 | 0.90 | 4096 | No | Balanced, preserve key information |
| `classification` | 0.00 | 0.80 | 512 | Yes | Deterministic label output |
| `extraction` | 0.00 | 0.80 | 2048 | Yes | Deterministic structured output |
| `brainstorming` | 0.90 | 0.95 | 8192 | No | Maximum creative diversity |
| `creative_writing` | 0.85 | 0.95 | 8192 | No | High diversity |
| `conversation` | 0.65 | 0.92 | 4096 | No | Natural variation |
| `goal_check` | 0.05 | 0.82 | 2048 | Yes | Binary pass/fail with evidence |
| `evaluation` | 0.10 | 0.85 | 4096 | Yes | Structured scoring with reasoning |
| `verification` | 0.10 | 0.85 | 4096 | Yes | Precise pass/fail with evidence |
| `general` | 0.35 | 0.90 | 8192 | No | Versatile fallback |

Many profiles have alias names that map to the same parameters (e.g., `code_gen` → `coding`, `security` → `security_audit`, `planner` → `planning`). Use whichever name matches your agent mode.

### Model-Size Adjustments

After the base profile parameters are applied, size-based offsets adjust temperature based on the model being used:

| Size Band | Param Range | Temperature Offset | Notes |
|-----------|-------------|-------------------|-------|
| `small` | ≤ 10B | -0.10 | Tighter output from smaller models |
| `medium` | 10B–40B | -0.05 | Mild adjustment |
| `large` | 40B–80B | 0.00 | No adjustment |
| `xlarge` | > 80B | +0.05 | Slightly more creative at scale |

### Per-Model Overrides

Specific models can apply additional offsets on top of the size adjustment:

```json
"model_overrides": {
    "qwen2.5-coder-7b": { "temperature_offset": -0.1, "note": "Strong coder, keep tight" },
    "deepseek-r1-distill-qwen-32b": { "top_p_offset": -0.05, "note": "Reasoning model, lower diversity" }
}
```

### System Prompt Token Budgets

The profile file also defines maximum system prompt sizes per agent mode. These ensure prompts fit within the context windows of smaller models:

```json
"per_mode_token_budgets": {
    "worker": { "build": 3000, "architecture": 3000, "_default": 2000 },
    "inspector": { "security_audit": 3000, "code_review": 2500 },
    "7b_tier_cap": 1500,
    "70b_tier_cap": 4000
}
```

---

## Cloud Models

Cloud models are defined under `cloud_models:` in `config/models.yaml` and are added automatically to the router when the required environment variable is set.

### Available Cloud Models

| Model | Provider | Context | Cost/1K | Env Var Required |
|-------|----------|---------|---------|-----------------|
| claude-sonnet-4 | Anthropic | 200K | $0.015 | `ANTHROPIC_API_KEY` |
| claude-haiku-4 | Anthropic | 200K | $0.0008 | `ANTHROPIC_API_KEY` |
| gemini-2.5-flash | Google | 1M | $0.00 | `GEMINI_API_KEY` |
| gemini-2.5-pro | Google | 2M | $0.00125 | `GEMINI_API_KEY` |
| gpt-4o | OpenAI | 128K | $0.005 | `OPENAI_API_KEY` |
| gpt-4o-mini | OpenAI | 128K | $0.0006 | `OPENAI_API_KEY` |
| Llama 3.1 70B (HF) | HuggingFace | 32K | $0.00 | `HF_HUB_TOKEN` |
| Mistral 7B (HF) | HuggingFace | 32K | $0.00 | `HF_HUB_TOKEN` |
| Llama 3.1 70B (Replicate) | Replicate | 32K | $0.001 | `REPLICATE_API_TOKEN` |

Anthropic also accepts `CLAUDE_API_KEY` as a fallback if `ANTHROPIC_API_KEY` is not set.

Cloud models are Tier 4 and are only used when the routing policy permits cloud
egress. The default runtime policy is `cloud_egress_mode:
"local_first_with_fallback"` with `allow_cloud_fallback: true` and
`cloud_fallback_trigger: "local_unavailable"`. Offline/private deployments set
`cloud_egress_mode: "local_only"`, `allow_cloud_fallback: false`, and
`cloud_fallback_trigger: "disabled"`.

---

## Model Discovery

On startup, `ModelPool` discovers available models using this fallback chain:

1. Check the llama-swap HTTP API (if `model_router: "llama-swap"` and `router_url` is configured).
2. Scan for local GGUF files via `LlamaCppProviderAdapter`.
3. Retry with exponential backoff — max 2 retries, 10-second wall-clock cap.
4. If local discovery fails, load last-known-good models from cache, then fall back to the static config entries.

Cloud models are added automatically for each provider whose API key is present in the environment.

---

## Model Scout

When Thompson Sampling detects that all available models for a task type have a mean success rate below 0.5, `ModelScout` (`vetinari/models/model_scout.py`) searches for better alternatives.

### Discovery Sources

ModelScout queries `ModelDiscovery` adapters across:

- HuggingFace
- Reddit
- GitHub
- PapersWithCode

Results are ranked by estimated quality score and cached in memory for the lifetime of the scout instance.

### Underperformance Threshold

```python
ModelScout.UNDERPERFORMANCE_THRESHOLD = 0.5  # Beta mean below this triggers scouting
ModelScout.MAX_RECOMMENDATIONS = 5  # maximum candidates returned
```

Only models estimated to outperform the current pool by a meaningful margin are surfaced. CLI hardware-based recommendations are available via `vetinari models recommend`; freshness checks are available via `vetinari models check`. The mounted model-scout recommendation route is `/api/v1/models/recommendations`.

---

## Adding a New Model

### Local GGUF Model

1. Place the GGUF file in your models directory (default: `./models`).
2. Run `vetinari models scan` to discover it automatically.
3. Add a matching entry to `config/models.yaml` under `models:`.
4. Set `capabilities`, `preferred_for`, and the appropriate `tier` placement.
5. Set `status: "available"`.
6. Run `vetinari health` to confirm the model loads correctly.

Example entry for a newly added model:

```yaml
- model_id: "mistral-nemo-12b"
  provider: "local"
  display_name: "Mistral Nemo 12B"
  capabilities:
    - coding
    - reasoning
  context_window: 128000
  latency_hint: "medium"
  privacy_level: "local"
  memory_requirements_gb: 8
  quantization: "q4_k_m"
  status: "available"
  preferred_for:
    - coding
    - quick_review
```

### Download from HuggingFace

```bash
vetinari models download \
  --repo TheBloke/Qwen2.5-Coder-7B-GGUF \
  --filename qwen2.5-coder-7b.Q4_K_M.gguf
```

The download command places the file in `models_dir` and prompts you to add a config entry.

---

## Environment Variables Reference

| Variable | Default | Purpose |
|----------|---------|---------|
| `VETINARI_MODELS_DIR` | `./models` | Directory scanned for GGUF files |
| `VETINARI_GPU_LAYERS` | `-1` | GPU layers to offload (-1 = all) |
| `VETINARI_CONTEXT_LENGTH` | `8192` | Default context window for unknown models |
| `CASCADE_CONFIDENCE_THRESHOLD` | `0.7` | Minimum response confidence to accept without escalating |
| `CASCADE_MAX_ESCALATIONS` | `2` | Maximum escalation steps per request |
| `CASCADE_ENABLED` | `1` | Set to `0` to disable cascade routing |
| `ANTHROPIC_API_KEY` | — | Anthropic/Claude cloud auth (also `CLAUDE_API_KEY`) |
| `GEMINI_API_KEY` | — | Google Gemini cloud auth |
| `OPENAI_API_KEY` | — | OpenAI cloud auth |
| `HF_HUB_TOKEN` | — | HuggingFace Inference API auth |
| `REPLICATE_API_TOKEN` | — | Replicate cloud auth |

---

## Key Files

| File | Purpose |
|------|---------|
| `config/models.yaml` | Model definitions, hardware profile, tiers, backends, routing policy |
| `config/task_inference_profiles.json` | Per-task sampling parameters and system prompt token budgets |
| `vetinari/models/dynamic_model_router.py` | `DynamicModelRouter` — Thompson Sampling model selection |
| `vetinari/cascade_router.py` | `CascadeRouter` — confidence-based tier escalation |
| `vetinari/models/model_pool.py` | Model discovery, VRAM tracking, startup loading |
| `vetinari/models/model_scout.py` | Underperformance detection and recommendation search |
| `vetinari/adapters/registry.py` | Provider adapter catalog — maps providers to adapter implementations |
| `vetinari/config/inference_config.py` | `InferenceConfigManager` — reads and applies task inference profiles |

## RCG-0065-P01 Security Doc Evidence

- Pack: RCG-0065-P01.
- Scope review: no direct source row maps to this file in slice 01/02; retained as an affected surface for explicit no-change evidence.
- Validation command: `.venv312/Scripts/python.exe -m pytest tests/operator/test_security_doc_standards.py -m operator -n 0`.
