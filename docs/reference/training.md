# Training and Self-Improvement

AM Workbench continuously improves itself through three interconnected systems:

1. **Idle-Time Training Pipeline** — QLoRA fine-tuning executed during idle periods
2. **Kaizen Continuous Improvement** — PDCA lifecycle for systematic improvement tracking
3. **Feedback Loop** — Execution outcomes feed back into model selection and learning

These systems are designed to close the loop: every task AM Workbench executes produces signals that eventually influence how it performs future tasks.

---

## Architecture Overview

```
Execution outcome
       │
       ▼
 Feedback Loop ──────────────────────────────────────────────┐
       │                                                      │
       ├─► EMA update → Thompson Sampling arms               │
       ├─► Router cache refresh                               │
       └─► Training records (quality ≥ threshold)            │
                                                              │
 Kaizen (AutoGembaWalk)                                       │
       │                                                      │
       ├─► Detect recurring failures, high rework             │
       ├─► Propose PDCA improvements                          │
       └─► Apply, check, confirm or revert                   │
                                                              │
 Idle-Time Training Pipeline ◄────────────────────────────────┘
       │
       ├─► Curriculum selects next activity
       ├─► Synthetic data + replay buffer
       ├─► QLoRA fine-tune with forgetting protection
       └─► Validate → deploy as LoRA adapter
```

---

## Idle-Time Training Pipeline

### Trigger Conditions

The pipeline activates when the system has been idle for 5+ minutes and all preconditions pass:

| Precondition | Threshold |
|---|---|
| Free VRAM | ≥ 8 GB |
| Training records available | ≥ 100 records |
| Base model present | Must be locally available |

Training can also be triggered manually via the CLI (see [CLI Commands](#cli-commands)).

### Pipeline Execution Steps

1. Check preconditions (VRAM, records, base model)
2. Select next activity from the curriculum (priority-based)
3. Mix current training data with replay buffer (80/20 split)
4. Execute QLoRA fine-tuning with continual learning protections active
5. Validate results — regression must be < 5%
6. Deploy as LoRA adapter if validation passes

### Key Files

| File | Purpose |
|---|---|
| `vetinari/training/pipeline.py` | Main training orchestration |
| `vetinari/training/idle_scheduler.py` | Idle detection and scheduling |
| `vetinari/training/validation.py` | Pre- and post-training checks |
| `vetinari/training/continual_learning.py` | Forgetting protection |

---

## Curriculum System

**File:** `vetinari/training/curriculum.py`

The curriculum decides what to train on next. It operates in three phases based on how much data has been collected.

### Phases

| Phase | Condition | Behavior |
|---|---|---|
| CALIBRATION | < 50 training records | Broad assessment across all skills |
| TARGETED_SKILL_BUILDING | ≥ 50 records and weakest skill < 0.7 | Focus on the lowest-scoring skill |
| CONTINUOUS_IMPROVEMENT | All skills ≥ 0.7 | Steady-state improvement across the board |

### Activity Types

Activities are ranked by a computed priority score. The curriculum selects the highest-priority activity that satisfies its prerequisites.

| Activity | Priority Formula | Prerequisites |
|---|---|---|
| Fine-tune weak skill | `(0.7 - score) × 0.4` | Any skill with quality < 0.7 |
| Self-play reasoning | `min(0.8, records / 1000)` | Sufficient training records |
| External data integration | `availability × 0.3` | HuggingFace dataset reachable |
| Prompt evolution | `0.6` (fixed) | ≥ 5 prompt variants available |
| Distillation | `0.7` (fixed) | Overall quality ≥ 0.85 |
| Benchmark practice | `days_stale / 30` | Stale benchmark results |
| RLEF code execution | `0.65` (fixed) | ≥ 30 execution traces available |

View the current prioritized queue:

```bash
vetinari train curriculum
```

---

## Continual Learning Protections

Forgetting protection prevents new training from degrading previously learned skills.

### STABLERegularizer

Computes forward-pass metrics without gradient computation. Training halts if any threshold is exceeded.

| Metric | What It Measures | Threshold | Stop If |
|---|---|---|---|
| EM drop | Embedding norm change | 0.15 | > 2× threshold |
| KL divergence | Output distribution shift | 0.5 | > 2× threshold |
| Bits increase | Information gain per parameter | 0.3 | > 2× threshold |

### ReplayBuffer

Prevents catastrophic forgetting by mixing past examples into every training run.

- **Path:** `~/.vetinari/replay_buffer.jsonl`
- **Maximum size:** 5,000 entries
- **Mix ratio:** 80% current data, 20% replay
- **Sampling:** Stratified by domain to preserve diversity

### LoRAAdapterManager

Each skill gets its own LoRA adapter, preventing cross-skill interference.

- **Registry:** `~/.vetinari/adapters/registry.json`
- Each task type maps to an isolated adapter
- Adapters are independently deployed and versioned

### Adapter Inventory API

Training control and inventory endpoints are owned by the Rust kernel, with
framework-neutral support in `vetinari/training/api_runtime.py`:

| Endpoint | Current owner | Release classification |
|---|---|---|
| `GET /api/v1/training/models` | Rust kernel route surface, Python training runtime support | Native kernel-owned |
| `GET /api/v1/training/adapters?task_type=...` | Rust kernel route surface, Python training runtime support | Native kernel-owned |
| `GET /api/v1/training/adapters/deployed` | Rust kernel route surface, Python training runtime support | Native kernel-owned |

Do not reintroduce Python web route wrappers for these endpoints. Keep sensitive
adapter details behind trusted localhost/native-kernel policy.

---

## Synthetic Data Generation

**File:** `vetinari/training/synthetic_data.py`

When real execution data is sparse, the pipeline generates synthetic training data through four pathways.

| Pathway | Source | Method | Quality Gate |
|---|---|---|---|
| Coding challenge mining | Successful coding episodes | LLM generates variations on accepted episodes | Quality ≥ 0.75 |
| V-STaR reasoning chains | Problem traces | Generate rationale → verify via test execution and Inspector scoring | Quality ≥ 0.80 |
| DPO preference pairs | Same-prompt records | Pair records where score gap ≥ 0.2 as (chosen, rejected) | Score gap ≥ 0.2 |
| Self-play task generation | Templates | Generate tasks across 7 domains × 3 difficulty levels | Varies by domain |

---

## Data Seeding

**File:** `vetinari/training/data_seeder.py`

On first run, bootstrap datasets are candidates for an initial training corpus.
The downloader uses the license gate in `vetinari/training/external_data.py`;
datasets with blocked or review-required license status do not enter default
training without an explicit internal-only override.

| Dataset | Records | Purpose | Release/license status |
|---|---|---|---|
| `codeparrot/apps` | 5,000 | Coding evaluations | Allowed only through the reviewed external-data catalog. |
| `mbpp` | 1,000 | Python basics | Allowed only through the reviewed external-data catalog. |
| `hendrycks/competition_math` | 5,000 | Reasoning problems | Allowed only through the reviewed external-data catalog. |
| `tatsu-lab/alpaca` | 10,000 | Instruction following | Blocked for default/commercial release use because the catalog records `blocked:cc-by-nc-4.0-noncommercial`. |

**Location:** `~/.vetinari/training_data/`

Download manually:

```bash
vetinari train seed
```

---

## Per-Agent Training Priority

**File:** `vetinari/training/agent_trainer.py`

Each agent's training need is scored from three signals:

| Signal | Weight | Description |
|---|---|---|
| Rejection rate | 40% | How often this agent's output is rejected by the Inspector |
| Quality decline | 30% | Drop in recent quality scores vs. historical baseline |
| Staleness | 30% | Days elapsed since the agent last received training |

### Dataset Configuration per Agent

| Agent | Min Quality for Record | Max Training Examples | Training Method |
|---|---|---|---|
| Foreman | 0.82 | 2,500 | SFT |
| Worker | 0.85 | 8,000 | SFT |
| Inspector | 0.80 | 3,000 | DPO |

The Inspector uses DPO (Direct Preference Optimization) because its job is to discriminate between better and worse outputs — preference pairs are the natural training signal for that task.

View per-agent training history:

```bash
vetinari train history
```

---

## Prompt Evolution

The prompt evolution subsystem learns better prompts through systematic mutation and measurement.

### 8 Mutation Operators

**File:** `vetinari/learning/prompt_mutator.py`

| Operator | Effect |
|---|---|
| `INSTRUCTION_REPHRASE` | Reword task instructions while preserving semantics |
| `CONSTRAINT_INJECTION` | Add explicit execution constraints |
| `EXAMPLE_INJECTION` | Insert concrete input/output examples |
| `FORMAT_RESTRUCTURE` | Change output format (e.g., JSON vs. prose) |
| `REASONING_SCAFFOLD` | Add step-by-step reasoning structure |
| `ROLE_REINFORCEMENT` | Strengthen the agent's stated identity |
| `OUTPUT_SCHEMA_TIGHTEN` | Narrow the set of acceptable output forms |
| `CONTEXT_PRUNE` | Remove context sections that do not improve quality |

### Operator Selection via Thompson Sampling

**File:** `vetinari/learning/operator_selector.py`

Each `(operator, agent_type, mode)` combination has an independent Beta distribution arm. The system learns which operators improve which agents in which modes.

- Successful mutations increment `alpha`
- Failed mutations increment `beta`
- Decay factor `0.995` applied per update for non-stationarity adaptation
- Arms require ≥ 5 pulls before results are trusted

State is persisted to `VETINARI_STATE_DIR/operator_selector_state.json` so learning carries across restarts.

### MASPOB Section Ordering

Multi-Agent Section Position Optimization by Permutation tests different orderings of prompt sections and measures quality impact.

Default heuristic: role sections first, constraints near the end, examples immediately before the task statement. The system learns deviations from this heuristic when execution data supports them.

---

## Kaizen Continuous Improvement

**Directory:** `vetinari/kaizen/`

Runtime status: the production scheduler path proposes improvements and reports
PDCA state. Apply/revert automation is available only where a concrete
`ImprovementApplicator` is registered; it is not a blanket code-edit or
execution-loop automation path.

Kaizen applies a PDCA (Plan-Do-Check-Act) lifecycle to systematic improvement tracking. It operates alongside the training pipeline and addresses process-level issues that gradient descent cannot fix directly.

### Improvement States

```
PROPOSED → ACTIVE → CONFIRMED
                  → FAILED
                  → REVERTED
```

### PDCA Lifecycle

**Plan — AutoGembaWalk**

AutoGembaWalk analyzes execution history for patterns that indicate a systemic problem. It proposes an improvement when it detects:

| Signal | Threshold |
|---|---|
| Recurring failure | ≥ 3 occurrences of the same failure pattern |
| High rework rate | > 20% of tasks require rework |
| Low value-add ratio | < 50% of effort produces accepted output |
| Dominant defect cause | > 40% of defects from a single category |
| Week-over-week defect increase | > 15% increase in any defect category |

**Do**

The `PDCAController` applies a proposed improvement only when a registered
applicator exists for that metric. Otherwise the improvement remains a proposal
for review/reporting.

**Check**

The system monitors outcomes over an observation window (default: 7 days). At the end of the window, it evaluates whether quality improved against the threshold.

**Act**

- If improvement is confirmed and has a registered applicator: persist the
  supported override and mark `CONFIRMED`
- If regression is detected for an applied override (> 5%): run the matching
  applicator revert and mark `REVERTED`

### Defect Categories

| Category | Description |
|---|---|
| `HALLUCINATION` | Output contains invented facts |
| `BAD_SPEC` | Task specification was underspecified or contradictory |
| `PROMPT_WEAKNESS` | Prompt did not elicit the required behavior |
| `WRONG_MODEL` | Wrong model selected for the task type |
| `INSUFFICIENT_CONTEXT` | Agent lacked necessary context to complete the task |
| `INTEGRATION_ERROR` | Failure at a system boundary (tool call, API, file I/O) |
| `COMPLEXITY_UNDERESTIMATE` | Task was harder than the planner estimated |

Defects are tracked as hotspots by `(agent_type, mode, defect_category)`. Week-over-week trends are monitored; a > 15% increase in any category triggers an auto-proposal.

### Kaizen CLI

```bash
vetinari kaizen report     # Weekly summary of improvements and outcomes
vetinari kaizen gemba      # Run AutoGembaWalk and show improvement proposals
```

---

## Feedback Loop

**File:** `vetinari/learning/feedback_loop.py`

The feedback loop is what makes the system self-improving rather than just self-monitoring. Every execution outcome flows back into three learning targets.

### Signal Computation

```
signal = 0.5 × success_flag + 0.5 × quality_score
```

Benchmark results carry 3× weight (`alpha = 0.5` instead of the standard `0.2`), because benchmarks are controlled evaluations with known correct answers.

### Update Targets

Each signal update is applied via exponential moving average (EMA, `alpha = 0.2`):

| Target | What Updates | Effect |
|---|---|---|
| Memory store | Agent performance by task type | Future recall surface |
| Router cache | Model quality scores | Model selection for future tasks |
| Thompson Sampling arms | `(operator, agent_type, mode)` arms | Prompt mutation operator selection |

### User Feedback Integration

User feedback is replayed from `outputs/feedback/feedback.jsonl`:

| Feedback | Quality Signal |
|---|---|
| Thumbs up | 0.9 |
| Thumbs down | 0.2 |

User feedback overrides inferred quality when available.

---

## CLI Commands

### Training

```bash
vetinari train status              # Current phase, idle duration, next scheduled activity
vetinari train start               # Manually trigger a training run
vetinari train start --skill X     # Train a specific skill
vetinari train run                 # Run the pipeline directly (bypasses idle check)
vetinari train data                # Dataset statistics (record count, quality distribution)
vetinari train seed                # Download bootstrap seed datasets
vetinari train curriculum          # Show the prioritized activity queue
vetinari train history             # Per-agent training records
```

Pause and resume are parsed legacy subcommands, but they are not wired to a
local scheduler control path. They return a nonzero unsupported result instead
of claiming a state change.

### Kaizen

```bash
vetinari kaizen report             # Weekly improvement summary
vetinari kaizen gemba              # Execution review with auto-generated proposals
```

---

## Configuration

### `config/ml_config.yaml`

Core ML parameters:

```yaml
thompson_sampling:
  prior_alpha: 2.0          # Initial alpha for Beta distribution arms
  prior_beta: 2.0           # Initial beta — symmetric uninformative prior
  cost_weight: 0.15         # Weight given to inference cost in model selection
  exploration_bonus: 0.15   # Added to UCB score to encourage exploration

quality_scoring:
  default_score: 0.55       # Score assigned when no quality signal is available
  ema_alpha: 0.3            # EMA smoothing factor for quality score updates
  min_effect_size: 0.5      # Minimum Cohen's d to count as a real improvement

prompt_evolver:
  min_trials: 20            # Minimum evaluations before a mutation is accepted
  min_improvement: 0.05     # Minimum quality delta to promote a mutation
  significance_level: 0.05  # p-value threshold for statistical significance
```

### `config/quality_thresholds.yaml`

Per-task-type quality gates. Tasks that score below these thresholds are not recorded as training data.

| Task Type | Minimum Quality |
|---|---|
| `security_audit` | 0.85 |
| `test_generation` | 0.80 |
| `code_review` | 0.75 |
| `code` | 0.75 |
| `planning` | 0.65 |
| `creative` | 0.60 |
| `general` | 0.70 |

---

## Installation

Training requires optional GPU dependencies that are not installed by default:

```bash
pip install -e ".[training]"
```

This installs the vanilla training profile: `torch`, `peft`, `trl>=0.24,<0.25`, `bitsandbytes>=0.49,<0.50`, `datasets>=3.4.1,<4.4`, `transformers>=4.57,<5.0`, and `huggingface-hub>=0.36,<2.0`. This profile is designed to coexist with normal runtime, image, and ComfyUI extras.

Use the accelerated Unsloth profile only in a dedicated training environment:

```bash
pip install -e ".[training-unsloth]"
```

The accelerated profile installs `unsloth>=2026.5.9,<2027.0` and `transformers>=5.1,<5.5`; it is intentionally mutually exclusive with the `all`, `gpu`, `vllm`, `image`, and `comfyui` extras because those serving/image profiles remain on the validated Transformers 4.x boundary. Treat this command as the dedicated "training-all" environment: create a separate virtualenv and install only `.[training-unsloth]` plus development tools needed for that training run.

Without these packages, the pipeline operates in **data collection and curriculum planning mode only** — it gathers training records and plans activities but does not execute fine-tuning runs.

---

## State and Persistence

All persistent training state lives under `~/.vetinari/` by default. `VETINARI_STATE_DIR` overrides the location.

| Location | Contents |
|---|---|
| `~/.vetinari/training_data/` | Seed datasets downloaded by `vetinari train seed` |
| `~/.vetinari/replay_buffer.jsonl` | Past training examples for continual learning (max 5,000) |
| `~/.vetinari/adapters/registry.json` | LoRA adapter registry — maps skill names to adapter paths |
| `~/.vetinari/agent_training_history.jsonl` | Per-agent training log with timestamps and metrics |
| `$KAIZEN_DB_PATH` | Kaizen improvement store (SQLite) |
| `$VETINARI_STATE_DIR/operator_selector_state.json` | Thompson Sampling arm states for prompt mutation |
| `outputs/feedback/feedback.jsonl` | User thumbs-up/thumbs-down feedback for replay |

## RCG-0069-P01 Privacy Gate

Training records and replay buffers are represented as FLOW-006 in
`docs/reference/privacy-manifest.json`. RCG-0069-P01 keeps default training use
disabled for every privacy-bearing flow until consent, redaction, retention, and
delete/export linkage are proven by a future runtime owner. The pack verifier is
`scripts/check_privacy_compliance.py`, with branch-discriminating coverage in
`tests/test_privacy_compliance_checker.py`.

---

## Related References

- [Config Reference](config.md) — Full configuration file documentation
- [CLI Reference](cli.md) — All CLI commands
- ADR-0061 — Three-agent factory pipeline (Foreman, Worker, Inspector)
