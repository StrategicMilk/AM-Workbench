# Configuration Matrix

This document maps Vetinari's configuration files to their owning modules and test coverage. It reflects the state as of 2026-06-27.

## Overview

Configuration lives in `config/` as YAML or JSON files. The loader at `vetinari/config/loader.py` reads and caches config at startup; route handlers never call `yaml.safe_load()` directly (enforced by VET rule).

Tests for config behavior are split across multiple dedicated test files — they are fully implemented and exercised in CI.

## Config File Registry

| Config file | Owning module | Test file(s) |
|-------------|--------------|-------------|
| `config/agent_model_defaults.yaml` | `vetinari/config/loader.py` | `tests/test_backend_config.py` |
| `config/agent_routing_policy.yaml` | `vetinari/orchestration/request_routing.py` | `tests/test_backend_config.py` |
| `config/capabilities.yaml` | `vetinari/capabilities/` | `tests/test_cli_configuration_health.py` |
| `config/compute_routing.yaml` | `vetinari/adapters/registry.py` | `tests/test_backend_config.py` |
| `config/gateway_policy.yaml` | `vetinari/security/request_guards.py` | `tests/test_guardrails_fail_closed.py` |
| `config/guardrails/` | `vetinari/guardrails/` | `tests/test_guardrails_fail_closed.py` |
| `config/inference_profiles.yaml` | `vetinari/config/inference_config.py` | `tests/test_inference_config.py`, `tests/test_inference_config_catalog_defaults.py` |
| `config/llamacpp_engine_defaults.yaml` | `vetinari/adapters/llama_cpp_adapter.py` | `tests/test_model_config.py` |
| `config/mcp_servers.yaml` | `vetinari/mcp/` | `tests/test_guardrails_fail_closed.py` |
| `config/models.yaml` | `vetinari/adapters/registry.py` | `tests/test_model_config.py`, `tests/test_models_inference_config.py` |
| `config/network_policy.yaml` | `vetinari/workbench/network/config.py` | `tests/test_guardrails_fail_closed.py` |
| `config/quality_baselines.yaml` | `vetinari/ml/quality_prescreener.py` | `tests/test_inference_config.py` |
| `config/quality_thresholds.yaml` | `vetinari/learning/self_refinement.py` | `tests/test_inference_config.py` |
| `config/safety_defaults.yaml` | `vetinari/guardrails/` | `tests/test_guardrails_fail_closed.py` |
| `config/task_inference_profiles.json` | `vetinari/config/inference_config.py` | `tests/test_inference_config_catalog_defaults.py` |
| `config/test_lanes.yaml` | `scripts/run_tests.py` | `tests/operator/test_docs_config_and_readme_hygiene.py` |
| `config/vet_rules.yaml` | `scripts/check_vetinari_rules.py` | `tests/operator/test_vet_rules_fixtures.py` |
| `config/workbench/` | `vetinari/workbench/` | `tests/test_workbench_effective_config.py` |
| `config/workbench_modes.yaml` | `vetinari/workbench/modes/templates.py` | `tests/test_workbench_mode_templates.py` |
| `config/workbench_scheduler.yaml` | `vetinari/runtime/workbench_scheduler.py` | `tests/runtime/test_workbench_scheduler.py` |

## Test Coverage Status

**Tests are fully implemented and split across multiple files.** As of 2026-06-27 there are 1,363 `test_*.py` files under `tests/`. Config-related test files include:

- `tests/test_backend_config.py` — backend selection, routing policy, compute routing
- `tests/test_cli_configuration_health.py` — CLI config health checks, capability flags
- `tests/operator/test_docs_config_and_readme_hygiene.py` — config doc hygiene and lint
- `tests/test_inference_config.py` — inference profile loading and validation
- `tests/test_inference_config_catalog_defaults.py` — catalog default profile contract
- `tests/test_workbench_effective_config.py` — Workbench config isolation and effective config resolution
- `tests/test_guardrails_fail_closed.py` — guardrail defaults, fail-closed behavior, and request safety checks
- `tests/test_model_config.py` — model registry, llamacpp engine defaults
- `tests/test_models_inference_config.py` — model-to-inference-profile binding
- `tests/test_rag_migrations_tools_webconfig.py` — RAG, migration, and web config
- `tests/test_workbench_effective_config.py` — workbench effective config resolution

Any prior claim that config tests are "unimplemented" or "single-file" is stale and incorrect.

## Adding New Config

When adding a new config file:

1. Place YAML/JSON in `config/` (or a subdirectory for grouped settings)
2. Add a loader/accessor in `vetinari/config/` that caches the result at module level
3. Register the config key in `config/vet_rules.yaml` if it needs lint enforcement
4. Add a test in the appropriate `tests/test_*_config.py` file asserting the loaded value matches the schema
5. Add a row to this matrix
