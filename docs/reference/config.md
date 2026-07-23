# AM Workbench Configuration Reference

Complete guide to configuring AM Workbench including Plan Mode, Ponder, and memory settings.

---

## Environment Variables

### Plan Mode Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| PLAN_MODE_ENABLE | true | Enable plan-first orchestration |
| PLAN_MODE_DEFAULT | true | Default mode for all tasks |
| DRY_RUN_ENABLED | false | Generate plans without execution |
| DRY_RUN_RISK_THRESHOLD | 0.25 | Auto-approval risk threshold (0.0-1.0) |
| PLAN_DEPTH_CAP | 16 | Maximum subtask decomposition depth |
| PLAN_MAX_CANDIDATES | 3 | Maximum plan variants to generate |
| VETINARI_ADMIN_TOKEN | (none) | Litestar admin guard token for guarded plan/control endpoints |
| PLAN_ADMIN_TOKEN | (none) | Legacy plan-memory/status flag; not accepted by Litestar `admin_guard` |
| PLAN_MEMORY_DB_PATH | ./vetinari_memory.db | SQLite database path |
| PLAN_USE_JSON_FALLBACK | false | Use JSON if SQLite unavailable |
| PLAN_RETENTION_DAYS | 90 | Days to keep plan history |

### Ponder Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| ENABLE_PONDER_MODEL_DISCOVERY | true | Enable Ponder model-discovery scoring in the model-selection flow; this is not a `vetinari.web.config.VetinariConfig` field |
| PONDER_CLOUD_WEIGHT | 0.20 | Cloud signal weight used by Ponder/model-discovery scoring; this is not a `vetinari.web.config.VetinariConfig` field |

### Cloud Provider API Keys

| Variable | Provider | Description |
|----------|----------|-------------|
| ANTHROPIC_API_KEY or CLAUDE_API_KEY | Anthropic Claude | API key for Claude models |
| OPENAI_API_KEY | OpenAI | API key for OpenAI models |
| GEMINI_API_KEY | Google Gemini | API key for Gemini models |
| HF_HUB_TOKEN | HuggingFace | Token for HF Inference API |
| REPLICATE_API_TOKEN | Replicate | Token for Replicate API |

### Local Inference Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| VETINARI_MODELS_DIR | ./models | Directory containing llama-cpp GGUF model files (other backends ignore this) |
| VETINARI_NATIVE_MODELS_DIR | ./models/native | Directory containing native HuggingFace-format checkpoints (safetensors, optional GPTQ/AWQ) for vLLM/SGLang/NIM workflows; faster-whisper uses CTranslate2 models, ComfyUI uses safetensors/ckpt |
| VETINARI_GPU_LAYERS | -1 | Number of layers to offload to GPU (-1 = auto-detect) |
| VETINARI_CONTEXT_LENGTH | 8192 | Context window size for loaded models |

### Native Backend Endpoints

| Variable | Default | Description |
|----------|---------|-------------|
| VETINARI_VLLM_ENDPOINT | http://localhost:8000 | OpenAI-compatible vLLM server endpoint; preferred primary backend when reachable |
| VETINARI_NIM_ENDPOINT | http://localhost:8001 | OpenAI-compatible NVIDIA NIM endpoint; preferred native backend on NVIDIA/CUDA hardware when reachable |
| VETINARI_PREFERRED_BACKEND | unset | Explicit backend preference (`nim`, `vllm`, or `llama_cpp`) used by setup-generated config |

### vLLM Container Setup

| Variable | Default | Description |
|----------|---------|-------------|
| VETINARI_VLLM_SETUP_MODE | guided | `manual`, `guided`, or `auto`; auto starts a vLLM container only when all required inputs are present |
| VETINARI_VLLM_IMAGE | vllm/vllm-openai:latest on CUDA, vllm/vllm-openai-rocm:latest on ROCm | vLLM OpenAI-compatible server image |
| VETINARI_VLLM_MODEL | unset | Hugging Face model ID or container-visible model path passed to `--model` |
| VETINARI_VLLM_CONTAINER_NAME | vetinari-vllm | Local Docker container name for setup-generated commands |
| VETINARI_VLLM_HOST_PORT | endpoint port | Host port mapped to the vLLM API port |
| VETINARI_VLLM_CONTAINER_PORT | 8000 | Port exposed by the vLLM container |
| VETINARI_VLLM_CACHE_DIR | unset | Optional host directory mounted to `/root/.cache/huggingface` |
| VETINARI_VLLM_EXTRA_ARGS | unset | Additional vLLM engine/server args appended after `--model` |
| HF_TOKEN | unset | Optional Hugging Face token passed by name, never by value, into the container |
| VETINARI_VLLM_PREFIX_CACHING_ENABLED | true | Enables or disables vLLM prefix caching in setup-generated container args |
| VETINARI_VLLM_PREFIX_CACHING_HASH_ALGO | sha256 | Hash algorithm used for vLLM prefix caching |

### NIM Container Setup

| Variable | Default | Description |
|----------|---------|-------------|
| VETINARI_NIM_SETUP_MODE | guided | `manual`, `guided`, or `auto`; auto starts a NIM container only when all required inputs are present |
| VETINARI_NIM_IMAGE | unset | NGC NIM container image to run when setup mode is guided or auto |
| VETINARI_NIM_CONTAINER_NAME | vetinari-nim | Local Docker container name for setup-generated commands |
| VETINARI_NIM_HOST_PORT | endpoint port | Host port mapped to the NIM container API port |
| VETINARI_NIM_CONTAINER_PORT | 8000 | Port exposed by the NIM container |
| VETINARI_NIM_CACHE_DIR | unset | Optional host directory mounted to `/opt/nim/.cache` for NIM model/cache reuse |
| NGC_API_KEY | unset | NGC API key passed to NIM containers for model/resource access |
| NIM_ENABLE_KV_CACHE_REUSE | 1 for setup-planned NIM containers | Enables NIM KV cache reuse/prefix caching when the container is started through the setup plan |
| NIM_ENABLE_KV_CACHE_HOST_OFFLOAD | unset | Lets NIM decide by default; set to `1` or `0` to force host-memory KV cache offload behavior |

### Web Server Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| VETINARI_WEB_PORT | 5000 | Web dashboard port |
| VETINARI_WEB_HOST | 127.0.0.1 | Web server bind address |
| VETINARI_DEBUG | false | Enable web/runtime debug mode when set to `1`, `true`, or `yes` |
| VETINARI_LOG_LEVEL | INFO | Python log level (DEBUG, INFO, WARNING, ERROR) |

### Vetinari Web Runtime Configuration

These variables are read by `vetinari.web.config.VetinariConfig.from_env()`.
Malformed integer values fall back to the documented defaults and emit a warning.

| Variable | Default | Description |
|----------|---------|-------------|
| VETINARI_API_TOKEN | unset | Optional API token stored on the web config object for local integrations |
| VETINARI_CONFIG | manifest/vetinari.yaml | Manifest path used by task/config route helpers |
| VETINARI_PROJECT_DIR | projects | Project directory relative to the active workspace |
| VETINARI_OUTPUT_DIR | outputs | Output directory relative to the active workspace |
| VETINARI_MEMORY_GB | 48 | Memory budget in GiB for local runtime planning |
| VETINARI_MAX_CONCURRENT | 4 | Maximum concurrent task budget for local runtime planning |
| VETINARI_TIMEOUT | 120 | Default task timeout in seconds |
| VETINARI_LLM_TIMEOUT | 300 | Default LLM inference timeout in seconds |
| ENABLE_EXTERNAL_DISCOVERY | true | Enable external model/tool discovery features |

### Cascade Routing

| Variable | Default | Description |
|----------|---------|-------------|
| CASCADE_CONFIDENCE_THRESHOLD | 0.7 | Minimum confidence to accept response (0.0-1.0) |
| CASCADE_MAX_ESCALATIONS | 2 | Maximum tier escalation steps |
| CASCADE_ENABLED | 1 | Enable cascade routing (0 to disable) |

### See Also

- **Model configuration**: See `docs/reference/models.md` for `config/models.yaml` and `config/task_inference_profiles.json`
- **Training configuration**: See `docs/reference/training.md` for `config/ml_config.yaml` and `config/quality_thresholds.yaml`
- **CLI commands**: See `docs/reference/cli.md` for all command-line options

---

## Configuration Examples

### Minimal Configuration (.env)

```bash
# Plan Mode (defaults enabled)
PLAN_MODE_ENABLE=true
PLAN_MODE_DEFAULT=true

# Admin token for guarded Litestar plan/control endpoints
VETINARI_ADMIN_TOKEN=my-secret-token

# Cloud providers (optional)
CLAUDE_API_KEY=sk-ant-...
GEMINI_API_KEY=AIza...

# Native backends (setup orders these by hardware and reachability)
VETINARI_VLLM_ENDPOINT=http://localhost:8000
VETINARI_NIM_ENDPOINT=http://localhost:8001

# llama-cpp GGUF (one backend among many)
VETINARI_MODELS_DIR=./models
# Native HuggingFace-format checkpoints for vLLM/SGLang/NIM
VETINARI_NATIVE_MODELS_DIR=./models/native
```

### Development Configuration

```bash
# Plan Mode
PLAN_MODE_ENABLE=true
PLAN_MODE_DEFAULT=true
DRY_RUN_ENABLED=true
DRY_RUN_RISK_THRESHOLD=0.5

# Memory store (JSON for quick prototyping)
PLAN_USE_JSON_FALLBACK=true
PLAN_MEMORY_DB_PATH=./dev_memory.json

# Admin
VETINARI_ADMIN_TOKEN=dev-token

# Logging
DEBUG=true
LOG_LEVEL=DEBUG
```

### Production Configuration

```bash
# Plan Mode
PLAN_MODE_ENABLE=true
PLAN_MODE_DEFAULT=true
DRY_RUN_ENABLED=false
DRY_RUN_RISK_THRESHOLD=0.25
PLAN_DEPTH_CAP=16
PLAN_MAX_CANDIDATES=3
PLAN_RETENTION_DAYS=180

# Memory store (SQLite)
PLAN_MEMORY_DB_PATH=/var/lib/vetinari/vetinari_memory.db
PLAN_USE_JSON_FALLBACK=false

# Admin (use secure token)
VETINARI_ADMIN_TOKEN=<secure-random-token>

# Cloud providers
CLAUDE_API_KEY=<key>
GEMINI_API_KEY=<key>

# Local inference
VETINARI_MODELS_DIR=/var/lib/vetinari/models
VETINARI_NATIVE_MODELS_DIR=/var/lib/vetinari/models/native
VETINARI_GPU_LAYERS=-1
VETINARI_CONTEXT_LENGTH=4096
VETINARI_VLLM_ENDPOINT=http://127.0.0.1:8000
VETINARI_NIM_ENDPOINT=http://127.0.0.1:8001

# Logging
LOG_LEVEL=INFO
```

### Backend Config Precedence

Runtime backend settings resolve in this order:

1. Environment variables such as `VETINARI_VLLM_ENDPOINT`, `VETINARI_NIM_ENDPOINT`, `VETINARI_MODELS_DIR`, and `VETINARI_NATIVE_MODELS_DIR`
2. User config at `~/.vetinari/config.yaml`
3. Project defaults in `config/models.yaml`

The normalized runtime model is:

- setup-generated configs prefer `nim` first on NVIDIA/CUDA hardware when the endpoint or auto-started container is available
- `vllm` is the native-model fallback and the normal non-NIM native backend when hardware supports it
- `llama_cpp` is used for explicit user preference, weak/no server setups, GGUF-only models, CPU/RAM+VRAM offload, oversized local models, and recovery fallback

---

## Plan Mode

### How It Works

1. **Goal Analysis**: When a task is submitted, Plan Mode generates a plan
2. **Candidate Generation**: Creates 1-3 plan variants with different approaches
3. **Risk Scoring**: Calculates risk based on depth, cost, dependencies
4. **Auto-Approval**: Low-risk plans (score <= 0.25) auto-approved in dry-run
5. **Manual Approval**: High-risk plans require admin approval via API

### Risk Scoring

Risk factors:
- Plan depth (max 16 levels)
- Number of subtasks
- Estimated cost
- Dependency complexity

Risk levels:
- **LOW** (0.0-0.25): Auto-approved
- **MEDIUM** (0.25-0.5): May require approval
- **HIGH** (0.5-0.75): Requires approval
- **CRITICAL** (0.75-1.0): Requires explicit approval

### Dry-Run Mode

When `DRY_RUN_ENABLED=true`:
- Plans generated but not executed
- Auto-approved if risk_score <= DRY_RUN_RISK_THRESHOLD
- Useful for testing and evaluation

---

## Memory Store

### SQLite (Primary)

Location: `./vetinari_memory.db` (or custom path)

Schema:
- `PlanHistory`: Plans, goals, status, risk scores
- `SubtaskMemory`: Subtasks, outcomes, metrics
- `ModelPerformance`: Model success rates, latency

### JSON Fallback

Location: `./vetinari_memory.json`

Used when:
- SQLite unavailable
- `PLAN_USE_JSON_FALLBACK=true`

### Retention

- Default: 90 days
- Configurable via `PLAN_RETENTION_DAYS`
- Automatic pruning on startup

---

## Plan API Endpoints

Guarded plan endpoints require the Litestar admin token in either `X-Admin-Token`
or the `Authorization` header:

```bash
Authorization: Bearer <VETINARI_ADMIN_TOKEN>
```

Current mounted behavior is not "all plan API calls require auth": `GET /api/plan/status`
and `GET /api/plan/templates` are public read surfaces in `litestar_plan_api.py`.
Treat those as localhost-only/operator surfaces until Session 34F1 proves scoped
auth and redaction or routes implementation to guard them.

### POST /api/plan/generate

Generate a plan from a goal.

**Request:**
```json
{
  "goal": "Build a web application",
  "constraints": "Use Python and Litestar",
  "plan_depth_cap": 16,
  "max_candidates": 3,
  "domain_hint": "coding",
  "dry_run": false,
  "risk_threshold": 0.25
}
```

**Response:**
```json
{
  "success": true,
  "plan_id": "plan_abc123",
  "version": 1,
  "goal": "Build a web application",
  "status": "draft",
  "risk_score": 0.15,
  "risk_level": "low",
  "subtask_count": 5,
  "auto_approved": false,
  "plan_candidates": [...]
}
```

### GET /api/plan/{plan_id}

Get plan details.

### POST /api/plan/{plan_id}/approve

Approve or reject a plan.

**Request:**
```json
{
  "approved": true,
  "approver": "admin",
  "reason": "Looks good"
}
```

### GET /api/plan/{plan_id}/history

Get plan history and subtask outcomes.

### GET /api/plan/status

Get plan mode status and memory statistics.

---

## Domain Templates

### Coding
- Define API surface and data models
- Implement core functionality
- Write unit tests
- Integrate with existing components
- Refactor for clarity

### Data Processing
- Define data schema and sources
- Build data ingestion pipeline
- Implement transformation logic
- Implement data quality checks
- Create deployment and scheduling

### Infra
- Define metrics and observability requirements
- Implement health checks and readiness probes
- Create monitoring dashboards
- Configure alerting rules
- Document runbooks

### Docs
- Outline documentation structure
- Draft main sections
- Add usage examples and code snippets
- Review and get feedback
- Finalize and publish

### AI Experiments
- Define experiment metrics and success criteria
- Design experiment configuration
- Run experiments
- Analyze results
- Document insights and recommendations

### Research
- Gather sources and literature
- Summarize findings
- Compare approaches
- Propose recommendations
- Validate against goals

---

## Security

### Admin Token

Set a secure token for plan API access:
```bash
# Generate secure token
python -c "import secrets; print(secrets.token_urlsafe(32))"

# Set in environment
export VETINARI_ADMIN_TOKEN=<generated-token>
```

### API Requests

Guarded plan API calls must include:
```bash
curl -H "Authorization: Bearer <VETINARI_ADMIN_TOKEN>" \
     -H "Content-Type: application/json" \
     ...
```

---

## Troubleshooting

### Plan Mode Not Working

1. Check PLAN_MODE_ENABLE=true
2. Check logs for plan generation errors
3. Verify memory store is accessible

### Memory Store Errors

1. Check PLAN_MEMORY_DB_PATH is writable
2. If SQLite fails, JSON fallback activates automatically
3. Check PLAN_USE_JSON_FALLBACK for fallback behavior

### API 401 Unauthorized

1. Verify `VETINARI_ADMIN_TOKEN` is set for guarded Litestar routes
2. Check Authorization header format: `Bearer <token>`
3. Token must match exactly

---

## See Also

- [Pipeline](../architecture/pipeline.md) - Current runtime pipeline
- [Cloud Ponder](cloud-ponder.md) - Ponder model selection
- [Ponder Contracts](../api/ponder.md) - Current in-process Ponder contract and route-restoration requirements
