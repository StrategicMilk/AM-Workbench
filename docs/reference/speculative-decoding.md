# Speculative Decoding

Speculative decoding accelerates local GGUF inference by running a small *draft*
model alongside the main target model.  The draft model generates candidate token
sequences cheaply; the target model then verifies them in a single forward pass.
When the draft's predictions are accepted, the target produces multiple tokens per
forward pass instead of one, yielding 1.5–3x throughput gains with no change to
output quality.

## How It Works

```
User prompt
    │
    ▼
Draft model generates N candidate tokens  (fast, small model)
    │
    ▼
Target model verifies all N candidates in one forward pass
    │
    ▼
Accepted tokens emitted, rejected tokens re-sampled by target model
```

The key property is that verification is **no slower** than normal sampling when
all tokens are rejected, and **much faster** when they are accepted.  Acceptance
rate depends on how closely the draft and target models agree — typically 50–90%
on structured or repetitive output.

## Capability Matrix

| llama-cpp-python version | `draft_model` param | Notes |
|--------------------------|--------------------|----|
| < 0.2.38                 | Not present        | `inspect.signature` detects absence; standard inference used |
| 0.2.38 – 0.2.56          | Present            | Full speculative decoding supported |
| 0.2.57+                  | Present            | Full speculative decoding supported; improved acceptance tracking |

Detection is performed once per process at the first inference call (see
`vetinari/adapters/speculative_decoding.py`).  The result is cached so repeated
calls do not re-inspect the signature.

## Fallback Chain

AM Workbench uses a three-level fallback so that speculative decoding failure never
blocks inference:

1. **Full speculative decoding** — `draft_model_id` is configured in settings AND
   the installed llama_cpp exposes the `draft_model` parameter on `Llama.__init__`.
   The adapter attaches the draft model at load time via `LlamaCppModelCache`.

2. **PromptLookupDecoding** — No `draft_model_id` configured (or draft model not
   found on disk), but `use_prompt_lookup_fallback = True` (default) AND the
   installed llama_cpp has `LlamaPromptLookupDecoding`.  This requires **no extra
   VRAM** and yields ~1.3–1.8x speedup on structured or repetitive output.  The
   `num_pred_tokens` value comes from `speculative_draft_n_tokens` in settings.

3. **Standard inference** — All else.  The adapter logs at DEBUG and proceeds
   normally.  No error is raised.

## Configuration

Add these fields to your environment or `.env` file (all prefixed `VETINARI_`):

```
VETINARI_SPECULATIVE_DECODING_ENABLED=true
VETINARI_SPECULATIVE_DRAFT_MODEL_ID=mistral-7b-instruct-v0.2-q4_0
VETINARI_SPECULATIVE_DRAFT_N_TOKENS=5
```

| Setting | Type | Default | Description |
|---------|------|---------|-------------|
| `speculative_decoding_enabled` | bool | `false` | Master switch.  Must be `true` for any speculative path to activate. |
| `speculative_draft_model_id` | str or null | `null` | Model ID of the small draft model.  Must be a `.gguf` file discoverable in `local_models_dir`. |
| `speculative_draft_n_tokens` | int | `5` | Number of tokens the draft model generates per speculation step.  Higher values help when acceptance rate is high; lower values reduce wasted work. |

`use_prompt_lookup_fallback` is always `true` (hardcoded in
`LlamaCppProviderAdapter._get_speculative_config`) — it requires no extra VRAM and
has no downside, so there is no reason to disable it independently.

## When to Use Speculative Decoding

**Good use cases:**
- Structured output tasks (JSON, code, markdown) where the draft and target agree
  frequently.
- High-throughput scenarios where you have a small spare model (e.g., 3B or 7B
  draft alongside a 70B target).
- Repeated calls with similar prompt prefixes — the PromptLookupDecoding fallback
  is most effective here.

**When not to use it:**
- Creative generation tasks where randomness is important — the draft acceptance
  rate is low and the overhead dominates.
- Memory-constrained environments — a draft model requires additional VRAM.
  PromptLookupDecoding avoids this cost.
- When llama_cpp < 0.2.38 is pinned — capability detection will find `draft_model`
  absent and fall back to standard inference automatically.

## Performance Expectations

| Scenario | Typical speedup |
|----------|----------------|
| PromptLookupDecoding (structured) | 1.3–1.8x |
| Draft model (7B → 70B) coding | 1.8–2.5x |
| Draft model (3B → 70B) creative | 1.1–1.4x |
| No speculative decoding | 1.0x (baseline) |

Speedup is measured in tokens per second.  Actual results depend on hardware,
quantization, and content type.
