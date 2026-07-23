# Security Data Inventory

Authoritative source: `docs/security/data-inventory.json`.

The inventory enumerates the RCG-0021 stores and external data flows: training collector, memory store, dashboard log streaming, Datadog log shipping, webhook log shipping, support bundles, `outputs/` scratch, Workbench network evidence, resource accounting ledgers, cloud provider inference, GenAI trace export, and chat conversation export. Retention classes reference `vetinari/memory/governance/lifecycle.py::RetentionClass` and `MemoryGovernanceRecord`; this document is only a human-readable summary.

| Store | Path | Classification | Consent Basis | Retention |
|---|---|---|---|---|
| `training_collector` | `training_data.jsonl` | high-sensitivity-user-content | local-only | `training-records-30d` |
| `memory_store` | `vetinari_memory.db::memories` | high-sensitivity-user-content | local-only | `memory-active-plus-tombstone` |
| `dashboard_log_streaming` | `logs/vetinari_audit.jsonl` and SSE buffer | high-sensitivity-user-content | local-only | `logs-14d` |
| `dashboard_datadog_log_shipping` | Datadog Logs Intake API | high-sensitivity-user-content | explicit-opt-in | `external-log-shipping-operator-owned` |
| `dashboard_webhook_log_shipping` | operator-configured webhook endpoint | high-sensitivity-user-content | explicit-opt-in | `external-log-shipping-operator-owned` |
| `launcher_support_bundles` | operator-selected `.zip` | high-sensitivity-user-content | explicit-opt-in | `support-bundles-operator-owned` |
| `outputs_scratch` | `outputs/` | high-sensitivity-user-content | local-only | `outputs-scratch-14d` |
| `rag_debugger_experiments` | `outputs/workbench/rag_debugger/<project_id>/experiments.jsonl` | high-sensitivity-user-content | local-only | `rag-debugger-experiments-14d` |
| `resource_accounting_lease_registry` | `outputs/workbench/resource_cockpit/lease_registry.json` | low-sensitivity-usage-metadata | local-only | `resource-accounting-30d` |
| `resource_accounting_ledgers` | `outputs/workbench/resource_cockpit/*.jsonl` | low-sensitivity-usage-metadata | local-only | `resource-accounting-30d` |
| `training_resource_ledger` | `outputs/workbench/training/ledger.jsonl` | low-sensitivity-usage-metadata | local-only | `resource-accounting-30d` |
| `workbench_network_evidence` | Workbench network JSON artifacts | low-sensitivity-usage-metadata | local-only | `network-evidence-30d` |
| `cloud_provider_inference` | operator-enabled cloud inference APIs | high-sensitivity-user-content | explicit-opt-in | `cloud-provider-operator-owned` |
| `genai_trace_export` | GenAI trace export JSON | high-sensitivity-user-content | local-only | `trace-exports-14d` |
| `chat_conversation_export` | export and attachment responses | high-sensitivity-user-content | explicit-opt-in | `chat-exports-operator-owned` |

## Runtime Redaction Gates

Training records, memory entries, structured logs, external log-shipping payloads, support bundle text files, GenAI trace tool payloads, RAG debugger experiments, resource accounting ledgers, and chat export/attachment bytes use `vetinari.safety.guardrails.redact_pii`, resource JSONL redaction, or the shared payload wrapper before data crosses a persistence, response, or network boundary. Workbench network evidence keeps its domain-specific `redact_network_evidence` gate because it redacts local paths, IP addresses, headers, and network credentials in addition to generic PII.

## Resource Accounting

Resource accounting records project ids, lease ids, scheduler lanes, model ids, token counts, GPU-hours, and `total_cost_usd`. It does not store raw prompts, responses, secrets, or unredacted user payloads by default. `config/resource_pricing.yaml` is the local pricing authority; missing or unreadable pricing makes accounting surfaces degrade instead of self-attesting success. Lease registry writes use atomic replace and JSONL ledger writes rotate before configured byte or line budgets are exceeded.

## Training-Record Deletion

Training-record retention is executable, not operator-only: `TrainingDataCollector.purge_expired_records(cutoff_days=30)` removes expired JSONL rows, and startup maintenance calls it through the local runtime lifecycle path. Subject deletion uses `TrainingDataCollector.delete_records_for_subject()` to remove matching training rows and trace artifact directories.

## Tombstone Semantics

Memory deletion is tombstone-first. `UnifiedMemoryStore.forget()` marks the row forgotten and overwrites content, summary, and metadata so active read APIs do not return residual raw content. `compact_memories()` is the physical removal step for forgotten rows.
