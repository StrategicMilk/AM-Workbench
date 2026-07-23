# Training Version Lattice

Vetinari has one training implementation with two dependency cells. `training` is the
vanilla TRL/PEFT path and deliberately excludes Unsloth. `training-unsloth` adds the
accelerator and its compatible Transformers cell. The vanilla path is a sanctioned
`degraded-performance` mode, never degraded correctness or a silent fallback.

## Invariants

| Package | `training` | `training-unsloth` | Constraint source |
|---|---|---|---|
| torch | `>=2.7.0,<2.11` | same | CUDA sm_120 floor and Unsloth 2026.5.x ceiling |
| datasets | `>=3.4.1,<4.4` | same | Unsloth 2026.x dataset ceiling |
| peft | `>=0.19,<1.0` | same | shared adapter contract |
| trl | `>=0.24,<0.25` | same | validated TRL 0.24 / Unsloth 2026.5.x cell |
| transformers | `>=4.57,<5.0` | `>=5.1,<5.5` | intentional vanilla/image compatibility versus accelerator cell |
| bitsandbytes | `>=0.49,<0.50` | same | validated quantization band; Blackwell may require a source build |
| huggingface-hub | `>=0.36,<2.0` | same | shared model acquisition contract |
| unsloth | absent | `>=2026.5.9,<2027.0` with the non-Apple-Silicon marker | accelerator-only cell |

The requirement strings in `pyproject.toml` are authoritative install inputs. The
independent contract in `scripts/check_training_version_lattice.py` is intentionally
versioned rather than inferred from those live strings, so a coordinated-band drift is
detectable. It normalizes names and markers and rejects missing cells, duplicates,
unbounded core members, shared-band drift, the wrong Transformers cell, or an invalid
Unsloth membership rule. Unreadable and malformed TOML fails closed.

## Ownership and bump windows

Training dependency changes use coordinated bump windows. No package in this lattice
is bumped in isolation.

A bump window proceeds in this order:

1. Update the compatibility table and checker contract from primary upstream evidence.
2. Change both optional-dependency cells together, retaining their intentional
   Transformers difference and vanilla Unsloth exclusion.
3. Run the known-bad fixture and require checker exit code 1 for the intended rule.
4. Run the known-good fixture and current `pyproject.toml`, requiring exit code 0.
5. Run the focused training regressions and the governed exit-hatch workflow.
6. Accept the window only after its uploaded receipt proves the real vanilla pipeline.

If any relation or governed proof fails, roll back the whole bump window. Do not relax
one edge, skip the GPU proof, or re-label an incomplete result as compatible.

## Enforcement and proof receipt

Use these local controls:

```powershell
.venv312/Scripts/python.exe scripts/check_training_version_lattice.py --pyproject tests/fixtures/pyproject_lattice_known_bad.toml
.venv312/Scripts/python.exe scripts/check_training_version_lattice.py --pyproject tests/fixtures/pyproject_lattice_known_good.toml
.venv312/Scripts/python.exe scripts/check_training_version_lattice.py
.venv312/Scripts/python.exe scripts/run_training_exit_hatch_proof.py --bootstrap-vanilla
```

The bootstrapped command is the reproducible local/main-session proof. It creates a
disposable environment, installs the governed CUDA Torch wheel from the official
CUDA 12.8 index before the vanilla `training` extra, proves that Unsloth is absent,
and then runs the same live receipt-producing entrypoint. Direct invocation without
`--bootstrap-vanilla` remains the workflow form because that runner has already
created the clean environment.

`.github/workflows/training-exit-hatch.yml` runs weekly and by manual dispatch on the
governed GPU runner. It installs the vanilla `training` cell plus test tooling and the
quality gate's pyproject-governed `jsonschema` requirement, then replaces the generic
Torch wheel with the matching official CUDA wheel. It rejects an importable Unsloth,
executes `TrainingPipeline.run` through the existing `use_unsloth=False` switch, and
uploads a `degraded-performance` receipt. A valid receipt records the immutable tiny
model revision, dependency versions, actual TRL backend, at least two finite decreasing
evaluation-loss observations, nonempty hashed adapter and deployment artifacts,
persisted run/evaluation evidence, and the production `quality_gate=deploy` decision.
Missing CUDA quantization, missing artifacts, corrupt state, or any non-deploy decision
is a failed proof, not a skipped or successful lane.

The forgetting gate measures `D_KL(P_baseline || P_adapter)` on the same deterministic
probe inputs. The pre-training output distribution remains untouched; the current
distribution is loaded from the matching PEFT adapter and fails closed on base-model
provenance mismatch. The deployment quality gate selects a typed
`peft_adapter`/`safetensors` evaluator for this artifact, compares deterministic
expected-continuation loss on CUDA, and preserves the existing local/GGUF evaluator for
registered inference models. Neither gate treats fallback output as evidence.
