# AM Workbench API Reference Index

This is an inventory-level reference for Python API support modules under
`vetinari/web/api/`. It is not an endpoint-by-endpoint schema contract. The
migrated AM Workbench API-domain route surface is owned by the native Rust
kernel router in `crates/amw-kernel/src/api/routes/workbench_domains.rs`; the
Python modules listed here remain support logic, protected sibling surfaces, or
historical compatibility coverage.

Regenerate the module set with:

```powershell
Get-ChildItem vetinari\web\api -File -Filter *.py |
  Where-Object Name -ne '__init__.py' |
  ForEach-Object { $_.BaseName } |
  Sort-Object
```

## Module Inventory

| Module | Kind | Reference scope |
|---|---|---|
| `_plan_feedback_dto` | Python module | Plan-feedback DTO helpers |
| `capability_api` | Python module | Capability records and capability-pack API helpers |
| `clarification_api` | Python module | Clarification request/response API helpers |
| `destructive_api` | Python module | Destructive-action guard API helpers |
| `exception_identity` | Python module | Exception identity serialization helpers |
| `extensions_api` | Python module | Extensions discovery and manifest API helpers |
| `full_spectrum_audit_results_api` | Python module | Full-spectrum audit-results inspection API helpers |
| `glossary_api` | Python module | Glossary API helpers |
| `intake_wizard_api` | Python module | Intake wizard API helpers |
| `litestar_user_export_api` | Python module | User export API helpers |
| `model_hub_api` | Python module | Model-hub catalog browsing API helpers |
| `models_api` | Python module | Model inventory and model action API helpers |
| `plan_feedback_api` | Python module | Plan feedback API helpers |
| `plan_runtime_api` | Python module | Plan runtime API helpers |
| `preferences_api` | Python module | User and workspace preferences API helpers |
| `projects_api` | Python module | Project API helpers |
| `scheduled_tasks_api` | Python module | Scheduled-task creation and inspection API helpers |
| `spec_frame_api` | Python module | Spec-frame API helpers |
| `workbench_adaptive_tuning_api` | Python module | Workbench adaptive tuning API helpers |
| `workbench_annotation_api` | Python module | Workbench annotation API helpers |
| `workbench_approval_chain_api` | Python module | Workbench approval-chain API helpers |
| `workbench_artifact_review_api` | Python module | Workbench artifact-review API helpers |
| `workbench_benchmark_import_api` | Python module | Workbench benchmark-import API helpers |
| `workbench_capability_packs_api` | Python module | Workbench capability-pack API helpers |
| `workbench_channels_api` | Python module | Workbench channel API helpers |
| `workbench_command_safety_api` | Python module | Workbench command-safety API helpers |
| `workbench_console_api` | Python module | Workbench console API helpers |
| `workbench_console_api_schemas` | Python module | Workbench console API schema definitions |
| `workbench_context_enrichment_api` | Python module | Workbench context-enrichment API helpers |
| `workbench_domain_kits_api` | Python module | Workbench domain-kit API helpers |
| `workbench_evidence_assets_api` | Python module | Workbench evidence-asset API helpers |
| `workbench_experiment_lab_api` | Python module | Workbench experiment-lab API helpers |
| `workbench_experiment_lab_api_schemas` | Python module | Workbench experiment-lab API schema definitions |
| `workbench_extensions_api` | Python module | Workbench extension API helpers |
| `workbench_failure_intelligence_api` | Python module | Workbench failure-intelligence API helpers |
| `workbench_gateway_policy_api` | Python module | Workbench gateway-policy API helpers |
| `workbench_habit_health_api` | Python module | Workbench habit-health API helpers |
| `workbench_interaction_api` | Python module | Workbench interaction API helpers |
| `workbench_knowledge_vault_api` | Python module | Workbench knowledge-vault API helpers |
| `workbench_lifecycle_api` | Python module | Workbench lifecycle API helpers |
| `workbench_managed_agents_api` | Python module | Workbench managed-agent API helpers |
| `workbench_memory_api` | Python module | Workbench memory API helpers |
| `workbench_method_library_api` | Python module | Workbench method-library API helpers |
| `workbench_migration_api` | Python module | Workbench migration API helpers |
| `crates/amw-kernel/src/api/routes/workbench_domains.rs::mission_control_snapshot_payload` | Rust symbol | Native Rust Workbench mission-control runtime projection |
| `vetinari/workbench/mission_control.py` | Python file path | Historical/read-only Python projection retained for tests and contract reference |
| `vetinari/workbench/mission_control_types.py` | Python file path | Workbench mission-control type definitions |
| `workbench_mode_api` | Python module | Workbench mode API helpers |
| `workbench_model_choices_api` | Python module | Workbench model-choice API helpers |
| `workbench_model_registry_api` | Python module | Workbench model-registry API helpers |
| `workbench_notebooks_api` | Python module | Workbench notebook API helpers |
| `workbench_onboarding_api` | Python module | Workbench onboarding API helpers |
| `workbench_playground_api` | Python module | Workbench playground API helpers |
| `workbench_policy_explainability_api` | Python module | Workbench policy-explainability API helpers |
| `workbench_prompt_engineering_api` | Python module | Workbench prompt-engineering mutation and optimizer API helpers |
| `workbench_private_ai_api` | Python module | Workbench private-AI API helpers |
| `workbench_professional_life_api` | Python module | Workbench professional-life API helpers |
| `workbench_promotion_api` | Python module | Workbench promotion API helpers |
| `workbench_rag_api` | Python module | Workbench RAG API helpers |
| `workbench_readback_support` | Python module | Workbench readback support helpers |
| `workbench_readiness_api` | Python module | Workbench readiness API helpers |
| `workbench_repro_capsules_api` | Python module | Workbench repro-capsule API helpers |
| `workbench_resource_cockpit_api` | Python module | Workbench resource-cockpit API helpers |
| `workbench_resource_cockpit_api_helpers` | Python module | Workbench resource-cockpit API helper utilities |
| `workbench_run_kernel_api` | Python module | Workbench run-kernel API helpers |
| `workbench_shell_api` | Python module | Workbench shell API helpers |
| `workbench_source_cards_api` | Python module | Workbench source-card API helpers |
| `workbench_status_api` | Python module | Workbench status API helpers |
| `workbench_tool_guides_api` | Python module | Workbench tool-guide API helpers |
| `workbench_tool_output_squasher_api` | Python module | Workbench tool-output-squasher API helpers |
| `workbench_update_api` | Python module | Workbench update API helpers |
| `workbench_work_graph_api` | Python module | Workbench work-graph API helpers |
| `workbench_workflow_builder_api` | Python module | Workbench workflow-builder API helpers |

## Operator Reachability Notes

These routes are covered by focused wiring tests and are intended operator entrypoints, not dormant helper code:

| Surface | Route family | Backing behavior |
|---|---|---|
| Conversation export and search | `/api/v1/chat/export/{project_id}`, `/api/v1/chat/search` | Reads project `conversation.json`, redacts export/search content, and blocks unauthenticated remote reads. |
| Named prompt library | `/api/v1/system-prompts` | Lists and writes reusable prompt templates under `system_prompts/*.txt`. |
| RAG ingestion and debugger | `/api/workbench/rag/ingest/*`, `/api/workbench/rag/datasets/*/replay` | Calls the shared `KnowledgeBase` ingestion backend and the retrieval replay lab with hybrid/reranker knobs. |
| Prompt engineering | `/api/workbench/prompt-engineering/*` | Calls `PromptMutator` and `PromptOptimizer` for deterministic mutations, heuristic search, and experiment history. |
| Native kernel API boundary | `/health`, `/ready`, `/api/*`, `/api/v1/*`, `/api/workbench/*`, `/api/v1/workbench/*`, `/api/v1/projects/{project_id}/workbench/*` | Owned by the native Rust kernel router and invoked by Tauri through `vetinari_kernel_request`; Python modules listed above remain support logic, compatibility coverage, or protected sibling services. |
| MCP tool and resource exposure | `/mcp/tools`, `/mcp/message`, `/mcp/resources`, `/mcp/resources/read`, `/mcp/resources/stream` | Protected sibling surface owned by the MCP transport module. |
