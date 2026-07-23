# End-to-End Workflow

This page gives a first-run operator path through AM Workbench: install,
start, run a small goal, inspect status, and capture recovery evidence. The
deeper API and Python examples live in the
[End-to-End AM Workbench Workflow Runbook](../runbooks/end-to-end-workflow.md).

## Prerequisites

- The project is installed in `.venv312` from the repo root.
- `python -m vetinari doctor --json` can run from the same shell.
- At least one inference backend is configured: `VETINARI_MODELS_DIR` for
  llama-cpp GGUF files; or `VETINARI_VLLM_ENDPOINT` / `VETINARI_NIM_ENDPOINT`
  for vLLM/NIM; or `VETINARI_WHISPER_MODEL` for faster-whisper; or a hosted
  provider API key (Anthropic, OpenAI, Gemini, Cohere, HuggingFace, Replicate)
  for cloud-via-LiteLLM routing.
- `VETINARI_ADMIN_TOKEN` is set before server startup when you need mutating
  Workbench routes.

## Golden Path

1. Verify the local environment:

```powershell
.venv312/Scripts/python.exe -m vetinari doctor --json
.venv312/Scripts/python.exe -m vetinari status
```

2. Start the local server on loopback:

```powershell
$env:VETINARI_ADMIN_TOKEN = "<choose-a-local-admin-token>"
.venv312/Scripts/python.exe -m vetinari serve --host 127.0.0.1 --port 5000
```

3. In a second terminal, run a small goal through the CLI:

```powershell
.venv312/Scripts/python.exe -m vetinari start --goal "Summarise the README in five bullets"
```

4. Inspect system state:

```powershell
.venv312/Scripts/python.exe -m vetinari status
.venv312/Scripts/python.exe scripts/check_workbench_health.py
```

5. If the run created outputs or receipts, inspect them through the Workbench
view or the local output directory referenced by the command output. Do not
edit receipt, JSONL, or SQLite state by hand.

## Approval And Guardrail Expectations

AM Workbench should fail closed when authorization, safety, or readiness checks
are unavailable. A blocked action is not automatically a bug:

- `401` on a mutating route usually means the admin token was missing at server
  startup or absent from the client request.
- readiness failures mean one or more Workbench subsystems did not initialize
  cleanly; run `doctor`, `status`, and `scripts/check_workbench_health.py`.
- model-capacity failures should be handled by lowering context length, using
  a smaller quantization for whichever backend is loaded (a lower GGUF quant
  for llama-cpp; GPTQ/AWQ INT4 for vLLM/SGLang; a smaller variant elsewhere),
  reducing GPU layers, or switching to a configured backend with enough
  memory (cloud-via-LiteLLM is always a valid fallback).

## Recovery Loop

When the golden path fails:

1. Preserve the exact command, exit code, and timestamp.
2. Run:

```powershell
.venv312/Scripts/python.exe -m vetinari doctor --json > workflow-doctor.json
.venv312/Scripts/python.exe -m vetinari status > workflow-status.txt
.venv312/Scripts/python.exe scripts/check_workbench_health.py > workflow-workbench-health.txt
```

3. Match the symptom in [Troubleshooting](../troubleshooting.md).
4. Use [Crash Recovery](../runbooks/crash-recovery.md) for unexpected exits.
5. Use [Upgrade, Migration, and Rollback](../runbooks/upgrade-migration-rollback.md)
   before changing application version, migration state, or persistent state.

## Completion Signal

An end-to-end first run is complete when:

- `doctor`, `status`, and Workbench health all exit 0 or name a specific
  degraded subsystem.
- the goal run reaches a terminal success, failure, or blocked state with a
  visible reason.
- any support escalation includes sanitized diagnostics rather than raw local
  state files.
