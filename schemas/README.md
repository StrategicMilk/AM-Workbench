# Schemas

JSON Schema definitions for Vetinari's structured file formats.

Schema files are runtime contracts, not advisory examples. New schema roots
must fail closed by default: set `additionalProperties: false` unless a field is
explicitly documented as an extension point, require the fields consumed by live
readers, and add a focused negative fixture or unit test for unknown root fields
and missing required fields.

Ownership for schema changes follows the consuming subsystem. Audit lane packet,
lane coverage, and structured finding schemas are owned by the full-spectrum
audit workflow; Workbench spine schemas are owned by the Workbench spine binding
model. Closure evidence must cite both the schema file and the test or validator
that exercises the rejected bad payload and accepted good payload.

## Consuming Subsystems

| Prefix or file family | Consuming subsystem | Schema files |
|---|---|---|
| `aks_*` | Agent kernel/session compatibility checks | `aks_bundle_compat.schema.json` |
| `amw-*` | AM Workbench foundation and protocol receipts | `amw-foundation-contract.schema.json`, `amw-kernel-lifecycle.schema.json`, `amw-protocol-receipt.schema.json` |
| `audit_*` and `audit-*` | Full-spectrum audit lanes and structured findings | `audit_ground_truth.schema.json`, `audit-action-intent.schema.json` |
| `brainstorm-*` | Brainstorm artifact typing | `brainstorm-artifact-intent.schema.json` |
| `finding-*` | Finding registry, disposition, proof, and structured-field contracts | `finding-disposition.schema.json`, `finding-proof-tier.schema.json`, `finding-structured-fields.schema.json` |
| `ide_*` | IDE extension metadata and boundary contracts | `ide_extension.schema.json` |
| `inference_*` | Inference quality and backend rubric contracts | `inference_quality_rubric.schema.json` |
| `lane-*` | Audit lane packet and lane coverage validation | `lane-coverage.schema.json`, `lane-packet.schema.json` |
| `language_*` | Language/runtime boundary declarations | `language_boundary.schema.json` |
| `program-*` | Program wave planning and execution intent | `program-wave-intent.schema.json` |
| `shard` | Generated shard plan validation | `shard.schema.json` |
| `workbench_adapter*` | Adapter authority and provider boundary records | `workbench_adapter_authority.schema.json` |
| `workbench_adaptive*` | Adaptive tuning state | `workbench_adaptive_tuning.schema.json` |
| `workbench_agent*` | Managed agents, routing, watcher, mailbox, cards, and harness evidence | `workbench_agent_card.schema.json`, `workbench_agent_mailbox.schema.json`, `workbench_agent_route_decision.schema.json`, `workbench_agent_run_harness.schema.json`, `workbench_agent_watcher.schema.json` |
| `workbench_ai*` | AI bundle metadata | `workbench_ai_bundle.schema.json` |
| `workbench_anti*` | Anti-sycophancy eval records | `workbench_anti_sycophancy_eval.schema.json` |
| `workbench_approval*` | Approval chain and approval diff records | `workbench_approval_chain.schema.json`, `workbench_approval_diff.schema.json` |
| `workbench_artifact*` | Artifact pair and artifact review records | `workbench_artifact_pair.schema.json`, `workbench_artifact_review.schema.json` |
| `workbench_automation*` | Automation definitions, assets, and shadow records | `workbench_automation.schema.json`, `workbench_automation_asset.schema.json`, `workbench_automation_shadow.schema.json` |
| `workbench_card` | Workbench card metadata | `workbench_card.schema.json` |
| `workbench_channel` | Channel definitions | `workbench_channel.schema.json` |
| `workbench_cohesion*` | Cohesion canary and eval cases | `workbench_cohesion_canary.schema.json`, `workbench_cohesion_eval.schema.json` |
| `workbench_command*` | Command safety metadata | `workbench_command_safety.schema.json` |
| `workbench_competitive*` | Competitive drift snapshots | `workbench_competitive_drift.schema.json` |
| `workbench_context*` | Context assets and enrichment payloads | `workbench_context_asset.schema.json`, `workbench_context_enrichment.schema.json` |
| `workbench_conversation` | Conversation export/search records | `workbench_conversation.schema.json` |
| `workbench_correction` | Correction-loop records | `workbench_correction.schema.json` |
| `workbench_cost*` | Cost planning records | `workbench_cost_plan.schema.json` |
| `workbench_creative*` | Creative-world records | `workbench_creative_world.schema.json` |
| `workbench_data*` | Data quality records | `workbench_data_quality.schema.json` |
| `workbench_diagnosis` | Operator diagnosis payloads | `workbench_diagnosis.schema.json` |
| `workbench_domain*` | Domain review records | `workbench_domain_review.schema.json` |
| `workbench_effective*` | Effective config snapshots | `workbench_effective_config.schema.json` |
| `workbench_event` | Workbench event spine records | `workbench_event.schema.json` |
| `workbench_evidence*` | Evidence budget and proof accounting | `workbench_evidence_budget.schema.json` |
| `workbench_experiment` | Experiment records | `workbench_experiment.schema.json` |
| `workbench_extension` | Extension records | `workbench_extension.schema.json` |
| `workbench_feature*` | Feature store metadata | `workbench_feature_store.schema.json` |
| `workbench_governance*` | Governance mode records | `workbench_governance_mode.schema.json` |
| `workbench_habit*` | Habit-health payloads | `workbench_habit_health.schema.json` |
| `workbench_hardware*` | Hardware digital twin records | `workbench_hardware_digital_twin.schema.json` |
| `workbench_improvement*` | Improvement engine and proposal records | `workbench_improvement_engine.schema.json`, `workbench_improvement_proposal.schema.json` |
| `workbench_influence` | Influence/provenance records | `workbench_influence.schema.json` |
| `workbench_knowledge*` | Knowledge backfeed, coverage, and vault payloads | `workbench_knowledge_backfeed.schema.json`, `workbench_knowledge_coverage.schema.json`, `workbench_knowledge_vault.schema.json` |
| `workbench_launcher` | Launcher configuration | `workbench_launcher.schema.json` |
| `workbench_loop*` | Loop cost watcher records | `workbench_loop_cost_watcher.schema.json` |
| `workbench_managed*` | Managed agent records | `workbench_managed_agent.schema.json` |
| `workbench_media*` | Media asset metadata | `workbench_media_asset.schema.json` |
| `workbench_memory*` | Memory governance, lineage, recall, and scope records | `workbench_memory_governance.schema.json`, `workbench_memory_lineage.schema.json`, `workbench_memory_recall.schema.json`, `workbench_memory_scope.schema.json` |
| `workbench_migration` | Migration workflow records | `workbench_migration.schema.json` |
| `workbench_mode*` | Mode template records | `workbench_mode_template.schema.json` |
| `workbench_model*` | Model choice, foundry, and registry records | `workbench_model_choice.schema.json`, `workbench_model_foundry.schema.json`, `workbench_model_registry.schema.json` |
| `workbench_monitoring*` | Monitoring signal records | `workbench_monitoring_signal.schema.json` |
| `workbench_network*` | Network transport policy | `workbench_network_transport.schema.json` |
| `workbench_outcome*` | Outcome records | `workbench_outcome_record.schema.json` |
| `workbench_policy*` | Policy verdict records | `workbench_policy_verdict.schema.json` |
| `workbench_preference*` | Preference cards | `workbench_preference_card.schema.json` |
| `workbench_promotion*` | Promotion recipe records | `workbench_promotion_recipe.schema.json` |
| `workbench_query` | Query records | `workbench_query.schema.json` |
| `workbench_readiness` | Readiness gate records | `workbench_readiness.schema.json` |
| `workbench_redteam` | Red-team case records | `workbench_redteam.schema.json` |
| `workbench_remote*` | Remote intent records | `workbench_remote_intent.schema.json` |
| `workbench_resource*` | Resource lease records | `workbench_resource_lease.schema.json` |
| `workbench_risk*` | Risk context records | `workbench_risk_context.schema.json` |
| `workbench_run*` | Run kernel records | `workbench_run_kernel.schema.json` |
| `workbench_semantic*` | Semantic layer records | `workbench_semantic_layer.schema.json` |
| `workbench_sensitive*` | Sensitive workflow records | `workbench_sensitive_workflow.schema.json` |
| `workbench_shadow*` | Shadow snapshot records | `workbench_shadow_snapshot.schema.json` |
| `workbench_shell` | Shell records | `workbench_shell.schema.json` |
| `workbench_shield*` | Shield pack records | `workbench_shield_pack.schema.json` |
| `workbench_simulation` | Simulation records | `workbench_simulation.schema.json` |
| `workbench_source*` | Source card and source health records | `workbench_source_card.schema.json`, `workbench_source_health.schema.json` |
| `workbench_specialist*` | Specialist model records | `workbench_specialist_model.schema.json` |
| `workbench_spine` | Metadata spine records | `workbench_spine.schema.json` |
| `workbench_status` | Status surface payloads | `workbench_status.schema.json` |
| `workbench_surface*` | Surface maturity records | `workbench_surface_maturity.schema.json` |
| `workbench_sweep` | Sweep records | `workbench_sweep.schema.json` |
| `workbench_tiny*` | Tiny scratch trainer records | `workbench_tiny_scratch_trainer.schema.json` |
| `workbench_tool*` | Tool guide, output squasher, and surface pin records | `workbench_tool_guide.schema.json`, `workbench_tool_output_squasher.schema.json`, `workbench_tool_surface_pin.schema.json` |
| `workbench_trace*` | Trace eval case records | `workbench_trace_eval_case.schema.json` |
| `workbench_training*` | Training recipe records | `workbench_training_recipe.schema.json` |
| `workbench_tuning*` | Tuning data source records | `workbench_tuning_data_source.schema.json` |
| `workbench_update` | Update records | `workbench_update.schema.json` |
| `workbench_user*` | User personalization and signal records | `workbench_user_personalization.schema.json`, `workbench_user_signal.schema.json` |
| `workbench_why` | Explanation records | `workbench_why.schema.json` |
| `workbench_work*` | Work graph and workflow builder records | `workbench_work_graph.schema.json`, `workbench_workflow_builder.schema.json` |

## Change Requirements

Every schema change must name the consuming subsystem in review evidence,
include the on-disk `.json` path, and identify the validator or test that
accepts a known-good payload and rejects at least one known-bad payload. When a
schema is intentionally extensible, document the exact extension field instead
of removing `additionalProperties: false` at the root.
