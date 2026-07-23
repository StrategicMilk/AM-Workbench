# Known Limitations Status

This page is the readable companion to the internal Wave 1 AM Workbench
migration remediation ledger and post-remediation preservation ledger.

The ledger is binding inventory, not closure evidence. A limitation is not
remediated just because it appears in the ledger. Owning migration packs must
cite substantive source, tests, docs, config, UI, schema, or workflow-script
evidence before removing a limit from product-facing docs.

## Product-Limit Closure Status

| Limit | Current status | Owner | Required closure proof |
|---|---|---|---|
| Single-user localhost deployment boundary | Implemented | `AMW-FULL-16` | Local-only non-claim and remote-token boundary are explicit in `README.md`, `docs/security/route-auth-matrix.md`, and production docs. |
| Cold config reload | Implemented | `AMW-FULL-16` | Restart-only operator guidance is preserved in README/config troubleshooting, with no hot-reload overclaim. |
| Windows vLLM through WSL | Implemented | `AMW-FULL-06` | README and config docs preserve the WSL endpoint boundary through `VETINARI_VLLM_ENDPOINT`; native Windows vLLM is not claimed. |
| No automatic backups | Implemented | `AMW-FULL-16` | Operator backup/restore workflow and drill proof exist in `scripts/backup_restore_state.py`, `scripts/check_backup_restore_drill.py`, `docs/troubleshooting.md`, and production docs. |
| Cloud adapter production proof | Implemented | `AMW-FULL-06` | Cloud paths are opt-in with network policy and provider disclosure; support rows fail closed through `config/workbench/network_transport.yaml`, `config/workbench/workbench_surface_maturity.json`, and route receipts. |
| Support-matrix runner gaps | Implemented | `AMW-FULL-17` | `config/support_matrix.yaml` records supported rows with proof commands and keeps unproved cells unsupported/experimental; Windows 11 NVIDIA 8/16/24/32 GB-class local inference, local training, native Rust kernel API, CLI, and agent-pipeline cells now use runnable proof commands, and Ubuntu 24.04 WSL NVIDIA 8/16/24/32 GB-class local inference, training, native Rust kernel API, CLI, and agent-pipeline rows are verified. `scripts/check_support_matrix_freshness.py` and endpoint-capability checks enforce drift. |
| MCP Streamable HTTP single-endpoint migration | Implemented | `AMW-FULL-06` | `/mcp` is a guarded Streamable HTTP JSON-RPC endpoint; `/mcp/message`, `/mcp/tools`, and resource SSE routes remain supported. |
| OAuth-backed marketplace installation | Implemented | `AMW-FULL-07` | PKCE authorization request, token exchange, redacted token serialization, bearer-authenticated install probing, CLI flags, and admin-gated API routes are implemented. |
| Migration Wizard default-on target | Implemented | `AMW-FULL-04` | Default-on route/backend proof is present through the native workbench surface policy, Tauri kernel command, Rust kernel route registry, and Svelte view. |
| Extensions Marketplace opt-in target | Implemented | `AMW-FULL-07` | Marketplace route/backend proof is present; native and Svelte paths preserve opt-in/disabled-by-default risk verdict enforcement. |
| Habit Health opt-in target | Implemented | `AMW-FULL-05` | Opt-in route/backend proof is present through the native workbench surface policy, Tauri kernel command, Rust kernel route registry, and local-only Habit Health view/API. |
| Training CLI server controls | Implemented | `AMW-FULL-06` | Start/pause/resume/stop/cancel/checkpoint/jobs commands route through the native Rust training control state, CLI, and admin-gated kernel routes with audit receipts. |
| Dataset remote backends | Implemented | `AMW-FULL-06` | DVC/lakeFS/Lance-style typed remotes support push/pull/sync, auth references, offline refusal, conflict handling, and audit receipts. |
| Local-first IDE extension | Implemented | `AMW-FULL-07` | Loopback/session/CSRF/origin-bound IDE goal submission exists in `vetinari/workbench/ide_extension/local_first.py` without a cloud control plane. |

Unknown, unreadable, missing, or corrupt safety, provenance, training,
governance, dataset, live-action, or MCP transport state fails closed. The
checker suite under `scripts/check_*` enforces that this inventory remains
explicit rather than silently accepting missing signals.
