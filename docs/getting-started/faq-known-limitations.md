# FAQ, Known Limitations, and Support

This page collects the support information a first-time AM Workbench operator
needs after the quick start: what is intentionally limited, what evidence to
collect, and where to escalate.

## Support Escalation

Public issue tracker:
`https://github.com/StrategicMilk/AM-Workbench/issues`

Before filing an issue, run:

```powershell
python -m vetinari doctor --json > doctor.json
python -m vetinari status > status.txt
python scripts/check_workbench_health.py > workbench-health.txt
```

Attach sanitized command output, the failing command, the exit code, the
platform, Python version, and the install command you used. Do not attach raw
SQLite databases, JSONL stores, model files, prompt text, API keys, or local
absolute paths. Remove local absolute checkout paths from logs before sharing.

## FAQ

### Does AM Workbench require a cloud model account?

No. The default posture is local-first. Local GGUF models and local vLLM or NIM
endpoints can run without sending prompts to cloud providers. Cloud adapters are
available only when you configure the provider key and choose that backend.

### Why do some Workbench requests return 401?

Routes that create, modify, or delete Workbench resources (projects, training
runs, model registrations) require admin proof. Set `VETINARI_ADMIN_TOKEN`
before server startup and send the matching `Authorization: Bearer <token>` or
`X-Admin-Token` header from the client. Read-only routes such as health checks,
project listings, and metrics do not require an admin token.

### Why does `/dashboard` return 404?

The legacy HTML dashboard route is not mounted by the current native API server.
Use Workbench views in the Svelte app and the JSON dashboard endpoints such as
`/api/v1/dashboard`, `/api/v1/dashboard/health`, and `/api/v1/metrics/latest`.
The legacy template source is retained as dormant source only.

### What should I do when the model cannot be found?

Run `python -m vetinari models scan`, verify `VETINARI_MODELS_DIR`, and make
sure the directory contains the model format you expect. For private or gated
Hugging Face downloads, set `HF_TOKEN` and confirm the account has accepted the
model license.

### Can I expose AM Workbench to a network?

The supported default is loopback on `127.0.0.1` for one trusted operator. If
you expose the service beyond localhost, configure an admin token, TLS through
a trusted reverse proxy, remote-read/mutation policy, and rate limiting before
accepting traffic.

### Does changing config hot-reload a running server?

No. Configuration changes are cold reload only. Restart the server after
changing environment variables, YAML config, model roots, or backend endpoints.

## Known Limitations

| Area | Current limitation | Practical action |
|---|---|---|
| Users and auth | Single-user local operator model; no multi-user RBAC. | Keep the app on loopback unless a trusted proxy and admin token are configured. |
| TLS | No built-in TLS termination. | Terminate TLS at a reverse proxy before remote exposure. |
| Backups | No automatic backup or remote sync. | Use [Upgrade, Migration, and Rollback](../runbooks/upgrade-migration-rollback.md) before risky changes. |
| Windows vLLM | vLLM is not the native Windows path. | Run `.\start-vllm-wsl.ps1` and point `VETINARI_VLLM_ENDPOINT` at the WSL endpoint; the helper disables the FlashInfer sampler by default for the verified RTX 5090 path. |
| Dashboard route | Legacy `/dashboard` HTML route is not mounted. | Use Workbench views and dashboard JSON endpoints. |
| Cloud backends | Cloud adapters are opt-in and less exercised than the local path. | Validate with `doctor`, a small goal, and a rollback plan before relying on them. |
| MCP marketplace distribution | OAuth-declared marketplace rows support PKCE authorization request, token exchange, and bearer-authenticated install probing, but hosted marketplace publication and dynamic client registration are not claimed. | Use local admin-token guarded MCP transports; keep marketplace rows disabled by default until Workbench risk checks and manual selection allow them. |
| Config reload | Server config is loaded at startup. | Restart after config changes. |

External publication, community, survey, and marketplace-listing evidence gaps
remain in private release-planning records. They are go-to-market work, not
product/runtime known limitations.

## Evidence Guard

The public support URLs and dormant dashboard-route limitation are covered by
`tests/test_frontend_package_boundary.py`. That test rejects stale
`StrategicMilk/Vetinari` community URLs and requires route metadata for retired
Litestar surfaces to fail closed to the Rust/Axum kernel.

## Where To Look Next

- [Quick Start](quick-start.md) for first-run setup.
- [Troubleshooting Guide](../troubleshooting.md) for common failures.
- [Upgrade, Migration, and Rollback](../runbooks/upgrade-migration-rollback.md)
  before changing versions or persistent state.
- [MCP Server Guide](../reference/mcp-server.md) for editor integration.
