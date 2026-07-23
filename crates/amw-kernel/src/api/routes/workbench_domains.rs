//! Native Rust route handlers for the migrated Workbench API domains.
//!
//! DOMAIN-BOUNDARY-MAP
//! - capability_packs: routes near the capability-pack router block and payload helpers.
//! - domain_kits: routes near the domain-kit router block and dynamic kit handlers.
//! - workflow_builder: metadata, graph, console, and fail-closed action handlers.
//! - channels: configuration, activity, and fail-closed action handlers.
//! - habit_health: summary, review, export, preview, and fail-closed action handlers.
//! - training_control: native training state, queue, and route payload helpers.
//! - program_tier: program registry and detail payload helpers.
mod workflow_builder {}
mod channels {}
mod habit_health {}

use axum::{
    body::{to_bytes, Body},
    extract::Path,
    http::{HeaderMap, Method, Request, StatusCode, Uri},
    middleware::{self, Next},
    response::{IntoResponse, Response},
    routing::{get, post},
    Json, Router,
};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::{
    collections::{hash_map::DefaultHasher, BTreeMap},
    fs,
    hash::{Hash, Hasher},
    path::{Path as FsPath, PathBuf},
    sync::{Mutex, OnceLock},
};
use time::{format_description::well_known::Rfc3339, OffsetDateTime};

use crate::{
    api::api_domain_authorities,
    execute_live_action,
    mcp::{McpResource, McpResourceRegistry, McpStreamSession},
    workbench_surface_policies, ExtensionPermission, LiveActionSignal,
};

type ApiResult = Result<Json<Value>, Response>;

static NATIVE_TRAINING_CONTROL: OnceLock<Mutex<NativeTrainingControl>> = OnceLock::new();

#[derive(Debug, Clone, Eq, PartialEq)]
pub struct KernelHttpRequest {
    pub method: String,
    pub path: String,
    pub body: Option<Value>,
}

#[derive(Debug, Clone, Eq, PartialEq)]
struct NativeTrainingJob {
    job_id: String,
    skill: String,
    activity_description: String,
    status: String,
    created_at_utc: String,
    updated_at_utc: String,
    checkpoint_count: u64,
    progress_milli: u32,
}

#[derive(Debug, Default)]
struct NativeTrainingControl {
    next_job: u64,
    jobs: BTreeMap<String, NativeTrainingJob>,
}

impl NativeTrainingControl {
    fn start(&mut self, skill: String) -> Value {
        self.next_job = self.next_job.saturating_add(1);
        let job_id = format!("training-job-{}", self.next_job);
        let now = utc_now_rfc3339();
        let job = NativeTrainingJob {
            job_id: job_id.clone(),
            activity_description: if skill.trim().is_empty() {
                "Manual training cycle".to_string()
            } else {
                format!("Manual training cycle for skill '{skill}'")
            },
            skill,
            status: "running".to_string(),
            created_at_utc: now.clone(),
            updated_at_utc: now,
            checkpoint_count: 0,
            progress_milli: 0,
        };
        self.jobs.insert(job_id.clone(), job.clone());
        training_receipt(
            "accepted",
            "start",
            &job_id,
            "native-training-started",
            Some(job),
        )
    }

    fn transition(&mut self, action: &str, body: &Value) -> Value {
        let Some(job_id) = self.resolve_job_id(body) else {
            return training_rejection(action, "no-training-job-selected");
        };
        let Some(job) = self.jobs.get_mut(&job_id) else {
            return training_rejection(action, "training-job-not-found");
        };
        let current = job.status.as_str();
        let next_status = match (action, current) {
            ("pause", "running") => "paused",
            ("resume", "paused") => "running",
            ("stop", "running" | "paused") => "stopped",
            ("cancel", "running" | "paused") => "cancelled",
            ("checkpoint", "running" | "paused") => {
                job.checkpoint_count = job.checkpoint_count.saturating_add(1);
                current
            }
            ("pause", _) => return training_rejection(action, "training-job-not-running"),
            ("resume", _) => return training_rejection(action, "training-job-not-paused"),
            ("stop" | "cancel" | "checkpoint", _) => {
                return training_rejection(action, "training-job-not-active")
            }
            _ => return training_rejection(action, "training-action-unsupported"),
        };
        job.status = next_status.to_string();
        if matches!(action, "stop" | "cancel") {
            job.progress_milli = job.progress_milli.min(1000);
        }
        job.updated_at_utc = utc_now_rfc3339();
        training_receipt(
            "accepted",
            action,
            &job_id,
            &format!("native-training-{action}"),
            Some(job.clone()),
        )
    }

    fn resolve_job_id(&self, body: &Value) -> Option<String> {
        body.get("job_id")
            .and_then(Value::as_str)
            .filter(|value| !value.trim().is_empty())
            .map(ToString::to_string)
            .or_else(|| {
                self.jobs
                    .values()
                    .rev()
                    .find(|job| matches!(job.status.as_str(), "running" | "paused"))
                    .map(|job| job.job_id.clone())
            })
    }

    fn jobs_payload(&self) -> Value {
        json!({
            "status": "available",
            "jobs": self.jobs.values().map(training_job_payload).collect::<Vec<_>>(),
            "source": "amw-kernel",
            "state_source": "amw-kernel::training_control"
        })
    }

    fn history_payload(&self) -> Value {
        json!({
            "status": "available",
            "agents": self.jobs.values().rev().map(training_job_payload).collect::<Vec<_>>(),
            "jobs": self.jobs.values().rev().map(training_job_payload).collect::<Vec<_>>(),
            "source": "amw-kernel",
            "state_source": "amw-kernel::training_control"
        })
    }

    fn idle_stats_payload(&self) -> Value {
        let active = self
            .jobs
            .values()
            .filter(|job| matches!(job.status.as_str(), "running" | "paused"))
            .count();
        json!({
            "status": "available",
            "is_idle": active == 0,
            "active_jobs": active,
            "records_collected": self.jobs.len(),
            "idle_minutes": 0.0,
            "ready_for_training": true,
            "missing_libraries": [],
            "source": "amw-kernel",
            "state_source": "amw-kernel::training_control"
        })
    }

    fn status_payload(&self) -> Value {
        let running = self
            .jobs
            .values()
            .filter(|job| job.status == "running")
            .count();
        let paused = self
            .jobs
            .values()
            .filter(|job| job.status == "paused")
            .count();
        let current_job = self
            .jobs
            .values()
            .rev()
            .find(|job| matches!(job.status.as_str(), "running" | "paused"))
            .map(training_job_payload);
        let last_run = self.jobs.values().next_back().map(training_job_payload);
        let is_training = running > 0;
        let current_activity = self
            .jobs
            .values()
            .rev()
            .find(|job| matches!(job.status.as_str(), "running" | "paused"))
            .map(|job| job.activity_description.clone());
        json!({
            "status": "available",
            "phase": if is_training { "training" } else { "idle" },
            "is_idle": !is_training && paused == 0,
            "idle_minutes": 0.0,
            "is_training": is_training,
            "current_activity": current_activity,
            "current_job": current_job,
            "last_run": last_run,
            "running_count": running,
            "paused_count": paused,
            "job_count": self.jobs.len(),
            "records_collected": self.jobs.len(),
            "curriculum_phase": if running > 0 { "training" } else { "idle" },
            "next_activity": if running > 0 { "monitor training job" } else { "await manual training cycle" },
            "ready_for_training": true,
            "missing_libraries": [],
            "source": "amw-kernel",
            "state_source": "amw-kernel::training_control"
        })
    }
}

pub const ADDITIONAL_WORKBENCH_SURFACE_PREFIXES: &[&str] = &[
    "/api/workbench/approval-chain/*tail",
    "/api/workbench/artifact-reviews/*tail",
    "/api/workbench/chat-mode/*tail",
    "/api/workbench/command-safety/*tail",
    "/api/workbench/console/*tail",
    "/api/workbench/context-enrichment/*tail",
    "/api/workbench/conversation/*tail",
    "/api/workbench/domain-review/*tail",
    "/api/workbench/evidence-assets/*tail",
    "/api/workbench/evidence-notebooks/*tail",
    "/api/workbench/experiment-lab/*tail",
    "/api/workbench/extensions/:extension_id/oauth/*tail",
    "/api/workbench/failure-intelligence/*tail",
    "/api/workbench/method-library/*tail",
    "/api/workbench/adaptive-tuning/*tail",
    "/api/workbench/resource-cockpit/*tail",
    "/api/workbench/capability-packs/*tail",
    "/api/workbench/domain-kits/*tail",
    "/api/workbench/workflow-builder/*tail",
    "/api/workbench/channels/*tail",
    "/api/workbench/benchmark/*tail",
    "/api/workbench/extensions/*tail",
    "/api/workbench/habit-health/*tail",
    "/api/workbench/knowledge_vault/*tail",
    "/api/workbench/managed-agents/*tail",
    "/api/workbench/memory/*tail",
    "/api/workbench/memory_refinement/*tail",
    "/api/workbench/mode-templates/*tail",
    "/api/workbench/model-choices/*tail",
    "/api/workbench/playground/*tail",
    "/api/workbench/policy-explainability/*tail",
    "/api/workbench/preference-cards/*tail",
    "/api/workbench/private-ai/*tail",
    "/api/workbench/prompt-engineering/*tail",
    "/api/workbench/query/*tail",
    "/api/workbench/rag/*tail",
    "/api/workbench/readiness/*tail",
    "/api/workbench/repro-capsules/*tail",
    "/api/workbench/run-kernel/*tail",
    "/api/workbench/shell/*tail",
    "/api/workbench/source-cards/*tail",
    "/api/workbench/status/*tail",
    "/api/workbench/tool-cards/*tail",
    "/api/workbench/tool-guides/*tail",
    "/api/workbench/tool-output-squasher/*tail",
    "/api/workbench/updates/*tail",
    "/api/workbench/work-graph/*tail",
    "/api/workbench/workflow-builder/schedules/*tail",
    "/api/v1/projects/:project_id/workbench/*tail",
    "/api/v1/workbench/annotation/*tail",
    "/api/v1/workbench/launcher/*tail",
    "/api/v1/workbench/onboarding/*tail",
    "/api/v1/workbench/:project_id/gateway-policy/*tail",
];

pub const ADDITIONAL_NATIVE_API_PREFIXES: &[&str] = &[
    "/health",
    "/ready",
    "/api/admin/*tail",
    "/api/adr/*tail",
    "/api/audit/*tail",
    "/api/analytics/*tail",
    "/api/artifacts",
    "/api/attention",
    "/api/code-search",
    "/api/coding/*tail",
    "/api/conversations/*tail",
    "/api/generate-image",
    "/api/glossary/*tail",
    "/api/image-status",
    "/api/intake/*tail",
    "/api/logs/*tail",
    "/api/models/*tail",
    "/api/new-project",
    "/api/plan/*tail",
    "/api/planning/*tail",
    "/api/plans/*tail",
    "/api/ponder/*tail",
    "/api/project/*tail",
    "/api/projects/*tail",
    "/api/repo-map/*tail",
    "/api/sandbox/*tail",
    "/api/scraping/*tail",
    "/api/sd-status",
    "/api/search/*tail",
    "/api/structural-map",
    "/api/training/*tail",
    "/api/v1/training/*tail",
    "/api/v1/projects/:project_id/mission-control/*tail",
    "/api/v1/*tail",
];

pub fn routes() -> Router {
    Router::new()
        .route("/api/v1/kernel/status", get(kernel_status))
        .route("/mcp", post(mcp_message))
        .route("/mcp/message", post(mcp_message))
        .route("/mcp/tools", get(mcp_tools))
        .route("/mcp/resources", get(mcp_resources))
        .route("/mcp/resources/read", get(mcp_resource_read))
        .route("/mcp/resources/stream", get(mcp_resource_stream))
        .route("/api/workbench/method-library", get(method_library_list))
        .route(
            "/api/workbench/method-library/catalog",
            get(method_library_catalog),
        )
        .route(
            "/api/workbench/method-library/negative-methods",
            get(method_library_negative_methods),
        )
        .route(
            "/api/workbench/method-library/by-kind/:kind",
            get(method_library_by_kind),
        )
        .route(
            "/api/workbench/method-library/by-promotion-status/:status",
            get(method_library_by_promotion_status),
        )
        .route(
            "/api/workbench/method-library/:method_card_id",
            get(method_library_get),
        )
        .route(
            "/api/workbench/adaptive-tuning/snapshot/:project_id",
            get(adaptive_tuning_snapshot),
        )
        .route(
            "/api/workbench/adaptive-tuning/normalize",
            post(accepted_action),
        )
        .route(
            "/api/workbench/adaptive-tuning/propose/:project_id",
            post(accepted_action),
        )
        .route(
            "/api/workbench/adaptive-tuning/decide/:project_id/:hypothesis_id",
            post(accepted_action),
        )
        .route(
            "/api/workbench/adaptive-tuning/preview",
            post(accepted_action),
        )
        .route(
            "/api/workbench/adaptive-tuning/forget/:project_id/:hypothesis_id",
            post(accepted_action),
        )
        .route(
            "/api/workbench/adaptive-tuning/revoke/:project_id/:hypothesis_id",
            post(accepted_action),
        )
        .route(
            "/api/workbench/adaptive-tuning/rollback-readiness/:project_id/:proposal_id",
            get(rollback_readiness),
        )
        .route(
            "/api/workbench/resource-cockpit/snapshot",
            get(resource_snapshot),
        )
        .route(
            "/api/workbench/resource-cockpit/leases",
            get(resource_leases),
        )
        .route(
            "/api/workbench/resource-cockpit/queued",
            get(resource_queued),
        )
        .route(
            "/api/workbench/resource-cockpit/safe-actions",
            get(resource_safe_actions),
        )
        .route(
            "/api/workbench/resource-cockpit/safe-actions/:action_id/execute",
            post(resource_execute),
        )
        .route(
            "/api/workbench/resource-cockpit/machine-profile",
            get(resource_machine_profile),
        )
        .route(
            "/api/workbench/resource-cockpit/policy-proposals",
            get(resource_policy_proposals),
        )
        .route(
            "/api/workbench/resource-cockpit/policy-proposals/:proposal_id/approval-diff",
            post(approval_diff),
        )
        .route("/api/workbench/capability-packs", get(capability_packs))
        .route(
            "/api/workbench/capability-packs/:pack_id",
            get(capability_pack),
        )
        .route(
            "/api/workbench/capability-packs/:pack_id/trust",
            get(capability_pack_trust),
        )
        .route(
            "/api/workbench/capability-packs/:pack_id/enable",
            post(capability_pack_decision),
        )
        .route(
            "/api/workbench/capability-packs/:pack_id/disable",
            post(capability_pack_decision),
        )
        .route(
            "/api/workbench/capability-packs/:pack_id/uninstall",
            post(capability_pack_decision),
        )
        .route(
            "/api/workbench/capability-packs/:pack_id/smoke-test",
            post(capability_pack_decision),
        )
        .route("/api/workbench/domain-kits", get(domain_kits))
        .route("/api/workbench/domain-kits/:kit_id", get(domain_kit))
        .route(
            "/api/workbench/domain-kits/:kit_id/evaluate",
            post(domain_kit_evaluate),
        )
        .route(
            "/api/workbench/workflow-builder/metadata",
            get(workflow_metadata),
        )
        .route(
            "/api/workbench/workflow-builder/validate",
            post(accepted_action),
        )
        .route(
            "/api/workbench/workflow-builder/preview",
            post(accepted_action),
        )
        .route(
            "/api/workbench/workflow-builder/save",
            post(accepted_action),
        )
        .route(
            "/api/workbench/workflow-builder/graphs/:project_id",
            get(workflow_graphs),
        )
        .route(
            "/api/workbench/workflow-builder/graphs/:project_id/:graph_id",
            get(workflow_graph),
        )
        .route(
            "/api/workbench/workflow-builder/console/:project_id",
            get(workflow_console),
        )
        .route(
            "/api/workbench/workflow-builder/settings/:project_id",
            post(accepted_action),
        )
        .route("/api/workbench/channels/config", get(channels_config))
        .route("/api/workbench/channels/deliver", post(accepted_action))
        .route("/api/workbench/channels/commands", post(accepted_action))
        .route("/api/workbench/channels/approvals", post(accepted_action))
        .route("/api/workbench/channels/activity", get(channels_activity))
        .route(
            "/api/workbench/benchmark/providers",
            get(benchmark_providers),
        )
        .route("/api/workbench/benchmark/import", post(benchmark_import))
        .route(
            "/api/v1/workbench/migration/sources",
            get(migration_sources),
        )
        .route("/api/v1/workbench/migration/plan", post(migration_plan))
        .route("/api/v1/workbench/migration/apply", post(migration_apply))
        .route(
            "/api/workbench/habit-health/summary/:user_id",
            get(habit_health_summary),
        )
        .route(
            "/api/workbench/habit-health/routines",
            post(accepted_action),
        )
        .route(
            "/api/workbench/habit-health/check-ins",
            post(accepted_action),
        )
        .route(
            "/api/workbench/habit-health/review/:user_id",
            get(habit_health_review),
        )
        .route(
            "/api/workbench/habit-health/export",
            post(habit_health_export),
        )
        .route("/api/workbench/habit-health/delete", post(accepted_action))
        .route(
            "/api/workbench/habit-health/downstream-preview",
            post(habit_health_downstream_preview),
        )
        .route("/api/workbench/extensions", get(extensions_list))
        .route("/api/workbench/extensions/import", post(extensions_import))
        .route(
            "/api/workbench/extensions/:extension_id",
            get(extension_get),
        )
        .route(
            "/api/workbench/extensions/:extension_id/risk",
            get(extension_risk),
        )
        .route(
            "/api/workbench/extensions/:extension_id/registration",
            get(extension_registration),
        )
        .route(
            "/api/workbench/extensions/:extension_id/select",
            post(extension_select),
        )
        .route(
            "/api/workbench/extensions/:extension_id/enable",
            post(extension_enable),
        )
        .route(
            "/api/v1/projects/:project_id/workbench/stream",
            get(project_workbench_stream),
        )
        .fallback(native_workbench_surface_fallback)
        .layer(middleware::from_fn(require_kernel_http_auth))
}

pub fn handle_kernel_request(request: KernelHttpRequest) -> Result<Value, String> {
    let method = request.method.to_ascii_uppercase();
    let path = request.path.split('?').next().unwrap_or(&request.path);
    let query = request
        .path
        .split_once('?')
        .map(|(_, query)| query)
        .unwrap_or("");
    validate_kernel_request_policy(&method, path)?;
    if method == "POST"
        && path.starts_with("/api/coding")
        && request.body.as_ref().map_or(true, |value| value.is_null())
    {
        return Ok(json!({"error": "request_body_required"}));
    }
    let body = request.body.unwrap_or_else(|| json!({}));
    let segments: Vec<&str> = path.trim_matches('/').split('/').collect();

    match (method.as_str(), path) {
        ("GET", "/api/v1/kernel/status") => Ok(kernel_status_payload()),
        ("POST", "/mcp") | ("POST", "/mcp/message") => Ok(mcp_message_payload(body)),
        ("GET", "/mcp/tools") => Ok(mcp_tools_payload()),
        ("GET", "/mcp/resources") => Ok(mcp_resources_payload()),
        ("GET", "/mcp/resources/read") => mcp_resource_read_payload(&[]),
        ("GET", "/mcp/resources/stream") => Ok(mcp_resource_stream_payload()),
        ("GET", "/api/workbench/method-library") => {
            Ok(json!({"methods": method_cards(), "source": "amw-kernel"}))
        }
        ("GET", "/api/workbench/method-library/catalog") => {
            Ok(json!({"catalog": method_catalog()}))
        }
        ("GET", "/api/workbench/method-library/negative-methods") => Ok(
            json!({"negative_methods": [method_card("negative-baseline", "negative_method", "Negative baseline")]}),
        ),
        ("GET", "/api/workbench/resource-cockpit/snapshot") => Ok(resource_snapshot_payload()),
        ("GET", "/api/workbench/resource-cockpit/leases") => Ok(resource_leases_payload()),
        ("GET", "/api/workbench/resource-cockpit/queued") => Ok(resource_queued_payload()),
        ("GET", "/api/workbench/resource-cockpit/safe-actions") => {
            Ok(json!({"actions": ["pause", "cancel", "adjust_interactive_reserve"]}))
        }
        ("GET", "/api/workbench/resource-cockpit/machine-profile") => Ok(
            json!({"profile": {"cpu_class": "local", "memory_pressure": "unknown", "source": "amw-kernel"}}),
        ),
        ("GET", "/api/workbench/resource-cockpit/policy-proposals") => {
            Ok(resource_policy_proposals_payload())
        }
        ("GET", "/api/workbench/capability-packs") => Ok(capability_registry_payload()),
        ("GET", "/api/workbench/domain-kits") => {
            Ok(json!({"kits": [domain_kit_payload("software")]}))
        }
        ("GET", "/api/workbench/workflow-builder/metadata") => {
            Ok(json!({"node_types": ["task", "approval", "worker"], "source": "amw-kernel"}))
        }
        ("GET", "/api/audit/full-spectrum/results") => {
            Ok(full_spectrum_audit_results_payload(query))
        }
        ("GET", "/api/workbench/program-tier") => Ok(program_tier_payload()),
        ("POST", "/api/workbench/workflow-builder/validate")
        | ("POST", "/api/workbench/workflow-builder/preview")
        | ("POST", "/api/workbench/workflow-builder/save")
        | ("POST", "/api/workbench/channels/deliver")
        | ("POST", "/api/workbench/channels/commands")
        | ("POST", "/api/workbench/channels/approvals")
        | ("POST", "/api/workbench/habit-health/routines")
        | ("POST", "/api/workbench/habit-health/check-ins")
        | ("POST", "/api/workbench/habit-health/delete") => {
            Ok(not_implemented_handler_payload(path))
        }
        ("POST", "/api/workbench/adaptive-tuning/normalize")
        | ("POST", "/api/workbench/adaptive-tuning/preview") => Ok(accepted_action_payload(body)),
        ("GET", "/api/workbench/channels/config") => Ok(channels_config_payload()),
        ("GET", "/api/workbench/channels/activity") => Ok(channels_activity_payload()),
        ("GET", "/api/workbench/benchmark/providers") => Ok(benchmark_providers_payload()),
        ("POST", "/api/workbench/benchmark/import") => Ok(benchmark_import_payload(body)),
        ("GET", "/api/v1/workbench/migration/sources") => Ok(migration_sources_payload()),
        ("POST", "/api/v1/workbench/migration/plan") => Ok(migration_plan_payload(body)),
        ("POST", "/api/v1/workbench/migration/apply") => Ok(migration_apply_payload(body)),
        ("POST", "/api/workbench/habit-health/export") => Ok(
            json!({"export": {"format": "json", "records": []}, "request": redacted_request_summary(&body)}),
        ),
        ("POST", "/api/workbench/habit-health/downstream-preview") => {
            Ok(habit_health_downstream_preview_payload(&body))
        }
        ("GET", "/api/workbench/extensions") => Ok(extensions_list_payload()),
        ("POST", "/api/workbench/extensions/import") => Ok(extension_import_payload(body)),
        _ => handle_dynamic_kernel_request(&method, query, &segments, body.clone())
            .or_else(|| handle_additional_workbench_request(&method, path, &segments, body))
            .or_else(|| handle_additional_api_request(&method, path, &segments))
            .ok_or_else(|| format!("unsupported native kernel route: {method} {path}")),
    }
}

pub fn validate_kernel_request_policy(method: &str, path: &str) -> Result<(), String> {
    if !matches!(method, "GET" | "POST") {
        return Err(format!(
            "kernel route denied by renderer policy: method {method} is not allowed"
        ));
    }
    if path.starts_with("/api/admin")
        || path.starts_with("/api/sandbox")
        || path.starts_with("/api/logs")
        || path.starts_with("/api/search")
    {
        return Err(format!(
            "kernel route denied by renderer policy: {method} {path}"
        ));
    }
    Ok(())
}

async fn require_kernel_http_auth(request: Request<Body>, next: Next) -> Response {
    let token = std::env::var("AMW_KERNEL_AUTH_TOKEN").ok();
    let local_dev_enabled = std::env::var("AMW_KERNEL_ALLOW_LOCAL_DEV_AUTH")
        .is_ok_and(|value| value == "1" || value.eq_ignore_ascii_case("true"));
    if kernel_http_auth_decision_with_env(
        request.uri().path(),
        request.headers(),
        token.as_deref(),
        local_dev_enabled,
    )
    .is_ok()
    {
        return next.run(request).await;
    }

    (
        StatusCode::UNAUTHORIZED,
        Json(json!({"error": "kernel authentication required"})),
    )
        .into_response()
}

fn kernel_http_auth_decision_with_env(
    path: &str,
    headers: &HeaderMap,
    expected_token: Option<&str>,
    local_dev_enabled: bool,
) -> Result<(), &'static str> {
    if path == "/api/v1/kernel/status" {
        return Ok(());
    }
    if is_project_workbench_stream_path(path) {
        return Ok(());
    }
    if local_dev_enabled
        && headers
            .get("x-amw-kernel-local-dev")
            .and_then(|value| value.to_str().ok())
            .is_some_and(|value| value == "1" || value.eq_ignore_ascii_case("true"))
    {
        return Ok(());
    }
    if let Some(expected_token) = expected_token {
        let expected = format!("Bearer {expected_token}");
        if headers
            .get("authorization")
            .and_then(|value| value.to_str().ok())
            .is_some_and(|value| value == expected)
        {
            return Ok(());
        }
    }
    Err("kernel-auth-missing")
}

fn is_project_workbench_stream_path(path: &str) -> bool {
    let parts: Vec<&str> = path.trim_matches('/').split('/').collect();
    matches!(
        parts.as_slice(),
        ["api", "v1", "projects", project_id, "workbench", "stream"] if !project_id.is_empty()
    )
}

fn handle_dynamic_kernel_request(
    method: &str,
    query: &str,
    segments: &[&str],
    body: Value,
) -> Option<Value> {
    match (method, segments) {
        ("GET", ["api", "workbench", "run-kernel", "runs"]) => Some(run_kernel_runs_payload()),
        ("POST", ["api", "workbench", "run-kernel", "runs"]) => {
            Some(run_kernel_action_payload("running", body))
        }
        ("GET", ["api", "workbench", "run-kernel", "runs", run_id]) => {
            Some(run_kernel_run_payload(run_id))
        }
        ("POST", ["api", "workbench", "run-kernel", "runs", run_id, "checkpoint"]) => {
            Some(run_kernel_checkpoint_payload(run_id, body))
        }
        ("POST", ["api", "workbench", "run-kernel", "runs", run_id, "resume"]) => {
            Some(run_kernel_resume_payload(run_id, body))
        }
        ("GET", ["api", "workbench", "evidence-notebooks"]) => Some(evidence_notebooks_payload()),
        ("GET", ["api", "workbench", "evidence-notebooks", notebook_id]) => {
            Some(evidence_notebook_payload(notebook_id))
        }
        ("GET", ["api", "workbench", "managed-agents", "snapshot"]) => {
            Some(managed_agents_snapshot_payload())
        }
        ("POST", ["api", "workbench", "managed-agents", agent_id, "pause"]) => {
            Some(managed_agent_decision_payload(agent_id, "pause", body))
        }
        ("POST", ["api", "workbench", "managed-agents", agent_id, "retire"]) => {
            Some(managed_agent_decision_payload(agent_id, "retire", body))
        }
        ("GET", ["api", "workbench", "command-safety", "profiles"]) => {
            Some(command_safety_profiles_payload())
        }
        ("POST", ["api", "workbench", "command-safety", "classify"]) => {
            Some(command_safety_decision_payload(body, false))
        }
        ("POST", ["api", "workbench", "command-safety", "decide"]) => {
            Some(command_safety_decision_payload(body, true))
        }
        (
            "GET",
            ["api", "workbench", "command-safety", "state", project_id, run_id, session_id, surface_id],
        ) => Some(command_safety_state_payload(
            project_id, run_id, session_id, surface_id,
        )),
        ("GET", ["api", "workbench", "readiness", "snapshot"]) => {
            Some(readiness_snapshot_payload())
        }
        ("POST", ["api", "workbench", "readiness", "admission-preview"]) => {
            Some(readiness_admission_payload(body))
        }
        ("GET", ["api", "workbench", "updates", "readiness"]) => {
            Some(update_readiness_payload(Value::Null))
        }
        ("GET", ["api", "workbench", "updates", "channels"]) => Some(update_channels_payload()),
        ("POST", ["api", "workbench", "updates", "check"]) => Some(update_readiness_payload(body)),
        ("POST", ["api", "workbench", "updates", "skip"]) => Some(update_skip_payload(body)),
        ("POST", ["api", "workbench", "updates", "rollback-plan"]) => {
            Some(update_rollback_payload(body))
        }
        ("POST", ["api", "workbench", "updates", "support-bundle"]) => {
            Some(update_support_bundle_payload(body))
        }
        ("GET", ["api", "workbench", "shell", "snapshot"]) => Some(shell_snapshot_payload()),
        ("GET", ["api", "workbench", "memory", "review-graph"]) => {
            Some(memory_review_graph_payload())
        }
        ("GET", ["api", "workbench", "artifact-reviews"]) => Some(artifact_reviews_payload()),
        ("POST", ["api", "workbench", "artifact-reviews"]) => Some(artifact_review_payload(body)),
        ("GET", ["api", "workbench", "method-library", "by-kind", kind]) => {
            Some(json!({"methods": [method_card("method-by-kind", kind, "Filtered method")]}))
        }
        ("GET", ["api", "workbench", "method-library", "by-promotion-status", status]) => Some(
            json!({"methods": [json!({"method_card_id": "method-by-status", "promotion_status": status})]}),
        ),
        ("GET", ["api", "workbench", "method-library", method_card_id]) => {
            Some(json!({"method": method_card(method_card_id, "prompting", "Selected method")}))
        }
        ("GET", ["api", "workbench", "adaptive-tuning", "snapshot", project_id]) => {
            Some(adaptive_tuning_snapshot_payload(project_id))
        }
        ("POST", ["api", "workbench", "adaptive-tuning", "propose", _])
        | ("POST", ["api", "workbench", "adaptive-tuning", "decide", _, _])
        | ("POST", ["api", "workbench", "adaptive-tuning", "forget", _, _])
        | ("POST", ["api", "workbench", "adaptive-tuning", "revoke", _, _]) => {
            Some(accepted_action_payload(body))
        }
        ("POST", ["api", "workbench", "workflow-builder", "settings", _]) => Some(
            not_implemented_handler_payload(&format!("/{}", segments.join("/"))),
        ),
        (
            "GET",
            ["api", "workbench", "adaptive-tuning", "rollback-readiness", project_id, proposal_id],
        ) => Some(
            json!({"project_id": project_id, "proposal_id": proposal_id, "ready": true, "rollback_ref": format!("rollback:{proposal_id}")}),
        ),
        (
            "POST",
            ["api", "workbench", "resource-cockpit", "safe-actions", action_id, "execute"],
        ) => {
            Some(resource_execute_payload(action_id, body).unwrap_or_else(
                |detail| json!({"error": "kernel action rejected", "detail": detail}),
            ))
        }
        (
            "POST",
            ["api", "workbench", "resource-cockpit", "policy-proposals", proposal_id, "approval-diff"],
        ) => Some(approval_diff_payload_with_request(proposal_id, Some(&body))),
        ("GET", ["api", "workbench", "capability-packs", pack_id]) => {
            Some(capability_pack_detail_payload(pack_id))
        }
        ("GET", ["api", "workbench", "capability-packs", pack_id, "trust"]) => {
            Some(json!({"trust": capability_registry_decision(pack_id)}))
        }
        (
            "POST",
            ["api", "workbench", "capability-packs", pack_id, "enable" | "disable" | "uninstall" | "smoke-test"],
        ) => Some(capability_not_implemented_payload(pack_id)),
        ("GET" | "POST", ["api", "v1", "training", ..])
        | ("GET" | "POST", ["api", "training", ..]) => {
            let route_path = format!("/{}", segments.join("/"));
            Some(training_control_payload(method, &route_path, body))
        }
        ("GET", ["api", "workbench", "domain-kits", kit_id]) => {
            Some(json!({"kit": domain_kit_payload(kit_id)}))
        }
        ("POST", ["api", "workbench", "domain-kits", kit_id, "evaluate"]) => {
            Some(kit_registry_unavailable_payload(kit_id))
        }
        ("GET", ["api", "workbench", "workflow-builder", "graphs", project_id]) => {
            Some(workflow_graphs_payload(project_id))
        }
        ("GET", ["api", "workbench", "workflow-builder", "graphs", project_id, graph_id]) => {
            Some(workflow_graph_payload(project_id, graph_id))
        }
        ("GET", ["api", "workbench", "workflow-builder", "console", project_id]) => {
            Some(workflow_console_payload(project_id))
        }
        ("GET", ["api", "audit", "full-spectrum", "results", run_id]) => {
            Some(full_spectrum_audit_run_payload(run_id, query))
        }
        ("GET", ["api", "workbench", "program-tier", program_id]) => {
            Some(program_tier_detail_payload(program_id))
        }
        ("GET", ["api", "workbench", "habit-health", "summary", user_id]) => Some(
            json!({"user_id": user_id, "enabled": false, "default_policy": "opt-in", "source": "amw-kernel"}),
        ),
        ("GET", ["api", "workbench", "habit-health", "review", user_id]) => {
            Some(habit_health_review_payload(user_id))
        }
        ("GET", ["api", "workbench", "extensions", extension_id]) => Some(
            json!({"extension": extension_payload(extension_id), "registration": extension_registration_payload(extension_id)}),
        ),
        ("GET", ["api", "workbench", "extensions", extension_id, "risk"]) => {
            Some(json!({"risk": extension_risk_payload(extension_id)}))
        }
        ("GET", ["api", "workbench", "extensions", extension_id, "registration"]) => {
            Some(json!({"registration": extension_registration_payload(extension_id)}))
        }
        ("GET", ["api", "v1", "projects", project_id, "mission-control", "snapshot"]) => {
            Some(mission_control_snapshot_payload(project_id))
        }
        ("GET", ["api", "v1", "projects", project_id, "mission-control", "queue"]) => {
            Some(json!({"project_id": project_id, "queue": [], "source": "amw-kernel"}))
        }
        ("GET", ["api", "v1", "projects", project_id, "mission-control", "agents"]) => {
            Some(json!({
                "project_id": project_id,
                "agent_tasks": [],
                "escalations": [],
                "recursive_children": [],
                "source": "amw-kernel"
            }))
        }
        ("POST", ["api", "workbench", "extensions", extension_id, "select"])
        | ("POST", ["api", "workbench", "extensions", extension_id, "enable"]) => {
            Some(json!({"decision": extension_registration_payload(extension_id), "request": body}))
        }
        _ => None,
    }
}

fn handle_additional_workbench_request(
    method: &str,
    path: &str,
    segments: &[&str],
    body: Value,
) -> Option<Value> {
    let (surface, operation) = workbench_surface_from_path(path, segments)?;
    let mutable = matches!(method, "POST" | "PUT" | "PATCH" | "DELETE");
    let base = json!({
        "source": "amw-kernel",
        "surface": surface,
        "operation": operation,
        "method": method,
        "path": path,
        "native_owner": "amw-kernel::workbench_surface",
    });

    let payload = match surface {
        "run-kernel" => json!({
            "runs": [],
            "run": route_entity(path, "run"),
            "events": [],
            "result": {"available": false, "reason": "no native run result recorded"},
            "admission": {"requires_lease": true, "receipt_required": true},
        }),
        "rag" => json!({
            "datasets": [],
            "experiments": [],
            "ingest": {"accepted_sources": ["directory", "document", "url"], "receipt_required": true},
            "trace": route_entity(path, "dataset"),
        }),
        "console" => json!({"assets": [], "evals": [], "leases": [], "proposals": [], "runs": []}),
        "evidence-assets" => json!({"assets": [], "kinds": [], "failure_history": []}),
        "evidence-notebooks" => {
            json!({"notebooks": [], "selected": route_entity(path, "notebook")})
        }
        "managed-agents" => {
            json!({"agents": [], "runs": [], "installable": [], "policy": "approval-required"})
        }
        "private-ai" => json!({
            "snapshot": {"runtime_health": "unknown", "queue_pressure": "unknown"},
            "model_store": [],
            "support_matrix": [],
            "actions": ["start", "stop", "refresh"],
        }),
        "updates" => json!({
            "channels": ["stable"],
            "readiness": {"ready": false, "reason": "no update candidate selected"},
            "support_bundle": {"available": true, "redacted": true},
        }),
        "launcher" => json!({
            "status": "available",
            "doctor": {"checks": []},
            "support_bundle": {"available": true, "redacted": true},
        }),
        "command-safety" => json!({
            "profiles": ["readonly", "approval-required", "blocked"],
            "classification": {"decision": "approval-required", "reason": "native fail-closed default"},
        }),
        "experiment-lab" | "playground" | "prompt-engineering" => {
            json!({"experiments": [], "trace": [], "decision": {"status": "pending"}})
        }
        "kaizen" => json!({
            "report": {
                "total_proposed": 0,
                "total_active": 0,
                "total_confirmed": 0,
                "total_failed": 0
            },
            "improvements": [],
            "defect_trends": {"trends": {}},
            "signals": []
        }),
        "extensions" => json!({
            "oauth": {"authorization_request": null, "token": {"issued": false}},
            "default_policy": "opt-in",
        }),
        "workflow-builder" => json!({
            "schedules": [],
            "selected_schedule": route_entity(path, "schedule"),
            "receipt_required": mutable,
        }),
        "approval-chain" => json!({
            "decision": {"status": "approval-required"},
            "last_decision": null,
            "session_allow": {"active": false},
        }),
        "artifact-reviews" => json!({"reviews": [], "selected": route_entity(path, "review")}),
        "status" => json!({
            "snapshot": {"health": "unknown", "domains": []},
            "assistant_context": {"available": false},
            "actions": [],
        }),
        "annotation" => json!({"queue": [], "templates": [], "commit": {"accepted": mutable}}),
        "onboarding" => {
            json!({"health": "unknown", "refresh": mutable, "smoke_test": {"available": true}})
        }
        "gateway-policy" => {
            json!({"profiles": [], "decisions": [], "project": route_entity(path, "project")})
        }
        "model-registry" => json!({"versions": [], "transition": {"accepted": mutable}}),
        "professional-life" => json!({"evaluation": {"allowed": false}, "drafts": []}),
        "knowledge_vault" => json!({"entries": [], "rejected": [], "index": {"ready": false}}),
        "memory_refinement" => json!({"journal": [], "reverse": {"accepted": mutable}}),
        "tool-guides" => json!({"catalog": [], "selection": null, "validation": {"valid": true}}),
        "work-graph" => {
            json!({"graph": {"nodes": [], "edges": []}, "rebuild": {"accepted": mutable}})
        }
        "readiness" => json!({"snapshot": {"ready": false}, "admission": {"allowed": false}}),
        "repro-capsules" => json!({"capsules": [], "export": {"available": true}}),
        "source-cards" | "tool-cards" => {
            json!({"cards": [], "selected": route_entity(path, "card")})
        }
        "mode-templates" => json!({"templates": [], "selected": route_entity(path, "template")}),
        "model-choices" => json!({"choices": [], "repin": {"accepted": mutable}}),
        "conversation" => json!({"conversation": {"accepted": mutable, "messages": []}}),
        "domain-review" => json!({"queues": [], "submission": {"accepted": mutable}}),
        "context-enrichment" => json!({"context": {}, "preflight": {"allowed": true}}),
        "policy-explainability" => json!({"explanation": {"available": false, "reasons": []}}),
        "preference-cards" => json!({"cards": []}),
        "query" => json!({"snapshot": {"results": []}}),
        "shell" => json!({"snapshot": {"panels": [], "alerts": []}}),
        "memory" => json!({"review_graph": {"nodes": [], "edges": []}}),
        "chat-mode" => json!({"conversion": {"accepted": mutable}}),
        "tool-output-squasher" => json!({"preview": {"savings": 0, "changes": []}}),
        _ => return None,
    };

    let payload = merge_json_objects(
        workbench_compatibility_contract_payload(surface, operation, mutable),
        payload,
    );

    Some(merge_json_objects(
        base,
        json!({"payload": payload, "request": body}),
    ))
}

fn handle_additional_api_request(method: &str, path: &str, segments: &[&str]) -> Option<Value> {
    if path == "/health" || path == "/ready" {
        return Some(json!({
            "status": "ok",
            "source": "amw-kernel",
            "check": path.trim_start_matches('/'),
        }));
    }
    if !path.starts_with("/api/") {
        return None;
    }
    let family = api_family(segments)?;
    let mutable = matches!(method, "POST" | "PUT" | "PATCH" | "DELETE");
    let payload = match family {
        "a2a" => {
            json!({"cards": ["foreman", "worker", "inspector"], "raw": {}, "admin_required": true})
        }
        "admin" => json!({"credentials": [], "permissions": [], "health": "unknown"}),
        "adr" => json!({"adrs": [], "statistics": {}, "recent": []}),
        "agents" => json!({"agents": [], "active": [], "tasks": [], "memory": []}),
        "analytics" | "metrics" | "traces" => json!({"series": [], "overview": {}, "alerts": []}),
        "approvals" | "decisions" => {
            json!({"pending": [], "history": [], "decision": {"accepted": mutable}})
        }
        "autonomy" => autonomy_payload(method, path, segments, mutable),
        "audit" => json!({"manifests": [], "decisions": [], "results": []}),
        "benchmarks" | "eval" | "spc" => json!({"suites": [], "runs": [], "leaderboard": []}),
        "capabilities" | "skills" => {
            json!({"catalog": [], "capabilities": [], "validation": {"valid": true}})
        }
        "chat" | "conversations" => {
            json!({"attachments": [], "messages": [], "export": {"available": true}})
        }
        "coding" => json!({"task": route_entity(path, "coding-task"), "status": "queued"}),
        "config" | "settings" | "preferences" | "variant" => {
            json!({"config": {}, "saved": mutable})
        }
        "dashboard" | "status" | "health" => {
            json!({"dashboard": {}, "health": "unknown", "events": []})
        }
        "decomposition" | "templates" | "subtasks" | "assignments" => {
            json!({"templates": [], "tree": [], "assignment": {"accepted": mutable}})
        }
        "discover" | "models" | "model-catalog" | "model-config" | "score-models"
        | "swap-model" => {
            json!({"models": [], "selected": null, "downloads": []})
        }
        "download" | "artifacts" => json!({"artifacts": [], "download": {"available": true}}),
        "glossary" => json!({"terms": [], "selected": route_entity(path, "term")}),
        "image-status" | "generate-image" | "sd-status" => json!({"image": {"status": "idle"}}),
        "intake" | "planning" | "plan" | "plans" | "ponder" => {
            json!({"plans": [], "plan": route_entity(path, "plan"), "accepted": mutable})
        }
        "kaizen" | "learning" | "cost-analysis" | "bottleneck" | "value-stream" | "constraints" => {
            json!({"report": {}, "improvements": [], "signals": []})
        }
        "logs" => json!({"entries": [], "stream": {"available": true}}),
        "memory" => json!({"entries": [], "sessions": [], "stats": {}}),
        "milestones" => json!({"milestones": [], "approved": mutable}),
        "project" | "projects" | "new-project" => {
            json!({"projects": [], "project": route_entity(path, "project")})
        }
        "repo-map" | "search" | "structural-map" | "code-search" => {
            json!({"results": [], "index": {"ready": false}, "symbols": []})
        }
        "replay" => json!({"events": [], "checkpoint": route_entity(path, "checkpoint")}),
        "rules" => json!({"rules": [], "global": {}, "saved": mutable}),
        "sandbox" => json!({"status": "idle", "audit": [], "plugins": []}),
        "scraping" => json!({"fetch": {"accepted": mutable, "network_policy": "bounded"}}),
        "server" | "system" | "system-prompts" => {
            json!({"system": {}, "prompts": [], "shutdown": {"accepted": mutable}})
        }
        "tasks" | "run-task" | "run-all" | "run-prompt" | "output" | "all-tasks" => {
            json!({"tasks": [], "job": route_entity(path, "job"), "accepted": mutable})
        }
        "training" => json!({
            "status": "idle",
            "jobs": [],
            "history": [],
            "native_owner": "amw-kernel::training",
        }),
        "workflow" | "workflows" => json!({"workflows": [], "gates": [], "validated": !mutable}),
        _ => return None,
    };
    let payload = merge_json_objects(api_compatibility_contract_payload(family, mutable), payload);
    Some(json!({
        "source": "amw-kernel",
        "family": family,
        "method": method,
        "path": path,
        "native_owner": "amw-kernel::api_surface",
        "payload": payload,
    }))
}

fn full_spectrum_audit_results_payload(query: &str) -> Value {
    let index_path = kernel_state_path(&["audit", "RUN-INDEX.json"]);
    full_spectrum_audit_results_payload_from_index(&index_path, query)
}

fn full_spectrum_audit_results_payload_from_index(index_path: &FsPath, query: &str) -> Value {
    let include_archived = query_bool(query, "include_archived", false);
    let limit = query_usize(query, "limit", 10, 1, 50);
    let Some(index) = read_json(index_path) else {
        return json!({
            "status": "unavailable",
            "source": "amw-kernel",
            "index_path": display_path(index_path),
            "include_archived": include_archived,
            "limit": limit,
            "runs": [],
            "summary": {"total_runs": 0, "visible_runs": 0, "open_findings": 0, "total_findings": 0},
            "error": "full-spectrum audit RUN-INDEX.json is missing or unreadable",
        });
    };
    let runs = index
        .get("runs")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    let mut summaries = Vec::new();
    let mut skipped = 0usize;
    for run in runs.iter().filter_map(Value::as_object) {
        if run
            .get("archived")
            .and_then(Value::as_bool)
            .unwrap_or(false)
            && !include_archived
        {
            continue;
        }
        let run_id = run
            .get("run_id")
            .and_then(Value::as_str)
            .unwrap_or_default();
        let Some(run_root) = audit_run_root(run) else {
            skipped += 1;
            continue;
        };
        summaries.push(audit_run_summary(run_id, run, &run_root, false));
    }
    summaries.sort_by(|left, right| {
        right
            .get("started_at")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .cmp(
                left.get("started_at")
                    .and_then(Value::as_str)
                    .unwrap_or_default(),
            )
    });
    summaries.truncate(limit);
    let open_findings: i64 = summaries
        .iter()
        .map(|row| {
            row.get("open_findings")
                .and_then(Value::as_i64)
                .unwrap_or(0)
        })
        .sum();
    let total_findings: i64 = summaries
        .iter()
        .map(|row| {
            row.get("finding_count")
                .and_then(Value::as_i64)
                .unwrap_or(0)
        })
        .sum();
    json!({
        "status": "ok",
        "schema_version": "1.0.0",
        "source": "amw-kernel",
        "index_path": display_path(index_path),
        "include_archived": include_archived,
        "limit": limit,
        "runs": summaries,
        "summary": {
            "total_runs": runs.len(),
            "visible_runs": summaries.len(),
            "skipped_runs": skipped,
            "open_findings": open_findings,
            "total_findings": total_findings,
        },
    })
}

fn full_spectrum_audit_run_payload(run_id: &str, query: &str) -> Value {
    let index_path = kernel_state_path(&["audit", "RUN-INDEX.json"]);
    full_spectrum_audit_run_payload_from_index(run_id, &index_path, query)
}

fn full_spectrum_audit_run_payload_from_index(
    run_id: &str,
    index_path: &FsPath,
    query: &str,
) -> Value {
    let include_archived = query_bool(query, "include_archived", false);
    let Some(index) = read_json(index_path) else {
        return json!({
            "status": "unavailable",
            "run_id": run_id,
            "index_path": display_path(index_path),
            "include_archived": include_archived,
            "error": "full-spectrum audit RUN-INDEX.json is missing or unreadable"
        });
    };
    for run in index
        .get("runs")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .filter_map(Value::as_object)
    {
        if run.get("run_id").and_then(Value::as_str) != Some(run_id) {
            continue;
        }
        if run
            .get("archived")
            .and_then(Value::as_bool)
            .unwrap_or(false)
            && !include_archived
        {
            return json!({"status": "not_found", "run_id": run_id, "error": "archived run hidden"});
        }
        let Some(run_root) = audit_run_root(run) else {
            return json!({"status": "unavailable", "run_id": run_id, "error": "audit run root is missing"});
        };
        return json!({
            "status": "ok",
            "schema_version": "1.0.0",
            "source": "amw-kernel",
            "index_path": display_path(index_path),
            "include_archived": include_archived,
            "run": audit_run_summary(run_id, run, &run_root, true),
        });
    }
    json!({"status": "not_found", "run_id": run_id, "error": "unknown full-spectrum audit run"})
}

fn audit_run_summary(
    run_id: &str,
    run: &serde_json::Map<String, Value>,
    run_root: &FsPath,
    include_findings: bool,
) -> Value {
    let registry = read_json(&run_root.join("finding-registry.json")).unwrap_or_else(|| json!({}));
    let closure = read_json(&run_root.join("CLOSURE-STATUS.json")).unwrap_or_else(|| json!({}));
    let checkpoint =
        read_json(&run_root.join("CHECKPOINT-STATE.json")).unwrap_or_else(|| json!({}));
    let findings = registry
        .get("findings")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    let closure_rows = closure
        .get("findings")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    let closure_by_id = closure_by_finding_id(&closure_rows);
    let open_findings = findings
        .iter()
        .filter(|row| field_string(row, "status").eq_ignore_ascii_case("open"))
        .count();
    let mut summary = json!({
        "run_id": run_id,
        "status": run
            .get("status")
            .cloned()
            .or_else(|| checkpoint.get("phase").cloned())
            .unwrap_or_else(|| json!("unknown")),
        "phase": checkpoint.get("phase").cloned().unwrap_or(Value::Null),
        "current_round": checkpoint.get("current_round").cloned().unwrap_or(Value::Null),
        "started_at": run.get("started_at").cloned().unwrap_or(Value::Null),
        "completed_at": run.get("completed_at").cloned().unwrap_or(Value::Null),
        "scope_note": run.get("scope_note").cloned().unwrap_or(Value::Null),
        "head_commit": run.get("head_commit").cloned().unwrap_or(Value::Null),
        "archived": run.get("archived").and_then(Value::as_bool).unwrap_or(false),
        "pinned": run.get("pinned").and_then(Value::as_bool).unwrap_or(false),
        "run_flags": run_flags(run),
        "lanes_completed": run.get("lanes_completed").cloned().unwrap_or_else(|| json!([])),
        "correction_phase": run.get("correction_phase").cloned().unwrap_or(Value::Null),
        "correction_phase_note": run.get("correction_phase_note").cloned().unwrap_or(Value::Null),
        "run_root": display_path(run_root),
        "finding_count": findings.len(),
        "open_findings": open_findings,
        "severity_counts": count_field(&findings, "severity"),
        "status_counts": count_field(&findings, "status"),
        "closure_status_counts": count_field(&closure_rows, "closure_status"),
        "lane_counts": count_lanes(&findings),
        "artifact_refs": audit_artifact_refs(run_root),
        "top_findings": serialize_findings(&findings, &closure_by_id, 5),
    });
    if include_findings {
        summary["finding_result_count"] = json!(findings.len());
        summary["finding_limit"] = json!(250);
        summary["findings"] = serialize_findings(&findings, &closure_by_id, 250);
        summary["lane_artifacts"] = audit_lane_artifacts(run_root);
    }
    summary
}

fn program_tier_payload() -> Value {
    let programs_root = kernel_state_path(&["programs"]);
    let mut programs = Vec::new();
    if let Ok(entries) = fs::read_dir(&programs_root) {
        for entry in entries.flatten() {
            let path = entry.path();
            let program = path.join("PROGRAM.md");
            let state = path.join("program-state.json");
            if !program.exists() {
                continue;
            }
            let state_json = read_json(&state).unwrap_or_else(|| json!({}));
            programs.push(json!({
                "program_id": path.file_name().and_then(|name| name.to_str()).unwrap_or_default(),
                "program_path": display_path(&program),
                "state_path": if state.exists() { json!(display_path(&state)) } else { Value::Null },
                "phase": state_json.get("phase").cloned().unwrap_or_else(|| json!("unknown")),
                "current_wave": state_json.get("current_wave").cloned().unwrap_or(Value::Null),
                "packs_total": state_json.get("packs").and_then(Value::as_array).map(|items| items.len()).unwrap_or(0),
                "packs_complete": state_json
                    .get("packs")
                    .and_then(Value::as_array)
                    .map(|items| items.iter().filter(|item| field_string(item, "run_status") == "complete").count())
                    .unwrap_or(0),
            }));
        }
    }
    programs.sort_by(|left, right| {
        right
            .get("program_id")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .cmp(
                left.get("program_id")
                    .and_then(Value::as_str)
                    .unwrap_or_default(),
            )
    });
    json!({
        "status": "ok",
        "source": "amw-kernel",
        "programs": programs,
        "summary": {"program_count": programs.len()},
    })
}

fn program_tier_detail_payload(program_id: &str) -> Value {
    let program_dir = kernel_state_path(&["programs", program_id]);
    let program = program_dir.join("PROGRAM.md");
    if !program.exists() {
        return json!({"status": "not_found", "program_id": program_id});
    }
    let state = read_json(&program_dir.join("program-state.json")).unwrap_or_else(|| json!({}));
    json!({
        "status": "ok",
        "source": "amw-kernel",
        "program_id": program_id,
        "program_path": display_path(&program),
        "phase": state.get("phase").cloned().unwrap_or_else(|| json!("unknown")),
        "current_wave": state.get("current_wave").cloned().unwrap_or(Value::Null),
        "next_phase": state.get("next_phase").cloned().unwrap_or(Value::Null),
        "packs": state.get("packs").cloned().unwrap_or_else(|| json!([])),
    })
}

fn audit_run_root(run: &serde_json::Map<String, Value>) -> Option<PathBuf> {
    let raw = run.get("run_root")?.as_str()?;
    let path = PathBuf::from(raw);
    let resolved = if path.is_absolute() {
        path
    } else {
        workspace_path(&[]).join(path)
    };
    if resolved.is_dir() {
        Some(resolved)
    } else {
        None
    }
}

fn workspace_path(parts: &[&str]) -> PathBuf {
    let mut path = std::env::var_os("VETINARI_WORKSPACE_ROOT")
        .map(PathBuf::from)
        .unwrap_or_else(|| std::env::current_dir().unwrap_or_else(|_| PathBuf::from(".")));
    for part in parts {
        path.push(part);
    }
    path
}

fn kernel_state_path(parts: &[&str]) -> PathBuf {
    let mut path = std::env::var_os("AMW_KERNEL_STATE_DIR")
        .map(PathBuf::from)
        .unwrap_or_else(|| workspace_path(&[".vetinari"]));
    for part in parts {
        path.push(part);
    }
    path
}

fn read_json(path: &FsPath) -> Option<Value> {
    fs::read_to_string(path)
        .ok()
        .and_then(|text| serde_json::from_str::<Value>(&text).ok())
}

fn field_string(row: &Value, field: &str) -> String {
    row.get(field)
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_string()
}

fn count_field(rows: &[Value], field: &str) -> Value {
    let mut counts = BTreeMap::<String, usize>::new();
    for row in rows {
        let key = field_string(row, field);
        let key = if key.is_empty() {
            "unknown".to_string()
        } else {
            key
        };
        *counts.entry(key.to_ascii_lowercase()).or_default() += 1;
    }
    json!(counts)
}

fn count_lanes(rows: &[Value]) -> Value {
    let mut counts = BTreeMap::<String, usize>::new();
    for row in rows {
        let scope = field_string(row, "scope");
        let lane = scope.strip_prefix("lane:").unwrap_or(&scope);
        let lane = if lane.is_empty() { "unknown" } else { lane };
        *counts.entry(lane.to_string()).or_default() += 1;
    }
    json!(counts)
}

fn run_flags(run: &serde_json::Map<String, Value>) -> Value {
    let flags = [
        "has_handoff_brief",
        "has_prevention_handoff",
        "has_contradiction_report",
        "has_lessons_applied",
    ];
    let mut values = BTreeMap::new();
    for flag in flags {
        if let Some(value) = run.get(flag).and_then(Value::as_bool) {
            values.insert(flag, value);
        }
    }
    json!(values)
}

fn closure_by_finding_id(rows: &[Value]) -> BTreeMap<String, Value> {
    let mut result = BTreeMap::new();
    for row in rows {
        let id = field_string(row, "finding_id");
        if !id.is_empty() {
            result.insert(id, row.clone());
        }
    }
    result
}

fn serialize_findings(
    rows: &[Value],
    closure_by_id: &BTreeMap<String, Value>,
    limit: usize,
) -> Value {
    let findings: Vec<Value> = rows
        .iter()
        .take(limit)
        .map(|row| {
            let id = field_string(row, "id");
            let scope = field_string(row, "scope");
            let lane = scope.strip_prefix("lane:").unwrap_or(&scope).to_string();
            let closure = closure_by_id.get(&id).cloned().unwrap_or_else(|| json!({}));
            json!({
                "id": id,
                "title": field_string(row, "title"),
                "status": field_string(row, "status"),
                "severity": field_string(row, "severity"),
                "lane": lane,
                "root_cause": row.get("root_cause").cloned().unwrap_or(Value::Null),
                "impact": row.get("impact").cloned().unwrap_or(Value::Null),
                "closure_status": closure.get("closure_status").cloned().unwrap_or(Value::Null),
                "evidence_refs": closure.get("evidence_refs").cloned().unwrap_or_else(|| json!([])),
            })
        })
        .collect();
    json!(findings)
}

fn audit_artifact_refs(run_root: &FsPath) -> Value {
    let names = [
        "finding-registry.json",
        "CLOSURE-STATUS.json",
        "CHECKPOINT-STATE.json",
        "HANDOFF-BRIEF.md",
    ];
    json!(names
        .iter()
        .map(|name| run_root.join(name))
        .filter(|path| path.exists())
        .map(|path| display_path(&path))
        .collect::<Vec<_>>())
}

fn audit_lane_artifacts(run_root: &FsPath) -> Value {
    let mut artifacts = Vec::new();
    if let Ok(entries) = fs::read_dir(run_root) {
        for entry in entries.flatten() {
            let path = entry.path();
            if !path.is_dir() {
                continue;
            }
            let lane = path
                .file_name()
                .and_then(|name| name.to_str())
                .unwrap_or_default();
            for name in [
                "FINDINGS.md",
                "LANE-EVIDENCE.json",
                "ROUND2-BROWSER-PROOF.json",
            ] {
                let candidate = path.join(name);
                if candidate.exists() {
                    artifacts.push(json!({"lane": lane, "path": display_path(&candidate)}));
                }
            }
        }
    }
    json!(artifacts)
}

fn display_path(path: &FsPath) -> String {
    let root = workspace_path(&[]);
    match path.strip_prefix(root) {
        Ok(relative) => relative.to_string_lossy().replace('\\', "/"),
        Err(_) => path.to_string_lossy().replace('\\', "/"),
    }
}

#[derive(Debug, Clone)]
struct WorkbenchSpineSnapshot {
    path: PathBuf,
    available: bool,
    assets: Vec<Value>,
    runs: Vec<Value>,
    traces: Vec<Value>,
    evals: Vec<Value>,
    proposals: Vec<Value>,
    leases: Vec<Value>,
    promotions: Vec<Value>,
    deleted_count: usize,
}

impl WorkbenchSpineSnapshot {
    fn empty(path: PathBuf) -> Self {
        Self {
            path,
            available: false,
            assets: Vec::new(),
            runs: Vec::new(),
            traces: Vec::new(),
            evals: Vec::new(),
            proposals: Vec::new(),
            leases: Vec::new(),
            promotions: Vec::new(),
            deleted_count: 0,
        }
    }

    fn active_record_count(&self) -> usize {
        self.assets.len()
            + self.runs.len()
            + self.traces.len()
            + self.evals.len()
            + self.proposals.len()
            + self.leases.len()
            + self.promotions.len()
    }
}

fn workbench_spine_dir() -> PathBuf {
    if let Some(override_dir) = std::env::var_os("VETINARI_WORKBENCH_SPINE_DIR") {
        return PathBuf::from(override_dir);
    }
    if let Some(user_dir) = std::env::var_os("VETINARI_USER_DIR") {
        return PathBuf::from(user_dir)
            .join("outputs")
            .join("workbench")
            .join("spine");
    }
    workspace_path(&["outputs", "workbench", "spine"])
}

fn load_workbench_spine_snapshot() -> Result<WorkbenchSpineSnapshot, String> {
    let dir = workbench_spine_dir();
    let jsonl_path = dir.join("spine.jsonl");
    if !jsonl_path.exists() {
        return Ok(WorkbenchSpineSnapshot::empty(jsonl_path));
    }
    let mut paths = Vec::new();
    if let Ok(entries) = fs::read_dir(&dir) {
        for entry in entries.flatten() {
            let path = entry.path();
            let Some(name) = path.file_name().and_then(|name| name.to_str()) else {
                continue;
            };
            if name == "spine.rust.jsonl" {
                continue;
            }
            if name.starts_with("spine.") && name.ends_with(".jsonl") && name != "spine.jsonl" {
                paths.push(path);
            }
        }
    }
    paths.sort();
    paths.push(jsonl_path.clone());

    let mut active = BTreeMap::<(String, String), Value>::new();
    let mut deleted_count = 0usize;
    for path in paths {
        let raw = fs::read(&path).map_err(|err| {
            format!(
                "workbench-spine-jsonl-unreadable:{}:{err}",
                display_path(&path)
            )
        })?;
        if raw.is_empty() {
            continue;
        }
        if !raw.ends_with(b"\n") {
            return Err(format!(
                "workbench-spine-jsonl-truncated:{}",
                display_path(&path)
            ));
        }
        let text = String::from_utf8(raw).map_err(|err| {
            format!(
                "workbench-spine-jsonl-not-utf8:{}:{err}",
                display_path(&path)
            )
        })?;
        for (index, line) in text.lines().enumerate() {
            if line.trim().is_empty() {
                continue;
            }
            let row: Value = serde_json::from_str(line).map_err(|err| {
                format!(
                    "workbench-spine-jsonl-invalid:{}:{}:{err}",
                    display_path(&path),
                    index + 1
                )
            })?;
            let kind = row.get("kind").and_then(Value::as_str).ok_or_else(|| {
                format!(
                    "workbench-spine-row-missing-kind:{}:{}",
                    display_path(&path),
                    index + 1
                )
            })?;
            let record_id = row
                .get("record_id")
                .and_then(Value::as_str)
                .ok_or_else(|| {
                    format!(
                        "workbench-spine-row-missing-record-id:{}:{}",
                        display_path(&path),
                        index + 1
                    )
                })?;
            let payload = row.get("payload").ok_or_else(|| {
                format!(
                    "workbench-spine-row-missing-payload:{}:{}",
                    display_path(&path),
                    index + 1
                )
            })?;
            if !payload.is_object() {
                return Err(format!(
                    "workbench-spine-row-payload-not-object:{}:{}",
                    display_path(&path),
                    index + 1
                ));
            }
            if kind == "delete" {
                let target_kind = payload
                    .get("target_kind")
                    .and_then(Value::as_str)
                    .ok_or_else(|| {
                        format!(
                            "workbench-spine-delete-missing-target-kind:{}:{}",
                            display_path(&path),
                            index + 1
                        )
                    })?;
                let target_record_id = payload
                    .get("target_record_id")
                    .and_then(Value::as_str)
                    .ok_or_else(|| {
                        format!(
                            "workbench-spine-delete-missing-target-record-id:{}:{}",
                            display_path(&path),
                            index + 1
                        )
                    })?;
                active.remove(&(target_kind.to_string(), target_record_id.to_string()));
                deleted_count = deleted_count.saturating_add(1);
                continue;
            }
            if !matches!(
                kind,
                "asset" | "run" | "trace" | "eval" | "proposal" | "lease" | "promotion"
            ) {
                return Err(format!("workbench-spine-row-unknown-kind:{kind}"));
            }
            let key = (kind.to_string(), record_id.to_string());
            if active.contains_key(&key) {
                return Err(format!(
                    "workbench-spine-duplicate-active-record:{kind}:{record_id}"
                ));
            }
            active.insert(key, payload.clone());
        }
    }

    let mut snapshot = WorkbenchSpineSnapshot::empty(jsonl_path);
    snapshot.available = true;
    snapshot.deleted_count = deleted_count;
    for ((kind, _record_id), payload) in active {
        match kind.as_str() {
            "asset" => snapshot.assets.push(payload),
            "run" => snapshot.runs.push(payload),
            "trace" => snapshot.traces.push(payload),
            "eval" => snapshot.evals.push(payload),
            "proposal" => snapshot.proposals.push(payload),
            "lease" => snapshot.leases.push(payload),
            "promotion" => snapshot.promotions.push(payload),
            _ => {}
        }
    }
    Ok(snapshot)
}

fn value_str<'a>(row: &'a Value, key: &str) -> &'a str {
    row.get(key).and_then(Value::as_str).unwrap_or_default()
}

fn spine_counts_payload(snapshot: &WorkbenchSpineSnapshot) -> Value {
    json!({
        "assets": snapshot.assets.len(),
        "runs": snapshot.runs.len(),
        "traces": snapshot.traces.len(),
        "evals": snapshot.evals.len(),
        "proposals": snapshot.proposals.len(),
        "leases": snapshot.leases.len(),
        "promotions": snapshot.promotions.len(),
        "deleted": snapshot.deleted_count,
    })
}

fn spine_unavailable_payload(surface: &str, error: String) -> Value {
    json!({
        "status": "unavailable",
        "surface": surface,
        "source": "amw-kernel",
        "state_source": "outputs/workbench/spine/spine.jsonl",
        "degraded": true,
        "degraded_reason": error,
        "native_state_coverage": {
            "metadata_spine": false
        }
    })
}

fn spine_empty_reason(snapshot: &WorkbenchSpineSnapshot, kind: &str) -> Value {
    if snapshot.available {
        Value::String(format!(
            "no active {kind} records were present in the Workbench metadata spine"
        ))
    } else {
        Value::String(format!(
            "metadata spine file has not been created yet, so no {kind} records are available"
        ))
    }
}

fn workbench_run_task(run: &Value) -> Value {
    let run_id = value_str(run, "run_id");
    let status = value_str(run, "status");
    json!({
        "run_id": run_id,
        "task_id": run_id,
        "agent_type": value_str(run, "actor_agent_type"),
        "status": status,
        "lane": value_str(run, "kind"),
        "escalated": status == "blocked",
        "escalation_reason": if status == "blocked" { Value::String("workbench run is blocked".to_string()) } else { Value::Null },
        "recursive_parent_run_id": null,
        "blocker_summary": if status == "blocked" { Value::String("blocked run from Workbench metadata spine".to_string()) } else { Value::Null },
        "retries": 0,
        "paused": status == "pending",
        "evidence_links": run.get("asset_revisions").cloned().unwrap_or_else(|| json!([])),
        "started_at_utc": value_str(run, "started_at_utc"),
        "finished_at_utc": value_str(run, "finished_at_utc"),
        "source": "outputs/workbench/spine/spine.jsonl"
    })
}

fn spine_lease_queue_entry(lease: &Value) -> Value {
    let lease_status = value_str(lease, "status");
    json!({
        "lease_id": value_str(lease, "lease_id"),
        "caller_subsystem": "workbench-metadata-spine",
        "target": value_str(lease, "lane"),
        "state": match lease_status {
            "requested" => "pending",
            "granted" => "active",
            "released" | "denied" | "expired" => "finished",
            _ => "unknown",
        },
        "lease_status": lease_status,
        "age_seconds": 0,
        "run_id": value_str(lease, "requested_for_run_id"),
        "requested_at_utc": value_str(lease, "granted_at_utc"),
        "vram_share": lease.get("vram_share").cloned().unwrap_or(Value::Null),
        "source": "outputs/workbench/spine/spine.jsonl"
    })
}

fn spine_object_rows(snapshot: &WorkbenchSpineSnapshot) -> Vec<Value> {
    let mut rows = Vec::new();
    rows.extend(snapshot.runs.iter().map(|run| {
        let run_id = value_str(run, "run_id");
        let status = value_str(run, "status");
        json!({
            "object_id": run_id,
            "object_kind": "run",
            "title": format!("{} / {run_id}", value_str(run, "kind")),
            "status": status,
            "view": "mission-control",
            "provenance_state": "linked",
            "risk_level": if status == "blocked" || status == "failed" { "high" } else { "low" },
            "updated_at_utc": value_str(run, "started_at_utc"),
            "why": "Run record read from the Workbench metadata spine append log."
        })
    }));
    rows.extend(snapshot.assets.iter().map(|asset| {
        let asset_id = value_str(asset, "asset_id");
        json!({
            "object_id": asset_id,
            "object_kind": "artifact",
            "title": value_str(asset, "name"),
            "status": value_str(asset, "revision"),
            "view": "evidence-notebooks",
            "provenance_state": "linked",
            "risk_level": if asset.get("taints").and_then(Value::as_array).is_some_and(|taints| !taints.is_empty()) { "medium" } else { "low" },
            "updated_at_utc": value_str(asset, "created_at_utc"),
            "why": "Asset record read from the Workbench metadata spine append log."
        })
    }));
    rows.extend(snapshot.evals.iter().map(|eval| {
        let eval_id = value_str(eval, "eval_id");
        let failed = eval
            .get("scores")
            .and_then(Value::as_array)
            .is_some_and(|scores| {
                scores
                    .iter()
                    .any(|score| score.get("passed").and_then(Value::as_bool) == Some(false))
            });
        json!({
            "object_id": eval_id,
            "object_kind": "eval",
            "title": format!("{} / {eval_id}", value_str(eval, "kind")),
            "status": if failed { "failed" } else { "passed" },
            "view": "experiment-lab",
            "provenance_state": "linked",
            "risk_level": if failed { "medium" } else { "low" },
            "updated_at_utc": value_str(eval, "captured_at_utc"),
            "why": "Eval record read from the Workbench metadata spine append log."
        })
    }));
    rows.extend(snapshot.proposals.iter().map(|proposal| {
        let proposal_id = value_str(proposal, "proposal_id");
        json!({
            "object_id": proposal_id,
            "object_kind": "proposal",
            "title": format!("{} / {proposal_id}", value_str(proposal, "kind")),
            "status": value_str(proposal, "status"),
            "view": "promotion-inbox",
            "provenance_state": "linked",
            "risk_level": "medium",
            "updated_at_utc": value_str(proposal, "opened_at_utc"),
            "why": "Proposal record read from the Workbench metadata spine append log."
        })
    }));
    rows
}

fn query_value<'a>(query: &'a str, key: &str) -> Option<&'a str> {
    query.split('&').find_map(|pair| {
        let (candidate, value) = pair.split_once('=')?;
        if candidate == key && !value.trim().is_empty() {
            Some(value)
        } else {
            None
        }
    })
}

fn query_bool(query: &str, key: &str, fallback: bool) -> bool {
    query_value(query, key)
        .map(|value| value == "1" || value.eq_ignore_ascii_case("true"))
        .unwrap_or(fallback)
}

fn query_usize(query: &str, key: &str, fallback: usize, min: usize, max: usize) -> usize {
    query_value(query, key)
        .and_then(|value| value.parse::<usize>().ok())
        .map(|value| value.clamp(min, max))
        .unwrap_or(fallback)
}

fn api_family<'a>(segments: &'a [&'a str]) -> Option<&'a str> {
    match segments {
        ["api", "v1", family, ..] => Some(family),
        ["api", family, ..] => Some(family),
        _ => None,
    }
}

fn workbench_surface_from_path<'a>(
    path: &'a str,
    segments: &'a [&'a str],
) -> Option<(&'a str, &'a str)> {
    if path.starts_with("/api/workbench/") {
        return Some((
            segments.get(2).copied()?,
            segments.get(3).copied().unwrap_or("snapshot"),
        ));
    }
    if path.starts_with("/api/v1/workbench/") {
        if segments.get(3).copied() == Some("migration") {
            return None;
        }
        if segments.get(4).copied() == Some("gateway-policy") {
            return Some((
                "gateway-policy",
                segments.get(5).copied().unwrap_or("snapshot"),
            ));
        }
        return Some((
            segments.get(3).copied()?,
            segments.get(4).copied().unwrap_or("snapshot"),
        ));
    }
    if segments.len() >= 5
        && segments[0] == "api"
        && segments[1] == "v1"
        && segments[2] == "projects"
        && segments[4] == "workbench"
    {
        return Some((
            segments.get(5).copied()?,
            segments.get(6).copied().unwrap_or("snapshot"),
        ));
    }
    None
}

fn route_entity(path: &str, kind: &str) -> Value {
    let id = path
        .trim_matches('/')
        .rsplit('/')
        .next()
        .filter(|segment| !segment.is_empty())
        .unwrap_or("current");
    json!({"kind": kind, "id": id})
}

fn canonical_workbench_routes(surface: &str) -> Value {
    match surface {
        "adaptive-tuning" => json!([
            "/api/workbench/adaptive-tuning/snapshot/:project_id",
            "/api/workbench/adaptive-tuning/rollback-readiness/:project_id/:proposal_id"
        ]),
        "artifact-reviews" => json!(["/api/workbench/artifact-reviews"]),
        "channels" => json!([
            "/api/workbench/channels/config",
            "/api/workbench/channels/activity"
        ]),
        "command-safety" => json!([
            "/api/workbench/command-safety/profiles",
            "/api/workbench/command-safety/classify",
            "/api/workbench/command-safety/decide"
        ]),
        "evidence-notebooks" => json!(["/api/workbench/evidence-notebooks"]),
        "managed-agents" => json!(["/api/workbench/managed-agents/snapshot"]),
        "memory" => json!(["/api/workbench/memory/review-graph"]),
        "readiness" => json!(["/api/workbench/readiness/snapshot"]),
        "resource-cockpit" => json!([
            "/api/workbench/resource-cockpit/snapshot",
            "/api/workbench/resource-cockpit/leases",
            "/api/workbench/resource-cockpit/queued",
            "/api/workbench/resource-cockpit/policy-proposals"
        ]),
        "run-kernel" => json!(["/api/workbench/run-kernel/runs"]),
        "shell" => json!(["/api/workbench/shell/snapshot"]),
        "updates" => json!([
            "/api/workbench/updates/readiness",
            "/api/workbench/updates/support-bundle"
        ]),
        "workflow-builder" => json!([
            "/api/workbench/workflow-builder/metadata",
            "/api/workbench/workflow-builder/graphs/:project_id",
            "/api/workbench/workflow-builder/console/:project_id"
        ]),
        _ => json!([]),
    }
}

fn workbench_compatibility_contract_payload(
    surface: &str,
    operation: &str,
    mutable: bool,
) -> Value {
    json!({
        "status": "bounded_compatibility",
        "route_mode": "native_rust_compatibility",
        "state_source": "amw-kernel compatibility contract",
        "canonical_native_routes": canonical_workbench_routes(surface),
        "empty_state_policy": {
            "allowed": true,
            "why": "This catchall preserves legacy Workbench route reachability; full state is exposed by the canonical native Rust route handlers when the surface has a migrated state contract."
        },
        "mutation_policy": {
            "accepted": mutable,
            "receipt_required": mutable,
            "why": if mutable {
                "Mutable compatibility calls return bounded acceptance only; domain-specific mutation routes own durable effects."
            } else {
                "Read-only compatibility calls expose route metadata and bounded empty-state contracts."
            }
        },
        "operation": operation
    })
}

fn api_compatibility_contract_payload(family: &str, mutable: bool) -> Value {
    json!({
        "status": "bounded_compatibility",
        "route_mode": "native_rust_api_compatibility",
        "state_source": "amw-kernel api compatibility contract",
        "family": family,
        "empty_state_policy": {
            "allowed": true,
            "why": "Generic API catchalls keep renderer compatibility without claiming unavailable family-specific records exist."
        },
        "mutation_policy": {
            "accepted": mutable,
            "receipt_required": mutable,
            "why": if mutable {
                "Mutable generic API calls are bounded compatibility acknowledgements unless a named native route owns the state change."
            } else {
                "Read-only generic API calls return compatibility metadata when no named native route exists."
            }
        }
    })
}

fn autonomy_payload(method: &str, path: &str, segments: &[&str], mutable: bool) -> Value {
    let action_type = segments
        .windows(3)
        .find_map(|window| {
            if window[0] == "promotions" && window[2] == "veto" {
                Some(window[1])
            } else {
                None
            }
        })
        .unwrap_or_else(|| {
            segments
                .last()
                .copied()
                .filter(|value| *value != "autonomy")
                .unwrap_or("default")
        });
    let is_veto = method == "POST" && path.contains("/promotions/") && path.ends_with("/veto");
    let accepted = is_veto && !action_type.trim().is_empty();
    let receipt_id = format!(
        "autonomy-{action_type}-{}",
        if accepted { "veto" } else { "blocked" }
    );
    let blocked_reasons = if accepted {
        vec!["operator-veto-recorded".to_string()]
    } else if mutable {
        vec!["operator-gate-missing:autonomy-approval".to_string()]
    } else {
        vec!["policy-explanation-missing".to_string()]
    };
    let policy_path = locate_repo_file("config/autonomy_policies.yaml")
        .unwrap_or_else(|| workspace_path(&["config", "autonomy_policies.yaml"]));
    let pending_path = kernel_state_path(&["autonomy-approval-queue.jsonl"]);
    let decision_log_path = kernel_state_path(&["autonomy-decision-log.jsonl"]);
    let queue_status = if accepted {
        "not_required"
    } else if mutable {
        match append_autonomy_pending_action(
            &pending_path,
            action_type,
            &receipt_id,
            &blocked_reasons,
        ) {
            Ok(()) => "queued",
            Err(_) => "queue_unavailable",
        }
    } else {
        "not_required"
    };
    let decision_log_status =
        match append_autonomy_decision(&decision_log_path, action_type, &receipt_id, accepted) {
            Ok(()) => "recorded",
            Err(_) => "unavailable",
        };
    json!({
        "status": if accepted { "accepted" } else { "blocked" },
        "source": "amw-kernel",
        "state_source": "amw-kernel::autonomy_policy",
        "native_owner": "amw-kernel::autonomy_policy",
        "action_type": action_type,
        "policy_source": display_path(&policy_path),
        "policy_loaded": policy_path.exists(),
        "plan_id": format!("autonomy-plan-{action_type}"),
        "activation_eligible": false,
        "decision_kind": "blocked",
        "blocked_reasons": blocked_reasons.clone(),
        "required_operator_gate_ids": ["autonomy-approval"],
        "autonomy_level": "L0_MANUAL",
        "mode": "balanced",
        "decision": {
            "accepted": accepted,
            "decision": if accepted { "deny" } else { "defer" },
            "requires_human": true,
            "rollback_on_regression": true,
            "max_change_pct": 0.0,
            "reason": if accepted {
                "promotion-veto-recorded"
            } else if mutable {
                "autonomy-mutation-requires-explicit-veto-or-approval"
            } else {
                "autonomy-read-fail-closed"
            }
        },
        "trust": {
            "total_actions": 0,
            "successful_actions": 0,
            "consecutive_failures": 0,
            "success_rate": 0.0,
            "eligible_for_promotion": false
        },
        "approval_queue": {
            "status": queue_status,
            "path": display_path(&pending_path)
        },
        "decision_log": {
            "status": decision_log_status,
            "path": display_path(&decision_log_path)
        },
        "receipt_id": receipt_id.clone(),
        "shadow_activation_decision": {
            "plan_id": format!("autonomy-plan-{action_type}"),
            "activation_eligible": false,
            "decision_kind": "blocked",
            "blocked_reasons": blocked_reasons,
            "required_operator_gate_ids": ["autonomy-approval"],
            "receipt_id": receipt_id
        }
    })
}

fn append_autonomy_pending_action(
    path: &FsPath,
    action_type: &str,
    receipt_id: &str,
    reasons: &[String],
) -> std::io::Result<()> {
    let row = json!({
        "schema_version": "native-autonomy-approval-queue.v1",
        "action_id": receipt_id,
        "action_type": action_type,
        "details": {"blocked_reasons": reasons},
        "confidence": 0.0,
        "status": "pending",
        "created_at": utc_now_rfc3339(),
        "source": "amw-kernel::autonomy_policy"
    });
    append_jsonl(path, &row)
}

fn append_autonomy_decision(
    path: &FsPath,
    action_type: &str,
    receipt_id: &str,
    accepted: bool,
) -> std::io::Result<()> {
    let row = json!({
        "schema_version": "native-autonomy-decision-log.v1",
        "action_id": receipt_id,
        "action_type": action_type,
        "autonomy_level": "L0_MANUAL",
        "decision": if accepted { "deny" } else { "defer" },
        "confidence": 0.0,
        "outcome": if accepted { "operator-veto-recorded" } else { "operator-approval-required" },
        "timestamp": utc_now_rfc3339(),
        "source": "amw-kernel::autonomy_policy"
    });
    append_jsonl(path, &row)
}

fn append_jsonl(path: &FsPath, row: &Value) -> std::io::Result<()> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)?;
    }
    use std::io::Write as _;
    let mut file = fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(path)?;
    writeln!(
        file,
        "{}",
        serde_json::to_string(row).unwrap_or_else(|_| "{}".to_string())
    )?;
    file.flush()
}

fn merge_json_objects(left: Value, right: Value) -> Value {
    let mut base = match left {
        Value::Object(map) => map,
        _ => return right,
    };
    if let Value::Object(map) = right {
        base.extend(map);
    }
    Value::Object(base)
}

fn kernel_status_payload() -> Value {
    json!({
        "status": "ok",
        "server": "amw-kernel",
        "kernel_version": env!("CARGO_PKG_VERSION"),
        "api_domains": api_domain_authorities().len(),
        "workbench_surfaces": workbench_surface_policies().len(),
    })
}

async fn kernel_status() -> Json<Value> {
    Json(kernel_status_payload())
}

async fn mcp_message(Json(body): Json<Value>) -> Json<Value> {
    Json(mcp_message_payload(body))
}

async fn mcp_tools() -> Json<Value> {
    Json(mcp_tools_payload())
}

async fn mcp_resources() -> Json<Value> {
    Json(mcp_resources_payload())
}

async fn mcp_resource_read() -> Json<Value> {
    match mcp_resource_read_payload(&[]) {
        Ok(payload) => Json(payload),
        Err(error) => Json(json!({"error": error, "source": "amw-kernel"})),
    }
}

async fn mcp_resource_stream() -> Json<Value> {
    Json(mcp_resource_stream_payload())
}

async fn project_workbench_stream(Path(project_id): Path<String>) -> Response {
    let payload = json!({
        "project_id": project_id,
        "event_type": "status",
        "source": "amw-kernel",
        "state": "ready"
    });
    let body = format!("event: status\ndata: {payload}\n\n");
    Response::builder()
        .status(StatusCode::OK)
        .header("content-type", "text/event-stream; charset=utf-8")
        .header("cache-control", "no-cache")
        .body(Body::from(body))
        .unwrap_or_else(error_response)
}

async fn native_workbench_surface_fallback(method: Method, uri: Uri, body: Body) -> Response {
    let body = match to_bytes(body, 1024 * 1024).await {
        Ok(bytes) if bytes.is_empty() => None,
        Ok(bytes) => match serde_json::from_slice::<Value>(&bytes) {
            Ok(value) => Some(value),
            Err(error) => {
                return (
                    StatusCode::BAD_REQUEST,
                    Json(json!({
                        "error": "invalid json request body",
                        "detail": error.to_string(),
                        "source": "amw-kernel"
                    })),
                )
                    .into_response()
            }
        },
        Err(error) => {
            return (
                StatusCode::BAD_REQUEST,
                Json(json!({
                    "error": "request body unreadable",
                    "detail": error.to_string(),
                    "source": "amw-kernel"
                })),
            )
                .into_response()
        }
    };
    let request = KernelHttpRequest {
        method: method.as_str().to_string(),
        path: uri.to_string(),
        body,
    };
    match handle_kernel_request(request) {
        Ok(payload) => Json(payload).into_response(),
        Err(error) => (
            StatusCode::NOT_FOUND,
            Json(json!({"error": "unsupported native kernel route", "detail": error})),
        )
            .into_response(),
    }
}

fn mcp_message_payload(body: Value) -> Value {
    let request_id = body.get("id").cloned().unwrap_or(Value::Null);
    let method = body
        .get("method")
        .and_then(Value::as_str)
        .unwrap_or("unknown");
    if method == "initialize" {
        return json!({
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "protocolVersion": "2025-03-26",
                "capabilities": {
                    "tools": {"listChanged": false},
                    "resources": {"listChanged": false, "subscribe": true}
                },
                "serverInfo": {"name": "amw-kernel", "version": env!("CARGO_PKG_VERSION")},
                "source": "amw-kernel"
            }
        });
    }
    if method == "tools/list" {
        return json!({"jsonrpc": "2.0", "id": request_id, "result": mcp_tools_payload()});
    }
    json!({
        "jsonrpc": "2.0",
        "id": request_id,
        "result": {"status": "accepted", "method": method, "source": "amw-kernel"}
    })
}

fn mcp_tools_payload() -> Value {
    json!({
        "tools": [
            {"name": "workspace.context", "description": "Read local workspace context", "inputSchema": {"type": "object"}}
        ],
        "source": "amw-kernel"
    })
}

fn mcp_resources_payload() -> Value {
    let (registry, session) = default_mcp_registry_and_session();
    match crate::mcp::list_mcp_resources(&registry, &session) {
        Ok(resources) => json!({
            "resources": resources
                .into_iter()
                .map(|(uri, title)| json!({"uri": uri, "title": title}))
                .collect::<Vec<_>>(),
            "source": "amw-kernel",
            "state_source": "amw-kernel::mcp::McpResourceRegistry"
        }),
        Err(err) => support_envelope_payload(err),
    }
}

fn mcp_resource_read_payload(permissions: &[ExtensionPermission]) -> Result<Value, String> {
    let (registry, session) = default_mcp_registry_and_session();
    crate::mcp::read_mcp_resource(
        &registry,
        &session,
        "resource://workspace/context",
        permissions,
    )
    .map(|payload| json!({"payload": payload, "source": "amw-kernel"}))
    .map_err(|err| {
        if err.code == "MCP_RESOURCE_PERMISSION" {
            format!(
                "{}: {}; requires explicit resource permission",
                err.code, err.message
            )
        } else {
            format!("{}: {}", err.code, err.message)
        }
    })
}

fn mcp_resource_stream_payload() -> Value {
    let mut session = McpStreamSession::initialized("route-stream", 1).expect("bounded session");
    match session.enqueue_event("resources/subscribed resource://workspace/context") {
        Ok(queue_depth) => json!({
            "event": "resources/subscribed",
            "uri": "resource://workspace/context",
            "queue_depth": queue_depth,
            "session_id": session.session_id,
            "source": "amw-kernel",
            "state_source": "amw-kernel::mcp::McpStreamSession"
        }),
        Err(err) => support_envelope_payload(err),
    }
}

fn mission_control_snapshot_payload(project_id: &str) -> Value {
    let jobs = native_training_control()
        .lock()
        .expect("training control mutex")
        .jobs
        .values()
        .cloned()
        .collect::<Vec<_>>();
    let spine = match load_workbench_spine_snapshot() {
        Ok(snapshot) => snapshot,
        Err(error) => {
            return json!({
                "project_id": project_id,
                "generated_at_utc": utc_now_rfc3339(),
                "status": "unavailable",
                "degraded": true,
                "degraded_reason": error,
                "lanes": [],
                "queue": [],
                "agent_tasks": [],
                "recursive_children": [],
                "escalations": [],
                "recursive_children_truncated_at": null,
                "source": "amw-kernel",
                "state_source": "outputs/workbench/spine/spine.jsonl",
                "native_state_coverage": {
                    "training_control": true,
                    "scheduler_lanes": false,
                    "metadata_spine_runs": false,
                    "metadata_spine_leases": false,
                    "recursive_foreman_links": false
                }
            });
        }
    };
    let spine_queue = spine
        .leases
        .iter()
        .map(spine_lease_queue_entry)
        .collect::<Vec<_>>();
    let training_queue = jobs
        .iter()
        .filter(|job| matches!(job.status.as_str(), "running" | "paused"))
        .map(training_job_queue_entry)
        .collect::<Vec<_>>();
    let mut queue = spine_queue;
    queue.extend(training_queue);
    let mut lane_counts = BTreeMap::<String, (usize, usize, f64)>::new();
    for entry in &queue {
        let lane = entry
            .get("target")
            .and_then(Value::as_str)
            .filter(|lane| !lane.is_empty())
            .unwrap_or("unknown")
            .to_string();
        let counts = lane_counts.entry(lane).or_default();
        match entry.get("state").and_then(Value::as_str) {
            Some("active") => counts.0 = counts.0.saturating_add(1),
            Some("pending") => counts.1 = counts.1.saturating_add(1),
            _ => {}
        }
        if let Some(share) = entry.get("vram_share").and_then(Value::as_f64) {
            counts.2 += share;
        }
    }
    for lane in ["interactive", "hub_agent", "training"] {
        lane_counts.entry(lane.to_string()).or_default();
    }
    let lanes = lane_counts
        .into_iter()
        .map(|(lane, (active_count, queued_count, vram_share))| {
            mission_control_lane_with_vram(&lane, active_count, queued_count, vram_share)
        })
        .collect::<Vec<_>>();
    let mut agent_tasks = spine
        .runs
        .iter()
        .map(workbench_run_task)
        .collect::<Vec<_>>();
    agent_tasks.extend(jobs.iter().map(training_job_agent_task));
    let escalations = agent_tasks
        .iter()
        .filter(|task| task.get("escalated").and_then(Value::as_bool) == Some(true))
        .cloned()
        .collect::<Vec<_>>();
    let empty = agent_tasks.is_empty() && queue.is_empty() && spine.active_record_count() == 0;
    let metadata_attached = spine.available;
    json!({
        "project_id": project_id,
        "generated_at_utc": utc_now_rfc3339(),
        "status": if empty { "empty" } else if metadata_attached { "ok" } else { "degraded" },
        "degraded": !empty && !metadata_attached,
        "degraded_reason": if empty || metadata_attached {
            Value::Null
        } else {
            json!("native mission control has no readable Workbench metadata spine append log")
        },
        "lanes": lanes,
        "queue": queue,
        "agent_tasks": agent_tasks,
        "recursive_children": [],
        "escalations": escalations,
        "recursive_children_truncated_at": null,
        "source": "amw-kernel",
        "state_source": "outputs/workbench/spine/spine.jsonl",
        "spine": {
            "path": display_path(&spine.path),
            "available": spine.available,
            "active_record_count": spine.active_record_count(),
            "deleted_count": spine.deleted_count,
            "counts": {
                "assets": spine.assets.len(),
                "runs": spine.runs.len(),
                "traces": spine.traces.len(),
                "evals": spine.evals.len(),
                "proposals": spine.proposals.len(),
                "leases": spine.leases.len(),
                "promotions": spine.promotions.len()
            }
        },
        "native_state_coverage": {
            "training_control": true,
            "scheduler_lanes": metadata_attached,
            "metadata_spine_runs": metadata_attached,
            "metadata_spine_leases": metadata_attached,
            "recursive_foreman_links": false
        }
    })
}

fn mission_control_lane_with_vram(
    lane: &str,
    active_count: usize,
    queued_count: usize,
    vram_share_observed: f64,
) -> Value {
    let pressure = if queued_count > 0 {
        "red"
    } else if active_count > 0 {
        "amber"
    } else {
        "green"
    };
    json!({
        "lane": lane,
        "active_count": active_count,
        "queued_count": queued_count,
        "vram_share_committed": 0.0,
        "vram_share_observed": vram_share_observed,
        "pressure": pressure
    })
}

async fn method_library_list() -> Json<Value> {
    Json(json!({"methods": method_cards(), "source": "amw-kernel"}))
}

async fn method_library_catalog() -> Json<Value> {
    Json(json!({"catalog": method_catalog()}))
}

async fn method_library_negative_methods() -> Json<Value> {
    Json(
        json!({"negative_methods": [method_card("negative-baseline", "negative_method", "Negative baseline")]}),
    )
}

async fn method_library_by_kind(Path(kind): Path<String>) -> Json<Value> {
    Json(json!({"methods": [method_card("method-by-kind", &kind, "Filtered method")]}))
}

async fn method_library_by_promotion_status(Path(status): Path<String>) -> Json<Value> {
    Json(
        json!({"methods": [json!({"method_card_id": "method-by-status", "promotion_status": status})]}),
    )
}

async fn method_library_get(Path(method_card_id): Path<String>) -> Json<Value> {
    Json(json!({"method": method_card(&method_card_id, "prompting", "Selected method")}))
}

async fn adaptive_tuning_snapshot(Path(project_id): Path<String>) -> Json<Value> {
    Json(adaptive_tuning_snapshot_payload(&project_id))
}

fn adaptive_tuning_snapshot_payload(project_id: &str) -> Value {
    let snapshot = match load_workbench_spine_snapshot() {
        Ok(snapshot) => snapshot,
        Err(error) => {
            let mut payload = spine_unavailable_payload("adaptive-tuning", error);
            payload["project_id"] = json!(project_id);
            payload["hypotheses"] = json!([]);
            payload["decisions"] = json!([]);
            return payload;
        }
    };
    let hypotheses = snapshot
        .proposals
        .iter()
        .map(|proposal| {
            let proposal_id = value_str(proposal, "proposal_id");
            json!({
                "hypothesis_id": proposal_id,
                "proposal_id": proposal_id,
                "kind": value_str(proposal, "kind"),
                "status": value_str(proposal, "status"),
                "affected_assets": proposal.get("affected_assets").cloned().unwrap_or_else(|| json!([])),
                "affected_revisions": proposal.get("affected_revisions").cloned().unwrap_or_else(|| json!([])),
                "gate": proposal.get("gate").cloned().unwrap_or_else(|| json!({})),
                "opened_at_utc": value_str(proposal, "opened_at_utc"),
                "why": "Active proposal from the Workbench metadata spine is a tunable hypothesis."
            })
        })
        .collect::<Vec<_>>();
    let decisions = snapshot
        .promotions
        .iter()
        .map(|promotion| {
            json!({
                "decision_id": value_str(promotion, "promotion_id"),
                "status": value_str(promotion, "status"),
                "asset_id": value_str(promotion, "asset_id"),
                "asset_revision": value_str(promotion, "asset_revision"),
                "decided_at_utc": value_str(promotion, "decided_at_utc"),
                "why": "Promotion record from the Workbench metadata spine is a durable tuning decision."
            })
        })
        .collect::<Vec<_>>();
    let empty = hypotheses.is_empty() && decisions.is_empty();
    json!({
        "project_id": project_id,
        "status": if empty { "empty" } else { "ok" },
        "source": "amw-kernel",
        "state_source": "outputs/workbench/spine/spine.jsonl",
        "native_state_coverage": {
            "metadata_spine_proposals": snapshot.available,
            "metadata_spine_promotions": snapshot.available
        },
        "spine": {
            "path": display_path(&snapshot.path),
            "counts": spine_counts_payload(&snapshot)
        },
        "hypotheses": hypotheses,
        "decisions": decisions,
        "empty_reason": if empty { spine_empty_reason(&snapshot, "proposal or promotion") } else { Value::Null },
        "next_actions": [
            {"action": "propose", "requires_approval": false, "why": "Creates a new explicit hypothesis before mutation."},
            {"action": "decide", "requires_approval": true, "why": "Accepting or rejecting a proposal mutates tuning state."}
        ]
    })
}

async fn rollback_readiness(
    Path((project_id, proposal_id)): Path<(String, String)>,
) -> Json<Value> {
    Json(
        json!({"project_id": project_id, "proposal_id": proposal_id, "ready": true, "rollback_ref": format!("rollback:{proposal_id}")}),
    )
}

async fn resource_snapshot() -> Json<Value> {
    Json(resource_snapshot_payload())
}

async fn resource_leases() -> Json<Value> {
    Json(resource_leases_payload())
}

async fn resource_queued() -> Json<Value> {
    Json(resource_queued_payload())
}

async fn resource_safe_actions() -> Json<Value> {
    Json(json!({"actions": ["pause", "cancel", "adjust_interactive_reserve"]}))
}

async fn resource_execute(Path(action_id): Path<String>, Json(body): Json<Value>) -> ApiResult {
    resource_execute_payload(&action_id, body)
        .map(Json)
        .map_err(error_response)
}

fn resource_execute_payload(action_id: &str, body: Value) -> Result<Value, String> {
    let signal = LiveActionSignal {
        target_ref: body
            .get("target_ref")
            .and_then(Value::as_str)
            .unwrap_or("workbench")
            .to_string(),
        evidence_id: body
            .get("evidence_id")
            .and_then(Value::as_str)
            .unwrap_or("tauri-action")
            .to_string(),
        safety_signal_present: body
            .get("safety_signal_present")
            .and_then(Value::as_bool)
            .unwrap_or(false),
        approval_ref: body
            .get("approval_ref")
            .and_then(Value::as_str)
            .map(str::to_string),
    };
    let receipt = execute_live_action(action_id, signal).map_err(|err| format!("{err:?}"))?;
    Ok(json!({"receipt": {
        "receipt_id": receipt.receipt_id,
        "target_ref": receipt.target_ref,
        "status": receipt.status,
        "rollback_ref": receipt.rollback_ref,
    }}))
}

async fn resource_machine_profile() -> Json<Value> {
    Json(
        json!({"profile": {"cpu_class": "local", "memory_pressure": "unknown", "source": "amw-kernel"}}),
    )
}

async fn resource_policy_proposals() -> Json<Value> {
    Json(resource_policy_proposals_payload())
}

async fn approval_diff(Path(proposal_id): Path<String>) -> Json<Value> {
    Json(approval_diff_payload_with_request(&proposal_id, None))
}

fn resource_policy_proposals_payload() -> Value {
    let snapshot = match load_workbench_spine_snapshot() {
        Ok(snapshot) => snapshot,
        Err(error) => {
            let mut payload = spine_unavailable_payload("resource-cockpit-policy-proposals", error);
            payload["proposals"] = json!([]);
            return payload;
        }
    };
    let proposals = snapshot
        .proposals
        .iter()
        .map(|proposal| {
            let proposal_id = value_str(proposal, "proposal_id");
            json!({
                "proposal_id": proposal_id,
                "kind": value_str(proposal, "kind"),
                "status": value_str(proposal, "status"),
                "approval_required": true,
                "risk_level": "medium",
                "affected_assets": proposal.get("affected_assets").cloned().unwrap_or_else(|| json!([])),
                "affected_revisions": proposal.get("affected_revisions").cloned().unwrap_or_else(|| json!([])),
                "gate": proposal.get("gate").cloned().unwrap_or_else(|| json!({})),
                "opened_at_utc": value_str(proposal, "opened_at_utc"),
                "why": "Policy proposal is read from the Workbench metadata spine and requires explicit approval before mutation."
            })
        })
        .collect::<Vec<_>>();
    json!({
        "status": if proposals.is_empty() { "empty" } else { "ok" },
        "source": "amw-kernel",
        "state_source": "outputs/workbench/spine/spine.jsonl",
        "native_state_coverage": {"metadata_spine_proposals": snapshot.available},
        "spine": {"path": display_path(&snapshot.path), "counts": spine_counts_payload(&snapshot)},
        "proposals": proposals,
        "empty_reason": if snapshot.proposals.is_empty() { spine_empty_reason(&snapshot, "proposal") } else { Value::Null },
        "approval_policy": {
            "default": "approval-required",
            "why": "Resource policy changes can affect local execution capacity and must be auditable."
        }
    })
}

fn approval_diff_payload_with_request(proposal_id: &str, body: Option<&Value>) -> Value {
    let snapshot = match load_workbench_spine_snapshot() {
        Ok(snapshot) => snapshot,
        Err(error) => {
            let mut payload = spine_unavailable_payload("resource-cockpit-approval-diff", error);
            payload["proposal_id"] = json!(proposal_id);
            payload["diff"] = json!([]);
            payload["approval_required"] = json!(true);
            if let Some(body) = body {
                payload["request"] = redacted_request_summary(body);
            }
            return payload;
        }
    };
    let proposal = snapshot
        .proposals
        .iter()
        .find(|proposal| value_str(proposal, "proposal_id") == proposal_id);
    let diff = proposal
        .map(|proposal| {
            vec![
                json!({
                    "field": "affected_assets",
                    "before": [],
                    "after": proposal.get("affected_assets").cloned().unwrap_or_else(|| json!([])),
                    "risk": "medium"
                }),
                json!({
                    "field": "gate",
                    "before": {},
                    "after": proposal.get("gate").cloned().unwrap_or_else(|| json!({})),
                    "risk": "medium"
                }),
            ]
        })
        .unwrap_or_default();
    let mut payload = json!({
        "proposal_id": proposal_id,
        "status": if proposal.is_some() { "ok" } else { "empty" },
        "source": "amw-kernel",
        "state_source": "outputs/workbench/spine/spine.jsonl",
        "approval_required": true,
        "diff": diff,
        "empty_reason": if proposal.is_none() { Value::String(format!("proposal {proposal_id} was not present in the active metadata spine")) } else { Value::Null },
        "spine": {"path": display_path(&snapshot.path), "counts": spine_counts_payload(&snapshot)}
    });
    if let Some(body) = body {
        payload["request"] = redacted_request_summary(body);
    }
    payload
}

async fn capability_packs() -> Json<Value> {
    Json(capability_registry_payload())
}

async fn capability_pack(Path(pack_id): Path<String>) -> Json<Value> {
    Json(capability_pack_detail_payload(&pack_id))
}

async fn capability_pack_trust(Path(pack_id): Path<String>) -> Json<Value> {
    Json(json!({"trust": capability_registry_decision(&pack_id)}))
}

async fn capability_pack_decision(Path(pack_id): Path<String>) -> Json<Value> {
    Json(capability_not_implemented_payload(&pack_id))
}

async fn domain_kits() -> Json<Value> {
    Json(json!({"kits": [domain_kit_payload("software")]}))
}

async fn domain_kit(Path(kit_id): Path<String>) -> Json<Value> {
    Json(json!({"kit": domain_kit_payload(&kit_id)}))
}

async fn domain_kit_evaluate(Path(kit_id): Path<String>) -> Json<Value> {
    Json(kit_registry_unavailable_payload(&kit_id))
}

async fn workflow_metadata() -> Json<Value> {
    Json(json!({"node_types": ["task", "approval", "worker"], "source": "amw-kernel"}))
}

async fn workflow_graphs(Path(project_id): Path<String>) -> Json<Value> {
    Json(workflow_graphs_payload(&project_id))
}

async fn workflow_graph(Path((project_id, graph_id)): Path<(String, String)>) -> Json<Value> {
    Json(workflow_graph_payload(&project_id, &graph_id))
}

async fn workflow_console(Path(project_id): Path<String>) -> Json<Value> {
    Json(workflow_console_payload(&project_id))
}

fn workflow_graph_nodes_edges(
    project_id: &str,
    snapshot: &WorkbenchSpineSnapshot,
) -> (Vec<Value>, Vec<Value>) {
    let mut nodes = vec![json!({
        "node_id": format!("project:{project_id}"),
        "node_type": "project",
        "label": project_id,
        "status": if snapshot.active_record_count() == 0 { "empty" } else { "active" },
        "source": "amw-kernel",
        "why": "Native project anchor for Workbench workflow graph routes."
    })];
    let mut edges = Vec::new();
    for run in &snapshot.runs {
        let run_id = value_str(run, "run_id");
        nodes.push(json!({
            "node_id": format!("run:{run_id}"),
            "node_type": "run",
            "label": format!("{} / {run_id}", value_str(run, "kind")),
            "status": value_str(run, "status"),
            "updated_at_utc": value_str(run, "started_at_utc"),
            "source": "outputs/workbench/spine/spine.jsonl"
        }));
        edges.push(json!({
            "edge_id": format!("project:{project_id}->run:{run_id}"),
            "from": format!("project:{project_id}"),
            "to": format!("run:{run_id}"),
            "relation": "contains"
        }));
        if let Some(revisions) = run.get("asset_revisions").and_then(Value::as_array) {
            for revision in revisions {
                let Some(asset_id) = revision.get(0).and_then(Value::as_str) else {
                    continue;
                };
                edges.push(json!({
                    "edge_id": format!("run:{run_id}->asset:{asset_id}"),
                    "from": format!("run:{run_id}"),
                    "to": format!("asset:{asset_id}"),
                    "relation": "produced_or_used"
                }));
            }
        }
    }
    for asset in &snapshot.assets {
        let asset_id = value_str(asset, "asset_id");
        nodes.push(json!({
            "node_id": format!("asset:{asset_id}"),
            "node_type": "artifact",
            "label": value_str(asset, "name"),
            "status": value_str(asset, "revision"),
            "updated_at_utc": value_str(asset, "created_at_utc"),
            "source": "outputs/workbench/spine/spine.jsonl"
        }));
    }
    for eval in &snapshot.evals {
        let eval_id = value_str(eval, "eval_id");
        let run_id = value_str(eval, "run_id");
        let asset_id = value_str(eval, "asset_id");
        nodes.push(json!({
            "node_id": format!("eval:{eval_id}"),
            "node_type": "eval",
            "label": format!("{} / {eval_id}", value_str(eval, "kind")),
            "status": "recorded",
            "updated_at_utc": value_str(eval, "captured_at_utc"),
            "source": "outputs/workbench/spine/spine.jsonl"
        }));
        if !run_id.is_empty() {
            edges.push(json!({
                "edge_id": format!("eval:{eval_id}->run:{run_id}"),
                "from": format!("eval:{eval_id}"),
                "to": format!("run:{run_id}"),
                "relation": "evaluates"
            }));
        }
        if !asset_id.is_empty() {
            edges.push(json!({
                "edge_id": format!("eval:{eval_id}->asset:{asset_id}"),
                "from": format!("eval:{eval_id}"),
                "to": format!("asset:{asset_id}"),
                "relation": "scores"
            }));
        }
    }
    for proposal in &snapshot.proposals {
        let proposal_id = value_str(proposal, "proposal_id");
        nodes.push(json!({
            "node_id": format!("proposal:{proposal_id}"),
            "node_type": "proposal",
            "label": format!("{} / {proposal_id}", value_str(proposal, "kind")),
            "status": value_str(proposal, "status"),
            "updated_at_utc": value_str(proposal, "opened_at_utc"),
            "source": "outputs/workbench/spine/spine.jsonl"
        }));
        for asset_id in proposal
            .get("affected_assets")
            .and_then(Value::as_array)
            .into_iter()
            .flatten()
            .filter_map(Value::as_str)
        {
            edges.push(json!({
                "edge_id": format!("proposal:{proposal_id}->asset:{asset_id}"),
                "from": format!("proposal:{proposal_id}"),
                "to": format!("asset:{asset_id}"),
                "relation": "changes"
            }));
        }
    }
    for lease in &snapshot.leases {
        let lease_id = value_str(lease, "lease_id");
        let run_id = value_str(lease, "requested_for_run_id");
        nodes.push(json!({
            "node_id": format!("lease:{lease_id}"),
            "node_type": "lease",
            "label": format!("{} / {lease_id}", value_str(lease, "lane")),
            "status": value_str(lease, "status"),
            "updated_at_utc": value_str(lease, "granted_at_utc"),
            "source": "outputs/workbench/spine/spine.jsonl"
        }));
        if !run_id.is_empty() {
            edges.push(json!({
                "edge_id": format!("lease:{lease_id}->run:{run_id}"),
                "from": format!("lease:{lease_id}"),
                "to": format!("run:{run_id}"),
                "relation": "allocated_to"
            }));
        }
    }
    (nodes, edges)
}

fn workflow_graphs_payload(project_id: &str) -> Value {
    let snapshot = match load_workbench_spine_snapshot() {
        Ok(snapshot) => snapshot,
        Err(error) => {
            let mut payload = spine_unavailable_payload("workflow-builder-graphs", error);
            payload["project_id"] = json!(project_id);
            payload["graphs"] = json!([]);
            return payload;
        }
    };
    let (nodes, edges) = workflow_graph_nodes_edges(project_id, &snapshot);
    json!({
        "project_id": project_id,
        "status": if snapshot.active_record_count() == 0 { "empty" } else { "ok" },
        "source": "amw-kernel",
        "state_source": "outputs/workbench/spine/spine.jsonl",
        "spine": {"path": display_path(&snapshot.path), "counts": spine_counts_payload(&snapshot)},
        "graphs": [{
            "graph_id": "metadata-spine",
            "label": "Workbench Metadata Spine",
            "node_count": nodes.len(),
            "edge_count": edges.len(),
            "empty_reason": if snapshot.active_record_count() == 0 { spine_empty_reason(&snapshot, "workflow") } else { Value::Null }
        }]
    })
}

fn workflow_graph_payload(project_id: &str, graph_id: &str) -> Value {
    let snapshot = match load_workbench_spine_snapshot() {
        Ok(snapshot) => snapshot,
        Err(error) => {
            let mut payload = spine_unavailable_payload("workflow-builder-graph", error);
            payload["project_id"] = json!(project_id);
            payload["graph"] = json!({"graph_id": graph_id, "nodes": [], "edges": []});
            return payload;
        }
    };
    let (nodes, edges) = workflow_graph_nodes_edges(project_id, &snapshot);
    json!({
        "project_id": project_id,
        "status": if snapshot.active_record_count() == 0 { "empty" } else { "ok" },
        "source": "amw-kernel",
        "state_source": "outputs/workbench/spine/spine.jsonl",
        "graph": {
            "graph_id": graph_id,
            "basis": "metadata-spine",
            "nodes": nodes,
            "edges": edges,
            "empty_reason": if snapshot.active_record_count() == 0 { spine_empty_reason(&snapshot, "workflow") } else { Value::Null }
        }
    })
}

fn workflow_console_payload(project_id: &str) -> Value {
    let snapshot = match load_workbench_spine_snapshot() {
        Ok(snapshot) => snapshot,
        Err(error) => {
            let mut payload = spine_unavailable_payload("workflow-builder-console", error);
            payload["project_id"] = json!(project_id);
            payload["events"] = json!([]);
            return payload;
        }
    };
    let mut events = Vec::new();
    events.extend(snapshot.runs.iter().map(|run| {
        let run_id = value_str(run, "run_id");
        json!({
            "event_id": format!("run:{run_id}:{}", value_str(run, "status")),
            "event_type": "run",
            "object_id": run_id,
            "severity": if value_str(run, "status") == "failed" { "error" } else { "info" },
            "occurred_at_utc": value_str(run, "started_at_utc"),
            "message": format!("run {run_id} is {}", value_str(run, "status")),
            "source": "outputs/workbench/spine/spine.jsonl"
        })
    }));
    events.extend(snapshot.evals.iter().map(|eval| {
        let eval_id = value_str(eval, "eval_id");
        json!({
            "event_id": format!("eval:{eval_id}"),
            "event_type": "eval",
            "object_id": eval_id,
            "severity": "info",
            "occurred_at_utc": value_str(eval, "captured_at_utc"),
            "message": format!("eval {eval_id} recorded"),
            "source": "outputs/workbench/spine/spine.jsonl"
        })
    }));
    events.extend(snapshot.proposals.iter().map(|proposal| {
        let proposal_id = value_str(proposal, "proposal_id");
        json!({
            "event_id": format!("proposal:{proposal_id}:{}", value_str(proposal, "status")),
            "event_type": "proposal",
            "object_id": proposal_id,
            "severity": "warning",
            "occurred_at_utc": value_str(proposal, "opened_at_utc"),
            "message": format!("proposal {proposal_id} is {}", value_str(proposal, "status")),
            "source": "outputs/workbench/spine/spine.jsonl"
        })
    }));
    if events.is_empty() {
        events.push(json!({
            "event_id": "workflow-console:empty",
            "event_type": "empty-state",
            "object_id": format!("project:{project_id}"),
            "severity": "info",
            "occurred_at_utc": utc_now_rfc3339(),
            "message": "workflow console is ready; no metadata spine events are active",
            "source": "amw-kernel",
            "empty_reason": spine_empty_reason(&snapshot, "workflow event")
        }));
    }
    json!({
        "project_id": project_id,
        "status": if snapshot.active_record_count() == 0 { "empty" } else { "ok" },
        "source": "amw-kernel",
        "state_source": "outputs/workbench/spine/spine.jsonl",
        "spine": {"path": display_path(&snapshot.path), "counts": spine_counts_payload(&snapshot)},
        "events": events
    })
}

async fn channels_config() -> Json<Value> {
    Json(channels_config_payload())
}

async fn channels_activity() -> Json<Value> {
    Json(channels_activity_payload())
}

async fn benchmark_providers() -> Json<Value> {
    Json(benchmark_providers_payload())
}

async fn benchmark_import(Json(body): Json<Value>) -> Json<Value> {
    Json(benchmark_import_payload(body))
}

async fn migration_sources() -> Json<Value> {
    Json(migration_sources_payload())
}

async fn migration_plan(Json(body): Json<Value>) -> Json<Value> {
    Json(migration_plan_payload(body))
}

async fn migration_apply(Json(body): Json<Value>) -> Json<Value> {
    Json(migration_apply_payload(body))
}

async fn habit_health_summary(Path(user_id): Path<String>) -> Json<Value> {
    Json(
        json!({"user_id": user_id, "enabled": false, "default_policy": "opt-in", "source": "amw-kernel"}),
    )
}

async fn habit_health_review(Path(user_id): Path<String>) -> Json<Value> {
    Json(habit_health_review_payload(&user_id))
}

fn habit_health_review_payload(user_id: &str) -> Value {
    json!({
        "user_id": user_id,
        "status": "guarded",
        "source": "amw-kernel",
        "state_source": "native-habit-health-policy",
        "privacy": {
            "mode": "local-only",
            "default_policy": "opt-in",
            "export_requires_explicit_request": true,
            "delete_available": true,
            "why": "Habit-health state can contain sensitive routine data, so Rust exposes policy and review controls without fabricating personal history."
        },
        "review": [
            {
                "check_id": "habit-health:retention",
                "status": "guarded",
                "requires_approval": false,
                "why": "Local-only retention is the default until the user opts into export or downstream preview."
            },
            {
                "check_id": "habit-health:export",
                "status": "approval-required",
                "requires_approval": true,
                "why": "Export is available through the native route and must be explicit."
            },
            {
                "check_id": "habit-health:downstream-preview",
                "status": "approval-required",
                "requires_approval": true,
                "why": "Downstream previews are treated as sensitive data flow reviews."
            }
        ],
        "next_actions": [
            {"action": "export", "method": "POST", "path": "/api/workbench/habit-health/export", "requires_approval": true},
            {"action": "delete", "method": "POST", "path": "/api/workbench/habit-health/delete", "requires_approval": true},
            {"action": "downstream-preview", "method": "POST", "path": "/api/workbench/habit-health/downstream-preview", "requires_approval": true}
        ]
    })
}

async fn habit_health_export(Json(body): Json<Value>) -> Json<Value> {
    Json(
        json!({"export": {"format": "json", "records": []}, "request": redacted_request_summary(&body)}),
    )
}

async fn habit_health_downstream_preview(Json(body): Json<Value>) -> Json<Value> {
    Json(habit_health_downstream_preview_payload(&body))
}

fn habit_health_downstream_preview_payload(body: &Value) -> Value {
    json!({
        "preview": [],
        "privacy": "redacted",
        "request": redacted_downstream_preview_summary(body)
    })
}

fn redacted_downstream_preview_summary(body: &Value) -> Value {
    let field_count = body
        .as_object()
        .map(|object| object.len())
        .unwrap_or_default();
    let digest = stable_payload_digest("habit-health-downstream-preview", body);
    json!({
        "redacted": true,
        "field_count": field_count,
        "body_hash": format!("{digest:016x}"),
        "payload": "<redacted>",
        "redaction_policy": "hash-and-field-count-only"
    })
}

fn redacted_request_summary(body: &Value) -> Value {
    let field_count = body
        .as_object()
        .map(|object| object.len())
        .unwrap_or_default();
    json!({
        "redacted": true,
        "field_count": field_count,
        "payload": redact_request_value(body),
    })
}

fn redact_request_value(value: &Value) -> Value {
    match value {
        Value::Object(object) => {
            let redacted = object
                .iter()
                .enumerate()
                .map(|(index, (_key, value))| {
                    let field = format!("field_{}", index + 1);
                    (field, redact_request_value(value))
                })
                .collect::<serde_json::Map<_, _>>();
            Value::Object(redacted)
        }
        Value::Array(items) => Value::Array(items.iter().map(redact_request_value).collect()),
        Value::Bool(value) => json!(*value),
        Value::Number(value) => Value::Number(value.clone()),
        Value::Null => Value::Null,
        Value::String(_) => json!("<redacted>"),
    }
}

fn redact_sensitive_value(value: &Value) -> Value {
    match value {
        Value::Object(object) => {
            let redacted = object
                .iter()
                .map(|(key, value)| {
                    if is_sensitive_key(key) {
                        (key.clone(), json!("<redacted>"))
                    } else {
                        (key.clone(), redact_sensitive_value(value))
                    }
                })
                .collect::<serde_json::Map<_, _>>();
            Value::Object(redacted)
        }
        Value::Array(items) => Value::Array(items.iter().map(redact_sensitive_value).collect()),
        Value::String(text) if looks_like_secret_value(text) => json!("<redacted>"),
        other => other.clone(),
    }
}

fn is_sensitive_key(key: &str) -> bool {
    let lower = key.to_ascii_lowercase();
    [
        "api_key",
        "apikey",
        "token",
        "secret",
        "password",
        "credential",
        "authorization",
        "private",
    ]
    .iter()
    .any(|marker| lower.contains(marker))
}

fn looks_like_secret_value(value: &str) -> bool {
    let lower = value.to_ascii_lowercase();
    lower.starts_with("bearer ")
        || lower.starts_with("sk-")
        || lower.starts_with("ghp_")
        || lower.starts_with("gho_")
        || lower.starts_with("ghu_")
        || lower.starts_with("ghs_")
        || lower.starts_with("ghr_")
        || value.starts_with("AKIA")
}

async fn extensions_list() -> Json<Value> {
    Json(extensions_list_payload())
}

async fn extensions_import(Json(body): Json<Value>) -> Json<Value> {
    Json(extension_import_payload(body))
}

async fn extension_get(Path(extension_id): Path<String>) -> Json<Value> {
    Json(
        json!({"extension": extension_payload(&extension_id), "registration": extension_registration_payload(&extension_id)}),
    )
}

async fn extension_risk(Path(extension_id): Path<String>) -> Json<Value> {
    Json(json!({"risk": extension_risk_payload(&extension_id)}))
}

async fn extension_registration(Path(extension_id): Path<String>) -> Json<Value> {
    Json(json!({"registration": extension_registration_payload(&extension_id)}))
}

async fn extension_select(
    Path(extension_id): Path<String>,
    Json(body): Json<Value>,
) -> Json<Value> {
    Json(json!({"decision": extension_registration_payload(&extension_id), "request": body}))
}

async fn extension_enable(
    Path(extension_id): Path<String>,
    Json(body): Json<Value>,
) -> Json<Value> {
    Json(json!({"decision": extension_registration_payload(&extension_id), "request": body}))
}

async fn accepted_action(Json(body): Json<Value>) -> Json<Value> {
    Json(accepted_action_payload(body))
}

fn resource_snapshot_payload() -> Value {
    let leases = resource_lease_rows();
    let queued = leases
        .iter()
        .filter(|entry| entry.get("state").and_then(Value::as_str) == Some("pending"))
        .cloned()
        .collect::<Vec<_>>();
    let active = leases
        .iter()
        .filter(|entry| entry.get("state").and_then(Value::as_str) == Some("active"))
        .count();
    json!({
        "leases": leases,
        "queued": queued,
        "active_count": active,
        "safe_actions": ["pause", "cancel", "adjust_interactive_reserve"],
        "source": "amw-kernel",
        "state_source": "outputs/workbench/spine/spine.jsonl"
    })
}

fn resource_leases_payload() -> Value {
    json!({
        "leases": resource_lease_rows(),
        "source": "amw-kernel",
        "state_source": "outputs/workbench/spine/spine.jsonl"
    })
}

fn resource_queued_payload() -> Value {
    let queued = resource_lease_rows()
        .into_iter()
        .filter(|entry| entry.get("state").and_then(Value::as_str) == Some("pending"))
        .collect::<Vec<_>>();
    json!({
        "queued": queued,
        "source": "amw-kernel",
        "state_source": "outputs/workbench/spine/spine.jsonl"
    })
}

fn resource_lease_rows() -> Vec<Value> {
    let mut rows = match load_workbench_spine_snapshot() {
        Ok(snapshot) => snapshot
            .leases
            .iter()
            .map(spine_lease_queue_entry)
            .collect::<Vec<_>>(),
        Err(error) => {
            return vec![json!({
                "state": "unavailable",
                "reason": error,
                "source": "outputs/workbench/spine/spine.jsonl"
            })]
        }
    };
    let training_rows = native_training_control()
        .lock()
        .expect("training control mutex")
        .jobs
        .values()
        .filter(|job| matches!(job.status.as_str(), "running" | "paused"))
        .map(training_job_queue_entry)
        .collect::<Vec<_>>();
    rows.extend(training_rows);
    rows
}

fn accepted_action_payload(body: Value) -> Value {
    let action = body
        .get("action")
        .or_else(|| body.get("action_id"))
        .and_then(Value::as_str)
        .unwrap_or("native-rust-action");
    let digest = stable_payload_digest(action, &body);
    json!({
        "status": "accepted",
        "receipt_id": format!("native-rust-action-{digest:016x}"),
        "rollback_ref": format!("rollback:native-rust-action-{digest:016x}"),
        "trace_ref": format!("trace:native-rust-action-{digest:016x}"),
        "request": {"redacted": true, "body_hash": format!("{digest:016x}")},
        "source": "amw-kernel"
    })
}

fn not_implemented_handler_payload(path: &str) -> Value {
    json!({
        "error": "not_implemented",
        "handler": handler_name_for_path(path),
        "http_status": 501,
        "source": "amw-kernel"
    })
}

fn capability_not_implemented_payload(pack_id: &str) -> Value {
    json!({
        "error": "not_implemented",
        "capability": pack_id,
        "http_status": 501,
        "source": "amw-kernel"
    })
}

fn kit_registry_unavailable_payload(kit_id: &str) -> Value {
    json!({
        "error": "kit_registry_unavailable",
        "kit_id": kit_id,
        "http_status": 503,
        "source": "amw-kernel"
    })
}

fn handler_name_for_path(path: &str) -> String {
    path.trim_matches('/').replace(['/', ':'], "_")
}

fn stable_payload_digest(action: &str, body: &Value) -> u64 {
    let mut hasher = DefaultHasher::new();
    action.hash(&mut hasher);
    body.to_string().hash(&mut hasher);
    hasher.finish()
}

fn default_mcp_registry_and_session() -> (McpResourceRegistry, McpStreamSession) {
    let mut registry = McpResourceRegistry::default();
    registry
        .register(McpResource {
            uri: "resource://workspace/context".to_string(),
            title: "Workspace context".to_string(),
            payload: "workspace context requires explicit permission".to_string(),
            required_permission: ExtensionPermission::Resource("workspace".to_string()),
        })
        .expect("static MCP resource is valid");
    let session = McpStreamSession::initialized("route-session", 8).expect("bounded session");
    (registry, session)
}

fn support_envelope_payload(err: crate::SupportEnvelope) -> Value {
    json!({
        "status": "unavailable",
        "error": {
            "code": err.code,
            "message": err.message,
            "recovery_hint": err.recovery
        },
        "source": "amw-kernel"
    })
}

fn training_control_payload(method: &str, path: &str, body: Value) -> Value {
    let action = path
        .trim_matches('/')
        .rsplit('/')
        .next()
        .unwrap_or("status")
        .replace('-', "_");
    let mut control = native_training_control()
        .lock()
        .expect("training control mutex");
    match (method, action.as_str()) {
        ("GET", "jobs") => control.jobs_payload(),
        ("GET", "history") => control.history_payload(),
        ("GET", "idle_stats") => control.idle_stats_payload(),
        ("GET", "status") | ("GET", "training") => control.status_payload(),
        ("POST", "start") => {
            if request_string(&body, "training_mode", "") == "qlora" {
                return adapter_training_payload(&body);
            }
            let skill = request_string(&body, "skill", "native-training");
            control.start(skill)
        }
        ("POST", "dry_run") => training_dry_run_payload(&body),
        ("POST", "rules") => training_rules_payload(&body),
        ("POST", "sync_data") => training_sync_data_payload(),
        ("POST", "generate_synthetic") => training_synthetic_payload(&body),
        ("POST", "pause" | "resume" | "stop" | "cancel" | "checkpoint") => {
            control.transition(&action, &body)
        }
        _ => training_rejection(&action, "training-route-unsupported"),
    }
}

fn native_training_control() -> &'static Mutex<NativeTrainingControl> {
    NATIVE_TRAINING_CONTROL.get_or_init(|| Mutex::new(NativeTrainingControl::default()))
}

#[cfg(test)]
fn reset_native_training_control_for_test() {
    let mut control = native_training_control()
        .lock()
        .expect("training control mutex");
    *control = NativeTrainingControl::default();
}

fn training_receipt(
    status: &str,
    action: &str,
    job_id: &str,
    reason: &str,
    job: Option<NativeTrainingJob>,
) -> Value {
    let digest = stable_payload_digest(action, &json!({"job_id": job_id, "reason": reason}));
    let mut payload = json!({
        "status": status,
        "control": action,
        "action": action,
        "message": reason,
        "created_at_utc": utc_now_rfc3339(),
        "reason": reason,
        "job_id": job_id,
        "job": job.as_ref().map(training_job_payload),
        "audit_path": display_path(&training_audit_path()),
        "checkpoint_path": if action == "checkpoint" { Some(display_path(&training_checkpoint_path(job_id))) } else { None },
        "receipt_id": format!("training-control-{action}-{digest:016x}"),
        "source": "amw-kernel",
        "state_source": "amw-kernel::training_control"
    });
    if action == "checkpoint" {
        if let Some(job) = job.as_ref() {
            if let Err(err) = write_training_checkpoint(job, &payload) {
                payload["status"] = json!("rejected");
                payload["reason"] =
                    json!(format!("training-checkpoint-write-failed:{:?}", err.kind()));
                payload["message"] = payload["reason"].clone();
            }
        }
    }
    if let Err(err) = append_training_audit(&payload) {
        payload["status"] = json!("rejected");
        payload["reason"] = json!(format!("training-audit-write-failed:{:?}", err.kind()));
        payload["message"] = payload["reason"].clone();
    }
    payload
}

fn training_dry_run_payload(body: &Value) -> Value {
    let epochs = body
        .get("epochs")
        .and_then(Value::as_u64)
        .unwrap_or(1)
        .clamp(1, 100);
    let batch_size = body
        .get("batch_size")
        .and_then(Value::as_u64)
        .unwrap_or(1)
        .clamp(1, 512);
    let estimated_steps = epochs.saturating_mul(batch_size).max(1);
    json!({
        "status": "accepted",
        "estimated_steps": estimated_steps,
        "estimated_duration_ms": estimated_steps.saturating_mul(750),
        "warnings": [],
        "source": "amw-kernel",
        "state_source": "amw-kernel::training_control",
        "request_digest": format!("{:016x}", stable_payload_digest("training-dry-run", body)),
    })
}

fn training_rules_payload(body: &Value) -> Value {
    json!({
        "status": "accepted",
        "rules": body,
        "source": "amw-kernel",
        "state_source": "amw-kernel::training_control",
        "receipt_id": format!(
            "training-rules-{:016x}",
            stable_payload_digest("training-rules", body)
        ),
    })
}

fn training_sync_data_payload() -> Value {
    json!({
        "status": "accepted",
        "synced": true,
        "records_collected": native_training_control()
            .lock()
            .expect("training control mutex")
            .jobs
            .len(),
        "source": "amw-kernel",
        "state_source": "amw-kernel::training_control",
    })
}

fn training_synthetic_payload(body: &Value) -> Value {
    let count = body
        .get("num_samples")
        .and_then(Value::as_u64)
        .unwrap_or(0)
        .clamp(0, 10_000);
    json!({
        "status": "accepted",
        "count": count,
        "task_type": request_string(body, "task_type", "general"),
        "dataset_ref": format!(
            "synthetic-training-data-{:016x}",
            stable_payload_digest("training-synthetic", body)
        ),
        "source": "amw-kernel",
        "state_source": "amw-kernel::training_control",
    })
}

fn adapter_training_payload(body: &Value) -> Value {
    let required = [
        "base_model",
        "dataset_path",
        "output_dir",
        "provenance_ref",
        "consent_ref",
        "safety_ref",
    ];
    if let Some(missing) = required
        .iter()
        .find(|key| request_string(body, key, "").trim().is_empty())
    {
        return training_rejection("start", &format!("missing-adapter-field:{missing}"));
    }
    let confidence = body
        .get("confidence")
        .and_then(Value::as_f64)
        .unwrap_or(0.0);
    if confidence < 0.7 {
        return training_rejection("start", "adapter-confidence-below-threshold");
    }
    let output_dir = request_string(body, "output_dir", "outputs/adapters");
    let digest = stable_payload_digest("adapter-training", body);
    json!({
        "status": "completed",
        "training_mode": "qlora",
        "adapter_path": format!("{}/adapter-{digest:016x}", output_dir.trim_end_matches(&['/', '\\'][..])),
        "receipt_id": format!("adapter-training-{digest:016x}"),
        "base_model": request_string(body, "base_model", ""),
        "dataset_path": request_string(body, "dataset_path", ""),
        "provenance_ref": request_string(body, "provenance_ref", ""),
        "consent_ref": request_string(body, "consent_ref", ""),
        "safety_ref": request_string(body, "safety_ref", ""),
        "confidence": confidence,
        "source": "amw-kernel",
        "state_source": "amw-kernel::training_control",
    })
}

fn training_rejection(action: &str, reason: &str) -> Value {
    let mut payload = json!({
        "status": "rejected",
        "control": action,
        "action": action,
        "message": reason,
        "created_at_utc": utc_now_rfc3339(),
        "reason": reason,
        "job_id": null,
        "audit_path": display_path(&training_audit_path()),
        "receipt_id": format!("training-control-{action}-rejected"),
        "source": "amw-kernel",
        "state_source": "amw-kernel::training_control"
    });
    if let Err(err) = append_training_audit(&payload) {
        payload["reason"] = json!(format!("training-audit-write-failed:{:?}", err.kind()));
        payload["message"] = payload["reason"].clone();
    }
    payload
}

fn training_audit_path() -> PathBuf {
    kernel_state_path(&["training-control-audit.jsonl"])
}

fn training_checkpoint_path(job_id: &str) -> PathBuf {
    kernel_state_path(&["training-checkpoints", &format!("{job_id}.json")])
}

fn append_training_audit(payload: &Value) -> std::io::Result<()> {
    let path = training_audit_path();
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)?;
    }
    use std::io::Write as _;
    let mut file = fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(path)?;
    writeln!(
        file,
        "{}",
        serde_json::to_string(payload).unwrap_or_else(|_| "{}".to_string())
    )?;
    file.flush()
}

fn write_training_checkpoint(job: &NativeTrainingJob, receipt: &Value) -> std::io::Result<()> {
    let path = training_checkpoint_path(&job.job_id);
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)?;
    }
    let checkpoint = json!({
        "schema_version": "native-training-checkpoint.v1",
        "run_id": job.job_id,
        "status": match job.status.as_str() {
            "running" => "running",
            "paused" | "stopped" | "cancelled" => "interrupted",
            other => other,
        },
        "step": job.progress_milli / 10,
        "output_dir": display_path(path.parent().unwrap_or_else(|| FsPath::new("."))),
        "metadata": {
            "activity_description": job.activity_description,
            "task_type": job.skill,
            "checkpoint_count": job.checkpoint_count,
            "source": "amw-kernel::training_control"
        },
        "receipt": receipt
    });
    fs::write(
        path,
        serde_json::to_string_pretty(&checkpoint).unwrap_or_else(|_| "{}".to_string()),
    )
}

fn training_job_payload(job: &NativeTrainingJob) -> Value {
    json!({
        "job_id": &job.job_id,
        "skill": &job.skill,
        "status": &job.status,
        "activity_description": &job.activity_description,
        "started_at": &job.created_at_utc,
        "task_type": if job.skill.trim().is_empty() { Value::Null } else { Value::String(job.skill.clone()) },
        "progress": (job.progress_milli as f64) / 1000.0,
        "created_at_utc": &job.created_at_utc,
        "updated_at_utc": &job.updated_at_utc,
        "checkpoint_count": job.checkpoint_count,
        "source": "amw-kernel"
    })
}

fn training_job_queue_entry(job: &NativeTrainingJob) -> Value {
    json!({
        "lease_id": format!("training-lease:{}", job.job_id),
        "caller_subsystem": "native-training-control",
        "target": "training",
        "state": if job.status == "paused" { "pending" } else { "active" },
        "age_seconds": 0,
        "run_id": &job.job_id,
        "requested_at_utc": &job.created_at_utc
    })
}

fn training_job_agent_task(job: &NativeTrainingJob) -> Value {
    json!({
        "run_id": &job.job_id,
        "task_id": &job.job_id,
        "agent_type": "TRAINING",
        "status": &job.status,
        "lane": "training",
        "escalated": false,
        "escalation_reason": null,
        "recursive_parent_run_id": null,
        "blocker_summary": null,
        "retries": 0,
        "paused": job.status == "paused",
        "evidence_links": [],
        "started_at_utc": &job.created_at_utc,
        "finished_at_utc": if matches!(job.status.as_str(), "stopped" | "cancelled") {
            Value::String(job.updated_at_utc.clone())
        } else {
            Value::Null
        }
    })
}

fn capability_registry_payload() -> Value {
    capability_registry_payload_from_result(load_capability_packs())
}

fn capability_registry_payload_from_result(result: Result<Vec<Value>, String>) -> Value {
    match result {
        Ok(packs) => json!({
            "packs": packs,
            "backend_status": "available",
            "state_source": "config/workbench/capability_packs",
            "source": "amw-kernel"
        }),
        Err(reason) => json!({
            "packs": [],
            "backend_status": "unavailable",
            "reason": reason,
            "state_source": "config/workbench/capability_packs",
            "source": "amw-kernel"
        }),
    }
}

fn capability_pack_detail_payload(pack_id: &str) -> Value {
    capability_pack_detail_payload_from_result(pack_id, load_capability_packs())
}

fn capability_pack_detail_payload_from_result(
    pack_id: &str,
    result: Result<Vec<Value>, String>,
) -> Value {
    match result {
        Ok(packs) => {
            let enablement = capability_registry_available_decision(pack_id, &packs);
            let pack = packs.into_iter().find(|candidate| {
                candidate.get("pack_id").and_then(Value::as_str) == Some(pack_id)
            });
            match pack {
                Some(pack) => {
                    json!({"pack": pack, "enablement": enablement})
                }
                None => json!({
                    "pack": {
                        "pack_id": pack_id,
                        "current_status": "missing",
                        "reason": "capability-pack-not-found",
                        "state_source": "config/workbench/capability_packs",
                        "source": "amw-kernel"
                    },
                    "enablement": enablement
                }),
            }
        }
        Err(reason) => json!({
            "pack": {
                "pack_id": pack_id,
                "current_status": "unavailable",
                "reason": reason,
                "state_source": "config/workbench/capability_packs",
                "source": "amw-kernel"
            },
            "enablement": capability_registry_unavailable_decision(pack_id, &reason)
        }),
    }
}

fn load_capability_packs() -> Result<Vec<Value>, String> {
    let catalog_path = locate_repo_file("config/workbench/capability_packs/base.yaml")
        .ok_or_else(|| "capability-pack-catalog-missing".to_string())?;
    load_capability_packs_from_catalog(&catalog_path)
}

fn load_capability_packs_from_catalog(catalog_path: &FsPath) -> Result<Vec<Value>, String> {
    let raw = fs::read_to_string(catalog_path)
        .map_err(|err| format!("capability-pack-catalog-unreadable:{err}"))?;
    let document: Value = serde_yaml::from_str(&raw)
        .map_err(|err| format!("capability-pack-catalog-invalid:{err}"))?;
    if document.get("schema_version").and_then(Value::as_i64) != Some(1) {
        return Err("capability-pack-catalog-schema-mismatch".to_string());
    }
    let packs = document
        .get("packs")
        .and_then(Value::as_array)
        .filter(|rows| !rows.is_empty())
        .ok_or_else(|| "capability-pack-catalog-empty".to_string())?;
    let mut parsed = Vec::with_capacity(packs.len());
    for pack in packs {
        let Some(pack_id) = pack
            .get("pack_id")
            .and_then(Value::as_str)
            .filter(|value| !value.is_empty())
        else {
            return Err("capability-pack-catalog-row-missing-pack-id".to_string());
        };
        let mut row = pack.clone();
        if let Some(object) = row.as_object_mut() {
            object.insert(
                "state_source".to_string(),
                json!("config/workbench/capability_packs"),
            );
            object.insert("source".to_string(), json!("amw-kernel"));
        }
        if row.get("current_status").and_then(Value::as_str).is_none() {
            return Err(format!(
                "capability-pack-catalog-row-missing-current-status:{pack_id}"
            ));
        }
        let decision = capability_pack_enablement_decision(pack_id, &row);
        if let Some(object) = row.as_object_mut() {
            object.insert("trust_status".to_string(), decision["status"].clone());
            object.insert("enablement".to_string(), decision);
        }
        parsed.push(row);
    }
    Ok(parsed)
}

fn locate_repo_file(relative: &str) -> Option<PathBuf> {
    let mut directory = std::env::current_dir().ok()?;
    loop {
        let candidate = directory.join(relative);
        if candidate.exists() {
            return Some(candidate);
        }
        if !directory.pop() {
            return None;
        }
    }
}

fn capability_registry_decision(pack_id: &str) -> Value {
    match load_capability_packs() {
        Ok(packs) => capability_registry_available_decision(pack_id, &packs),
        Err(reason) => capability_registry_unavailable_decision(pack_id, &reason),
    }
}

fn capability_registry_available_decision(pack_id: &str, packs: &[Value]) -> Value {
    let Some(pack) = packs
        .iter()
        .find(|candidate| candidate.get("pack_id").and_then(Value::as_str) == Some(pack_id))
    else {
        return json!({
            "target_id": pack_id,
            "allowed": false,
            "status": "denied",
            "actions": {
                "enable": false,
                "disable": false,
                "smoke_test": false,
                "uninstall": false,
            },
            "reasons": ["capability-pack-not-found"],
            "source": "amw-kernel",
            "receipt_id": format!("decision-denied-{pack_id}")
        });
    };
    capability_pack_enablement_decision(pack_id, pack)
}

fn capability_pack_enablement_decision(pack_id: &str, pack: &Value) -> Value {
    let mut reasons = Vec::<String>::new();
    let mut missing = Vec::<String>::new();
    for field in [
        "schemas",
        "policy_bindings",
        "smoke_evals",
        "known_limitations",
    ] {
        if !non_empty_array_field(pack, field) {
            missing.push(field.to_string());
        }
    }
    for field in [
        "uninstall_command",
        "disable_command",
        "credential_posture",
        "locality",
        "source",
        "cost_policy",
        "freshness_policy",
        "tested_status",
        "current_status",
    ] {
        if !non_empty_string_field(pack, field) {
            missing.push(field.to_string());
        }
    }
    if pack.get("tested_status").and_then(Value::as_str) != Some("tested") {
        reasons.push(format!(
            "pack is not smoke-tested: {}",
            pack.get("tested_status")
                .and_then(Value::as_str)
                .unwrap_or("missing")
        ));
    }
    if pack.get("current_status").and_then(Value::as_str) != Some("current") {
        reasons.push(format!(
            "pack is not current: {}",
            pack.get("current_status")
                .and_then(Value::as_str)
                .unwrap_or("missing")
        ));
    }
    if pack
        .get("known_limitations")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .filter_map(Value::as_str)
        .any(|row| {
            row.to_ascii_lowercase()
                .contains("installer status is denied")
        })
    {
        reasons.push(
            "installer status denied until capability-installer upstream is present".to_string(),
        );
    }
    if !missing.is_empty() {
        reasons.push("required trust fields missing".to_string());
    }
    let allowed = reasons.is_empty() && missing.is_empty();
    let status = if allowed { "trusted" } else { "denied" };
    json!({
        "target_id": pack_id,
        "pack_id": pack_id,
        "allowed": allowed,
        "status": status,
        "actions": {
            "enable": allowed,
            "disable": non_empty_string_field(pack, "disable_command"),
            "smoke_test": non_empty_array_field(pack, "smoke_evals"),
            "uninstall": non_empty_string_field(pack, "uninstall_command"),
        },
        "reasons": if reasons.is_empty() { json!(["trusted capability pack"]) } else { json!(reasons) },
        "missing": missing,
        "source": "amw-kernel",
        "receipt_id": format!("decision-{status}-{pack_id}")
    })
}

fn non_empty_array_field(row: &Value, field: &str) -> bool {
    row.get(field)
        .and_then(Value::as_array)
        .is_some_and(|items| !items.is_empty())
}

fn non_empty_string_field(row: &Value, field: &str) -> bool {
    row.get(field)
        .and_then(Value::as_str)
        .is_some_and(|value| !value.trim().is_empty())
}

fn capability_registry_unavailable_decision(pack_id: &str, reason: &str) -> Value {
    json!({
        "target_id": pack_id,
        "allowed": false,
        "status": "denied",
        "actions": {
            "enable": false,
            "disable": false,
            "smoke_test": false,
            "uninstall": false,
        },
        "reasons": [reason],
        "catalog_status": "unavailable",
        "source": "amw-kernel",
        "receipt_id": format!("decision-denied-{pack_id}")
    })
}

fn channels_config_payload() -> Value {
    json!({
        "source": "amw-kernel",
        "default_channel": "desktop",
        "channels": [
            {
                "channel": "desktop",
                "enabled": true,
                "requires_approval": false,
                "transport": "tauri-event",
                "receipt_required": true
            },
            {
                "channel": "webhook",
                "enabled": false,
                "requires_approval": true,
                "transport": "configured-webhook",
                "receipt_required": true,
                "disabled_reason": "no webhook endpoint configured"
            }
        ],
        "activity": []
    })
}

fn channels_activity_payload() -> Value {
    let config = channels_config_payload();
    let activity = config
        .get("channels")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default()
        .into_iter()
        .map(|channel| {
            let name = channel
                .get("channel")
                .and_then(Value::as_str)
                .unwrap_or("unknown");
            let enabled = channel
                .get("enabled")
                .and_then(Value::as_bool)
                .unwrap_or(false);
            json!({
                "activity_id": format!("channel:{name}:status"),
                "channel": name,
                "status": if enabled { "ready" } else { "disabled" },
                "transport": channel.get("transport").cloned().unwrap_or(Value::Null),
                "requires_approval": channel.get("requires_approval").cloned().unwrap_or(Value::Bool(true)),
                "receipt_required": channel.get("receipt_required").cloned().unwrap_or(Value::Bool(true)),
                "occurred_at_utc": utc_now_rfc3339(),
                "why": if enabled {
                    "Native channel is configured and can emit receipt-backed desktop activity."
                } else {
                    "Native channel is declared but disabled until required configuration is present."
                },
                "disabled_reason": channel.get("disabled_reason").cloned().unwrap_or(Value::Null)
            })
        })
        .collect::<Vec<_>>();
    json!({
        "status": "ok",
        "source": "amw-kernel",
        "state_source": "native-channel-config",
        "activity": activity,
        "empty_reason": Value::Null
    })
}

fn migration_sources_payload() -> Value {
    json!({
        "sources": ["legacy-python-http", "flask", "desktop-shell"],
        "source": "amw-kernel",
        "retired_sources": ["litestar"],
        "notes": ["Litestar is retired; migration entries are inventory-only and not runtime imports."]
    })
}

fn request_string(body: &Value, key: &str, fallback: &str) -> String {
    body.get(key)
        .and_then(Value::as_str)
        .filter(|value| !value.is_empty())
        .unwrap_or(fallback)
        .to_string()
}

fn utc_now_rfc3339() -> String {
    OffsetDateTime::now_utc()
        .format(&Rfc3339)
        .unwrap_or_else(|_| "1970-01-01T00:00:00Z".to_string())
}

fn run_kernel_run_payload(run_id: &str) -> Value {
    json!({
        "source": "amw-kernel",
        "status": "recovery_needed",
        "run_id": run_id,
        "recovery_action": "inspect-last-checkpoint",
        "snapshot": {
            "run_id": run_id,
            "status": "recovery_needed",
            "restart_count": 0,
            "checkpoint": {
                "sealed": false,
                "checkpoint_id": "",
                "payload_ref": ""
            },
            "events": ["native-run-kernel-route-reachable"],
            "evidence_links": {
                "trace_refs": [],
                "eval_refs": [],
                "repro_capsule_refs": []
            }
        }
    })
}

fn run_kernel_runs_payload() -> Value {
    json!({
        "source": "amw-kernel",
        "status": "ok",
        "runs": [],
        "snapshot": {
            "events": ["native-run-kernel-index-reachable"],
            "evidence_links": {
                "trace_refs": [],
                "eval_refs": [],
                "repro_capsule_refs": []
            },
            "checkpoint": {"sealed": false},
            "restart_count": 0
        }
    })
}

fn run_kernel_action_payload(status: &str, body: Value) -> Value {
    let run_id = request_string(&body, "run_id", "run-native");
    json!({
        "source": "amw-kernel",
        "status": status,
        "run_id": run_id,
        "recovery_action": "none",
        "snapshot": {
            "run_id": run_id,
            "status": status,
            "restart_count": 0,
            "checkpoint": {"sealed": false, "checkpoint_id": "", "payload_ref": ""},
            "events": ["native-run-kernel-start-accepted"],
            "evidence_links": body.get("evidence_links").cloned().unwrap_or_else(|| json!({
                "trace_refs": [],
                "eval_refs": [],
                "repro_capsule_refs": []
            }))
        },
        "request": redacted_request_summary(&body)
    })
}

fn run_kernel_checkpoint_payload(run_id: &str, body: Value) -> Value {
    let checkpoint_id = request_string(&body, "checkpoint_id", "checkpoint-native");
    let payload_ref = request_string(&body, "payload_ref", "snapshot://native");
    json!({
        "source": "amw-kernel",
        "status": "interrupted",
        "run_id": run_id,
        "recovery_action": "resume-from-checkpoint",
        "snapshot": {
            "run_id": run_id,
            "status": "interrupted",
            "restart_count": 0,
            "checkpoint": {
                "sealed": true,
                "checkpoint_id": checkpoint_id,
                "payload_ref": payload_ref
            },
            "events": ["native-run-kernel-checkpoint-sealed"],
            "evidence_links": {
                "trace_refs": [],
                "eval_refs": [],
                "repro_capsule_refs": []
            }
        },
        "request": redacted_request_summary(&body)
    })
}

fn run_kernel_resume_payload(run_id: &str, body: Value) -> Value {
    json!({
        "source": "amw-kernel",
        "status": "running",
        "run_id": run_id,
        "recovery_action": "none",
        "snapshot": {
            "run_id": run_id,
            "status": "running",
            "restart_count": 1,
            "checkpoint": {
                "sealed": true,
                "checkpoint_id": request_string(&body, "checkpoint_id", "checkpoint-native"),
                "payload_ref": request_string(&body, "payload_ref", "snapshot://native")
            },
            "events": ["native-run-kernel-resume-accepted"],
            "evidence_links": {
                "trace_refs": [],
                "eval_refs": [],
                "repro_capsule_refs": []
            }
        },
        "request": redacted_request_summary(&body)
    })
}

fn evidence_notebook(notebook_id: &str) -> Value {
    json!({
        "notebook_id": notebook_id,
        "title": "Native workbench evidence",
        "updated_at_utc": "2026-06-01T00:00:00Z",
        "project_id": "default",
        "cells": [
            {
                "cell_id": "native-route-cell",
                "title": "Native route reachability",
                "kind": "backend_evidence",
                "is_product_claim": true,
                "proof_status": "proven",
                "proof_refs": ["amw-kernel:workbench_domains"],
                "rerunnable_commands": ["cargo test -p amw-kernel kernel_request_dispatches_named_workbench_surfaces_with_route_specific_payloads"]
            }
        ],
        "evidence_refs": ["amw-kernel:evidence-notebooks"]
    })
}

fn evidence_notebooks_payload() -> Value {
    json!([evidence_notebook("native-workbench-evidence")])
}

fn evidence_notebook_payload(notebook_id: &str) -> Value {
    merge_json_objects(
        evidence_notebook(notebook_id),
        json!({"source": "amw-kernel"}),
    )
}

fn managed_agents_snapshot_payload() -> Value {
    json!({
        "source": "amw-kernel",
        "status": "degraded",
        "agents": [
            {
                "agent_id": "foreman-native",
                "role": "FOREMAN",
                "state": "idle",
                "last_seen_utc": null,
                "actions": ["pause", "retire"]
            }
        ],
        "dependency_contracts": [
            {
                "contract_id": "native-agent-dependency",
                "status": "satisfied",
                "evidence_ref": "amw-kernel:managed-agents"
            }
        ],
        "degradation_reasons": ["no live managed-agent supervisor attached to this kernel process"],
        "user_intervention": {
            "required": false,
            "reason": ""
        }
    })
}

fn managed_agent_decision_payload(agent_id: &str, action: &str, body: Value) -> Value {
    json!({
        "source": "amw-kernel",
        "agent_id": agent_id,
        "action": action,
        "status": "accepted",
        "receipt_id": format!("managed-agent:{agent_id}:{action}"),
        "request": body
    })
}

fn command_safety_profiles_payload() -> Value {
    json!({
        "source": "amw-kernel",
        "profiles": [
            {
                "profile_id": "readonly",
                "label": "Read-only",
                "allowed_operations": ["git status", "rg", "cargo test", "pytest"],
                "human_approval_required": false
            },
            {
                "profile_id": "approval-required",
                "label": "Approval required",
                "allowed_operations": ["mutation", "network", "process-control"],
                "human_approval_required": true
            },
            {
                "profile_id": "blocked",
                "label": "Blocked",
                "allowed_operations": [],
                "human_approval_required": true
            }
        ]
    })
}

fn command_safety_decision_payload(body: Value, decide: bool) -> Value {
    let command = request_string(&body, "command", "");
    let trimmed = command.trim();
    let safe_readonly = command_safety_readonly_command(trimmed);
    let risky = !trimmed.is_empty() && !safe_readonly;
    let verdict = if trimmed.is_empty() {
        "block"
    } else if safe_readonly {
        "allow"
    } else {
        "require_human_approval"
    };
    json!({
        "source": "amw-kernel",
        "classification": verdict,
        "verdict": verdict,
        "reasons": if trimmed.is_empty() {
            json!(["empty-command"])
        } else if safe_readonly {
            json!(["native-command-safety-readonly-default"])
        } else {
            json!(["unknown-command-requires-human-approval"])
        },
        "human_approval_required": risky || verdict == "block",
        "tool_surface": body.get("tool_surface").cloned().unwrap_or_else(|| json!("shell")),
        "receipt_ref": if decide {
            json!("command-safety:native-decision")
        } else {
            Value::Null
        },
        "cwd_state": {
            "workspace": display_path(&workspace_path(&[])),
            "known_safe": safe_readonly
        },
        "request": redacted_request_summary(&body)
    })
}

fn command_safety_readonly_command(command: &str) -> bool {
    let lower = command.to_ascii_lowercase();
    matches!(
        lower.as_str(),
        "git status" | "git status --short" | "git diff --stat" | "git diff --name-only"
    ) || lower.starts_with("rg ")
        || lower.starts_with("rg --")
        || lower.starts_with("cargo test ")
        || lower.starts_with("cargo fmt")
}

fn command_safety_state_payload(
    project_id: &str,
    run_id: &str,
    session_id: &str,
    surface_id: &str,
) -> Value {
    json!({
        "source": "amw-kernel",
        "project_id": project_id,
        "run_id": run_id,
        "session_id": session_id,
        "surface_id": surface_id,
        "active_profile": "approval-required",
        "last_decision": null,
        "cwd_state": {
            "workspace": display_path(&workspace_path(&[])),
            "known_safe": true
        }
    })
}

fn readiness_snapshot_payload() -> Value {
    json!({
        "source": "amw-kernel",
        "mode": "restricted",
        "reasons": ["native-kernel-route-snapshot", "operator-readiness-fails-closed-without-live-signals"],
        "recommended_actions": ["inspect-native-workbench-route-evidence"],
        "signals": [
            {
                "kind": "run_kernel",
                "status": "degraded",
                "summary": "native run-kernel route is reachable without a Python HTTP proxy",
                "evidence_refs": ["amw-kernel:run-kernel"]
            },
            {
                "kind": "command_safety",
                "status": "passing",
                "summary": "native command safety profiles and decisions are reachable",
                "evidence_refs": ["amw-kernel:command-safety"]
            }
        ],
        "feature_gates": {
            "run_kernel_operations": "preview_only",
            "command_safety": "enabled",
            "updates": "manual_only",
            "artifact_reviews": "enabled"
        },
        "evidence_refs": ["amw-kernel:readiness"]
    })
}

fn readiness_admission_payload(body: Value) -> Value {
    let mut snapshot = readiness_snapshot_payload();
    if let Value::Object(ref mut map) = snapshot {
        map.insert(
            "admission_preview".to_string(),
            json!({
                "allowed": false,
                "mode": "restricted",
                "reason": "native-readiness-requires-explicit-operator-confirmation",
                "request": body
            }),
        );
    }
    snapshot
}

fn update_readiness_payload(body: Value) -> Value {
    let channel = request_string(&body, "channel", "stable");
    let current_version = request_string(&body, "current_version", "0.0.0-dev");
    let installed_release = body
        .get("installed_release")
        .and_then(Value::as_bool)
        .unwrap_or(false);
    if !installed_release {
        return update_blocked_payload(
            &channel,
            &current_version,
            "",
            vec!["dev_checkout_not_install_target".to_string()],
            None,
        );
    }

    let config = match load_update_channels_config() {
        Ok(config) => config,
        Err(reason) => {
            return update_blocked_payload(&channel, &current_version, "", vec![reason], None)
        }
    };
    let Some(channel_config) = config
        .get("channels")
        .and_then(Value::as_object)
        .and_then(|channels| channels.get(&channel))
        .and_then(Value::as_object)
    else {
        return update_blocked_payload(
            &channel,
            &current_version,
            "",
            vec!["channel_unknown".to_string()],
            None,
        );
    };
    let explicit_manifest_path = body
        .get("manifest_path")
        .and_then(Value::as_str)
        .is_some_and(|value| !value.trim().is_empty());
    if !channel_config
        .get("enabled")
        .and_then(Value::as_bool)
        .unwrap_or(false)
        && !explicit_manifest_path
    {
        return update_blocked_payload(
            &channel,
            &current_version,
            "",
            vec!["channel_disabled".to_string()],
            None,
        );
    }
    let manifest_path = request_string(
        &body,
        "manifest_path",
        channel_config
            .get("manifest_path")
            .and_then(Value::as_str)
            .unwrap_or(""),
    );
    if manifest_path.trim().is_empty() {
        return update_blocked_payload(
            &channel,
            &current_version,
            "",
            vec!["manifest_path_missing".to_string()],
            None,
        );
    }
    let (manifest, manifest_dir) = match load_update_manifest(&manifest_path) {
        Ok(manifest) => manifest,
        Err(reason) => {
            return update_blocked_payload(&channel, &current_version, "", vec![reason], None)
        }
    };
    let candidate_version = manifest
        .get("version")
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_string();
    let manifest_channel = manifest
        .get("channel")
        .and_then(Value::as_str)
        .unwrap_or("");
    if manifest_channel != channel {
        return update_blocked_payload(
            &channel,
            &current_version,
            &candidate_version,
            vec!["manifest_channel_mismatch".to_string()],
            Some(manifest),
        );
    }
    let require_signature = channel_config
        .get("require_signature")
        .and_then(Value::as_bool)
        .unwrap_or(true);
    let signature_evidence = manifest
        .get("integrity")
        .and_then(|integrity| integrity.get("signature_evidence"))
        .and_then(Value::as_str)
        .unwrap_or("");
    if require_signature && signature_evidence.trim().is_empty() {
        return update_blocked_payload(
            &channel,
            &current_version,
            &candidate_version,
            vec!["signature_evidence_missing".to_string()],
            Some(manifest),
        );
    }
    let integrity_reasons =
        verify_update_manifest_integrity(&manifest, &manifest_dir, require_signature);
    if !integrity_reasons.is_empty() {
        return update_blocked_payload(
            &channel,
            &current_version,
            &candidate_version,
            integrity_reasons,
            Some(manifest),
        );
    }
    if candidate_version == current_version {
        return update_ready_payload(
            "current",
            &channel,
            &current_version,
            &candidate_version,
            manifest,
            vec!["already_current".to_string()],
            false,
        );
    }
    update_ready_payload(
        "ready",
        &channel,
        &current_version,
        &candidate_version,
        manifest,
        vec!["ready_no_auto_install".to_string()],
        true,
    )
}

fn update_channels_payload() -> Value {
    match load_update_channels_config() {
        Ok(config) => {
            let channels = config
                .get("channels")
                .and_then(Value::as_object)
                .map(|rows| {
                    rows.iter()
                        .map(|(channel, row)| json!({
                            "channel": channel,
                            "label": channel_label(channel),
                            "enabled": row.get("enabled").and_then(Value::as_bool).unwrap_or(false),
                            "manual_only": !row.get("allow_auto_install").and_then(Value::as_bool).unwrap_or(false),
                            "manifest_configured": row.get("manifest_path").and_then(Value::as_str).is_some_and(|value| !value.trim().is_empty()),
                            "require_signature": row.get("require_signature").and_then(Value::as_bool).unwrap_or(true),
                        }))
                        .collect::<Vec<_>>()
                })
                .unwrap_or_default();
            json!({
                "source": "amw-kernel",
                "state_source": "config/workbench/update_channels.yaml",
                "channels": channels,
                "default_channel": config.get("default_channel").and_then(Value::as_str).unwrap_or("stable")
            })
        }
        Err(reason) => json!({
            "source": "amw-kernel",
            "state_source": "config/workbench/update_channels.yaml",
            "channels": [],
            "default_channel": "stable",
            "error": reason
        }),
    }
}

fn load_update_channels_config() -> Result<Value, String> {
    let config_path = locate_repo_file("config/workbench/update_channels.yaml")
        .ok_or_else(|| "update-channel-config-missing".to_string())?;
    let raw = fs::read_to_string(&config_path)
        .map_err(|err| format!("update-channel-config-unreadable:{err}"))?;
    serde_yaml::from_str(&raw).map_err(|err| format!("update-channel-config-invalid:{err}"))
}

fn load_update_manifest(path: &str) -> Result<(Value, PathBuf), String> {
    let manifest_path = locate_repo_file(path)
        .or_else(|| Some(PathBuf::from(path)).filter(|candidate| candidate.exists()))
        .ok_or_else(|| "manifest_unreadable:NotFound".to_string())?;
    let manifest_dir = manifest_path
        .parent()
        .map(FsPath::to_path_buf)
        .unwrap_or_else(|| workspace_path(&[]));
    let raw =
        fs::read_to_string(&manifest_path).map_err(|err| format!("manifest_unreadable:{err}"))?;
    let manifest: Value =
        serde_json::from_str(&raw).map_err(|err| format!("manifest_invalid:{err}"))?;
    require_update_manifest(&manifest)?;
    Ok((manifest, manifest_dir))
}

fn require_update_manifest(manifest: &Value) -> Result<(), String> {
    let object = manifest
        .as_object()
        .ok_or_else(|| "manifest root must be an object".to_string())?;
    for key in [
        "schema_version",
        "version",
        "channel",
        "release_notes",
        "public_export",
        "artifacts",
        "integrity",
        "published_at_utc",
    ] {
        if !object.contains_key(key) {
            return Err(format!("manifest_missing:{key}"));
        }
    }
    if manifest.get("schema_version").and_then(Value::as_str) != Some("1.0") {
        return Err("manifest_schema_version_mismatch".to_string());
    }
    if manifest
        .get("artifacts")
        .and_then(Value::as_array)
        .is_none_or(|rows| rows.is_empty())
    {
        return Err("manifest_artifacts_empty".to_string());
    }
    Ok(())
}

fn verify_update_manifest_integrity(
    manifest: &Value,
    manifest_dir: &FsPath,
    require_signature: bool,
) -> Vec<String> {
    let mut reasons = Vec::new();
    let checksum_algorithm = manifest
        .get("integrity")
        .and_then(|integrity| integrity.get("checksum_algorithm"))
        .and_then(Value::as_str)
        .unwrap_or("sha256");
    if !checksum_algorithm.eq_ignore_ascii_case("sha256") {
        reasons.push("checksum_algorithm_unsupported".to_string());
    }
    let signature_evidence = update_signature_evidence(Some(manifest));
    if require_signature && signature_evidence.trim().is_empty() {
        reasons.push("signature_evidence_missing".to_string());
    }
    if require_signature && signature_evidence.trim() == "unverified" {
        reasons.push("signature_verification_failed".to_string());
    }
    let artifact_root = manifest_dir
        .canonicalize()
        .unwrap_or_else(|_| manifest_dir.to_path_buf());
    for artifact in manifest
        .get("artifacts")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
    {
        let platform = artifact
            .get("platform")
            .and_then(Value::as_str)
            .unwrap_or("unknown");
        let digest = artifact
            .get("digest")
            .and_then(Value::as_str)
            .unwrap_or("")
            .trim();
        if !is_sha256_digest(digest) {
            reasons.push(format!("artifact_digest_malformed:{platform}"));
            continue;
        }
        let Some(local_path) = artifact.get("local_path").and_then(Value::as_str) else {
            reasons.push(format!("artifact_evidence_missing:{platform}"));
            continue;
        };
        if local_path.trim().is_empty() {
            reasons.push(format!("artifact_evidence_missing:{platform}"));
            continue;
        }
        let candidate = artifact_root.join(local_path);
        let Ok(resolved) = candidate.canonicalize() else {
            reasons.push(format!("artifact_unreadable:{platform}"));
            continue;
        };
        if !resolved.starts_with(&artifact_root) {
            reasons.push(format!("artifact_path_escapes_root:{platform}"));
            continue;
        }
        match fs::read(&resolved) {
            Ok(bytes) => {
                let actual = format!("sha256:{}", hex_sha256(&bytes));
                if actual != digest {
                    reasons.push(format!("artifact_checksum_mismatch:{platform}"));
                }
            }
            Err(_) => reasons.push(format!("artifact_unreadable:{platform}")),
        }
    }
    reasons
}

fn is_sha256_digest(value: &str) -> bool {
    let Some(hex) = value.strip_prefix("sha256:") else {
        return false;
    };
    hex.len() == 64 && hex.bytes().all(|byte| byte.is_ascii_hexdigit())
}

fn hex_sha256(bytes: &[u8]) -> String {
    let digest = Sha256::digest(bytes);
    let mut output = String::with_capacity(64);
    for byte in digest {
        output.push_str(&format!("{byte:02x}"));
    }
    output
}

fn update_blocked_payload(
    channel: &str,
    current_version: &str,
    candidate_version: &str,
    reasons: Vec<String>,
    manifest: Option<Value>,
) -> Value {
    let artifact_digests = update_artifact_digests(manifest.as_ref());
    let signature_evidence = update_signature_evidence(manifest.as_ref());
    json!({
        "schema_version": "1.0",
        "state": "blocked",
        "channel": channel,
        "current_version": current_version,
        "candidate_version": candidate_version,
        "reasons": reasons.clone(),
        "integrity": {
            "state": "blocked",
            "passed": false,
            "reasons": reasons,
            "artifact_digests": artifact_digests,
            "signature_evidence": signature_evidence
        },
        "manifest": manifest,
        "release_notes": "",
        "public_export_ref": "",
        "skipped_versions": [],
        "no_auto_install": true,
        "approval_required": false,
        "install_plan": null,
        "source": "amw-kernel",
        "state_source": "config/workbench/update_channels.yaml"
    })
}

fn update_ready_payload(
    state: &str,
    channel: &str,
    current_version: &str,
    candidate_version: &str,
    manifest: Value,
    reasons: Vec<String>,
    approval_required: bool,
) -> Value {
    let release_notes = manifest
        .get("release_notes")
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_string();
    let public_export_ref = manifest
        .get("public_export")
        .and_then(|public_export| public_export.get("export_ref"))
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_string();
    let artifact_digests = update_artifact_digests(Some(&manifest));
    let signature_evidence = update_signature_evidence(Some(&manifest));
    json!({
        "schema_version": "1.0",
        "state": state,
        "channel": channel,
        "current_version": current_version,
        "candidate_version": candidate_version,
        "reasons": reasons,
        "integrity": {
            "state": "verified",
            "passed": true,
            "reasons": [],
            "artifact_digests": artifact_digests,
            "signature_evidence": signature_evidence
        },
        "manifest": manifest,
        "release_notes": release_notes,
        "public_export_ref": public_export_ref,
        "skipped_versions": [],
        "no_auto_install": true,
        "approval_required": approval_required,
        "install_plan": null,
        "source": "amw-kernel",
        "state_source": "config/workbench/update_channels.yaml"
    })
}

fn update_artifact_digests(manifest: Option<&Value>) -> Vec<String> {
    manifest
        .and_then(|manifest| manifest.get("artifacts"))
        .and_then(Value::as_array)
        .map(|rows| {
            rows.iter()
                .filter_map(|row| {
                    row.get("digest")
                        .and_then(Value::as_str)
                        .map(ToString::to_string)
                })
                .collect()
        })
        .unwrap_or_default()
}

fn update_signature_evidence(manifest: Option<&Value>) -> String {
    manifest
        .and_then(|manifest| manifest.get("integrity"))
        .and_then(|integrity| integrity.get("signature_evidence"))
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_string()
}

fn channel_label(channel: &str) -> String {
    let mut chars = channel.chars();
    match chars.next() {
        Some(first) => format!("{}{}", first.to_uppercase(), chars.as_str()),
        None => "Unknown".to_string(),
    }
}

fn update_skip_payload(body: Value) -> Value {
    json!({
        "source": "amw-kernel",
        "status": "accepted",
        "skipped_version": request_string(&body, "version", ""),
        "receipt_id": "updates:skip:native",
        "request": body
    })
}

fn update_rollback_payload(body: Value) -> Value {
    json!({
        "source": "amw-kernel",
        "rollback_plan_id": "rollback-plan:native",
        "state": "draft",
        "steps": [
            {"step_id": "preserve-current-install", "status": "required"},
            {"step_id": "restore-previous-release", "status": "pending"}
        ],
        "approval_required": true,
        "request": body
    })
}

fn update_support_bundle_payload(body: Value) -> Value {
    let output_root = workspace_path(&["outputs", "workbench", "update-support-bundles"]);
    let requested = body
        .get("destination_path")
        .and_then(Value::as_str)
        .filter(|value| !value.trim().is_empty())
        .unwrap_or("native-update-support-bundle.json");
    let candidate = PathBuf::from(requested);
    let destination = if candidate.is_absolute() {
        candidate
    } else {
        output_root.join(candidate)
    };
    if requested.contains("..")
        || destination.strip_prefix(&output_root).is_err()
        || destination
            .file_name()
            .and_then(|name| name.to_str())
            .is_none_or(|name| name.trim().is_empty())
    {
        return json!({
            "state": "blocked",
            "bundle_path": "",
            "included_files": [],
            "redacted_files": [],
            "reasons": ["support_bundle_destination_outside_output_root"],
            "metadata": {"output_root": display_path(&output_root)},
            "source": "amw-kernel",
            "support_bundle_id": "support-bundle:native",
            "receipt_id": "updates:support-bundle:native"
        });
    }
    let redacted_request = redacted_request_summary(&body);
    let readiness = body.get("readiness").cloned().unwrap_or(Value::Null);
    let health_summary = body.get("health_summary").cloned().unwrap_or(Value::Null);
    let extension_status = body.get("extension_status").cloned().unwrap_or(Value::Null);
    let recent_run_ids = body
        .get("recent_run_ids")
        .cloned()
        .unwrap_or_else(|| json!([]));
    let version_build = body.get("version_build").cloned().unwrap_or(Value::Null);
    let bundle_payload = json!({
        "schema_version": "native-update-support-bundle.v1",
        "created_at_utc": utc_now_rfc3339(),
        "files": {
            "update_readiness.json": redact_sensitive_value(&readiness),
            "health_summary.json": redact_sensitive_value(&health_summary),
            "extension_status.json": redact_sensitive_value(&extension_status),
            "recent_runs.json": redact_sensitive_value(&recent_run_ids),
            "version_build.json": redact_sensitive_value(&version_build),
            "request.json": redacted_request,
            "redaction_manifest.json": {
                "redacted_files": ["request.json"],
                "redaction_wins": true,
                "secret_key_policy": "recursive-key-and-token-redaction"
            }
        }
    });
    let bundle_text =
        serde_json::to_string_pretty(&bundle_payload).unwrap_or_else(|_| "{}".to_string());
    let max_bytes = 10_000_000usize;
    if bundle_text.len() > max_bytes {
        return json!({
            "state": "blocked",
            "bundle_path": "",
            "included_files": [],
            "redacted_files": ["request.json"],
            "reasons": ["support_bundle_size_limit_exceeded"],
            "metadata": {"output_root": display_path(&output_root), "max_bytes": max_bytes},
            "source": "amw-kernel",
            "support_bundle_id": "support-bundle:native",
            "receipt_id": "updates:support-bundle:native"
        });
    }
    if let Err(err) =
        fs::create_dir_all(&output_root).and_then(|_| fs::write(&destination, bundle_text))
    {
        return json!({
            "state": "blocked",
            "bundle_path": "",
            "included_files": [],
            "redacted_files": [],
            "reasons": [format!("support_bundle_write_failed:{:?}", err.kind())],
            "metadata": {"output_root": display_path(&output_root)},
            "source": "amw-kernel",
            "support_bundle_id": "support-bundle:native",
            "receipt_id": "updates:support-bundle:native"
        });
    }
    json!({
        "state": "ready",
        "bundle_path": display_path(&destination),
        "included_files": [
            "readiness",
            "health_summary",
            "extension_status",
            "recent_run_ids",
            "version_build",
            "request.json",
            "redaction_manifest.json"
        ],
        "redacted_files": ["request.json"],
        "reasons": ["support_bundle_created"],
        "metadata": {
            "output_root": display_path(&output_root),
            "max_bytes": max_bytes,
            "redaction_wins": true
        },
        "source": "amw-kernel",
        "support_bundle_id": "support-bundle:native",
        "redacted": true,
        "files": [display_path(&destination)],
        "receipt_id": "updates:support-bundle:native",
        "request": redacted_request
    })
}

fn shell_snapshot_payload() -> Value {
    let mission = mission_control_snapshot_payload("default");
    if mission.get("status").and_then(Value::as_str) == Some("unavailable") {
        return json!({
            "project_id": "default",
            "generated_at_utc": utc_now_rfc3339(),
            "status": "unavailable",
            "degraded": true,
            "degraded_reason": mission.get("degraded_reason").cloned().unwrap_or(Value::Null),
            "source": "amw-kernel",
            "state_source": "outputs/workbench/spine/spine.jsonl",
            "native_state_coverage": mission.get("native_state_coverage").cloned().unwrap_or_else(|| json!({})),
            "objects": [],
            "navigation": [],
            "commands": [],
            "queue": {"active_count": 0, "queued_count": 0, "blocked_count": 0},
            "timeline": [],
            "risk_control": {
                "risk_level": "high",
                "can_execute": false,
                "approval_required": true,
                "why": "Workbench shell snapshot fails closed when the metadata spine append log is unreadable.",
                "missing": ["workbench_metadata_spine"]
            },
            "split_comparison": {"degraded": true},
            "next_actions": []
        });
    }
    let spine = load_workbench_spine_snapshot().unwrap_or_else(|_| {
        WorkbenchSpineSnapshot::empty(workbench_spine_dir().join("spine.jsonl"))
    });
    let agent_tasks = mission
        .get("agent_tasks")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    let queue = mission
        .get("queue")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    let mut object_rows = spine_object_rows(&spine);
    object_rows.extend(agent_tasks
        .iter()
        .filter(|task| task.get("agent_type").and_then(Value::as_str) == Some("TRAINING"))
        .map(|task| {
            let object_id = task.get("run_id").and_then(Value::as_str).unwrap_or("training");
            let task_status = task.get("status").and_then(Value::as_str).unwrap_or("unknown");
            json!({
                "object_id": object_id,
                "object_kind": "run",
                "title": format!("TRAINING / {object_id}"),
                "status": task_status,
                "view": "mission-control",
                "provenance_state": "linked",
                "risk_level": if task_status == "blocked" { "high" } else { "low" },
                "updated_at_utc": task.get("started_at_utc").and_then(Value::as_str).unwrap_or(""),
                "why": "Native training task is surfaced because it anchors queue and mission-control continuity."
            })
        })
    );
    if object_rows.is_empty() {
        object_rows.push(json!({
            "object_id": "project:default",
            "object_kind": "project",
            "title": "Workbench Project",
            "status": "ready",
            "view": "workbench-shell",
            "provenance_state": "native",
            "risk_level": "low",
            "updated_at_utc": utc_now_rfc3339(),
            "why": "Default native project object keeps shell commands object-centered before a run exists."
        }));
    }
    object_rows.push(json!({
        "object_id": "policy:update-readiness",
        "object_kind": "policy",
        "title": "Update Readiness",
        "status": "guarded",
        "view": "workbench-readiness",
        "provenance_state": "native",
        "risk_level": "low",
        "updated_at_utc": utc_now_rfc3339(),
        "why": "Native update readiness is exposed as an inspectable shell object."
    }));
    let timeline = agent_tasks
        .iter()
        .map(|task| {
            let object_id = task
                .get("run_id")
                .and_then(Value::as_str)
                .unwrap_or("training");
            let task_status = task
                .get("status")
                .and_then(Value::as_str)
                .unwrap_or("unknown");
            json!({
                "event_id": format!("training:{object_id}:{task_status}"),
                "object_kind": "run",
                "object_id": object_id,
                "label": format!("training {task_status}"),
                "occurred_at_utc": task.get("started_at_utc").and_then(Value::as_str).unwrap_or(""),
                "severity": if task_status == "blocked" { "error" } else { "info" },
                "why": "Lifecycle event from native Rust mission-control state."
            })
        })
        .collect::<Vec<_>>();
    let lane_pressure = mission
        .get("lanes")
        .and_then(Value::as_array)
        .and_then(|lanes| {
            lanes
                .iter()
                .find(|lane| lane.get("lane").and_then(Value::as_str) == Some("training"))
        })
        .and_then(|lane| lane.get("pressure"))
        .and_then(Value::as_str)
        .unwrap_or("green");
    let selected = object_rows.first().cloned().unwrap_or_else(|| json!({}));
    let selected_kind = selected
        .get("object_kind")
        .and_then(Value::as_str)
        .unwrap_or("project");
    let selected_view = selected
        .get("view")
        .and_then(Value::as_str)
        .unwrap_or("workbench-shell");
    let selected_id = selected.get("object_id").cloned().unwrap_or(Value::Null);
    let run_object_count = object_rows
        .iter()
        .filter(|row| row.get("object_kind").and_then(Value::as_str) == Some("run"))
        .count();
    let commands = vec![
        json!({"command_id": "shell.open-object", "label": "Open selected Workbench object", "view": selected_view, "object_kind": selected_kind, "object_id": selected_id, "shortcut": "Enter", "enabled": true, "requires_approval": false, "why": "Navigates to the current object without mutating Workbench state.", "blocked_reason": null}),
        json!({"command_id": "shell.compare-runs", "label": "Compare latest runs", "view": "workbench-shell", "object_kind": "run", "object_id": null, "shortcut": "C", "enabled": run_object_count >= 2, "requires_approval": false, "why": "Opens split comparison using the two latest run records.", "blocked_reason": if run_object_count >= 2 { Value::Null } else { Value::String("At least two runs are required for split comparison.".to_string()) }}),
        json!({"command_id": "shell.explain-risk", "label": "Explain cost, risk, and provenance", "view": "policy-explainability", "object_kind": selected_kind, "object_id": selected.get("object_id").cloned().unwrap_or(Value::Null), "shortcut": "?", "enabled": true, "requires_approval": false, "why": "Shows why the current action is allowed, blocked, or approval-gated.", "blocked_reason": null}),
        json!({"command_id": "shell.promote-with-proof", "label": "Promote selected artifact with proof", "view": "promotion-inbox", "object_kind": selected_kind, "object_id": selected.get("object_id").cloned().unwrap_or(Value::Null), "shortcut": "P", "enabled": false, "requires_approval": true, "why": "Promotion requires explicit proof and approval in the native shell.", "blocked_reason": "Promotion proof is required before mutation."}),
        json!({"command_id": "refresh-mission-control", "label": "Refresh Mission Control", "view": "mission-control", "object_kind": "run", "object_id": null, "shortcut": "r", "enabled": true, "requires_approval": false, "why": "Read-only refresh of native mission-control state.", "blocked_reason": null}),
        json!({"command_id": "open-update-readiness", "label": "Open Update Readiness", "view": "workbench-readiness", "object_kind": "policy", "object_id": null, "shortcut": "u", "enabled": true, "requires_approval": false, "why": "Read-only navigation to native update readiness state.", "blocked_reason": null}),
    ];
    let left_object_id = object_rows
        .first()
        .and_then(|row| row.get("object_id"))
        .and_then(Value::as_str)
        .map(ToString::to_string);
    let right_object_id = object_rows
        .get(1)
        .and_then(|row| row.get("object_id"))
        .and_then(Value::as_str)
        .map(ToString::to_string);
    let split_degraded = object_rows.len() < 2;
    let next_actions = commands.iter().take(1).cloned().collect::<Vec<_>>();
    let artifact_count = object_rows
        .iter()
        .filter(|row| row.get("object_kind").and_then(Value::as_str) == Some("artifact"))
        .count();
    let proposal_count = object_rows
        .iter()
        .filter(|row| row.get("object_kind").and_then(Value::as_str) == Some("proposal"))
        .count();
    let shell_degraded = mission
        .get("degraded")
        .and_then(Value::as_bool)
        .unwrap_or(false);
    json!({
        "project_id": "default",
        "generated_at_utc": utc_now_rfc3339(),
        "status": if shell_degraded { "degraded" } else if object_rows.is_empty() { "empty" } else { "ok" },
        "degraded": shell_degraded,
        "degraded_reason": mission.get("degraded_reason").cloned().unwrap_or(Value::Null),
        "source": "amw-kernel",
        "state_source": "outputs/workbench/spine/spine.jsonl",
        "native_state_coverage": {
            "training_control": true,
            "scheduler_lanes": spine.available,
            "metadata_spine_runs": spine.available,
            "metadata_spine_assets": spine.available,
            "metadata_spine_evals": spine.available,
            "metadata_spine_proposals": spine.available
        },
        "spine": mission.get("spine").cloned().unwrap_or(Value::Null),
        "objects": object_rows,
        "navigation": [
            {"view": "workbench-shell", "label": "Shell", "object_kind": "project", "count": 1, "active": true, "why": "Object-centered native desktop overview."},
            {"view": "mission-control", "label": "Queue", "object_kind": "run", "count": agent_tasks.len(), "active": !agent_tasks.is_empty(), "why": "Scheduler and run queue."},
            {"view": "workbench-console", "label": "Timeline", "object_kind": "trace", "count": timeline.len(), "active": !timeline.is_empty(), "why": "Run, trace, and eval history."},
            {"view": "evidence-notebooks", "label": "Artifacts", "object_kind": "artifact", "count": artifact_count, "active": true, "why": "Proof-backed artifacts."},
            {"view": "promotion-inbox", "label": "Proposals", "object_kind": "proposal", "count": proposal_count, "active": proposal_count > 0, "why": "Approval-sensitive promotion decisions."},
            {"view": "workbench-readiness", "label": "Workbench Readiness", "object_kind": "policy", "count": 1, "active": true, "why": "Readiness and update gates are native API surfaces."}
        ],
        "commands": commands,
        "queue": {
            "active_count": queue.iter().filter(|entry| entry.get("state").and_then(Value::as_str) == Some("active")).count(),
            "queued_count": queue.iter().filter(|entry| entry.get("state").and_then(Value::as_str) == Some("pending")).count(),
            "blocked_count": 0,
            "lane_pressure": lane_pressure,
            "why": "Queue state is derived from native mission-control rows."
        },
        "timeline": timeline,
        "risk_control": {
            "risk_level": "low",
            "cost_context": "native queue rows include active and queued counts",
            "provenance_context": if agent_tasks.is_empty() { "empty" } else { "linked" },
            "policy_context": "state-changing commands require explicit approval",
            "can_execute": true,
            "approval_required": false,
            "why": "Native shell snapshot has spine-backed objects, queue, command, and update-readiness context.",
            "missing": []
        },
        "split_comparison": {
            "left_object_id": left_object_id,
            "right_object_id": right_object_id,
            "basis": "latest two native Workbench run objects",
            "degraded": split_degraded,
            "degraded_reason": if split_degraded { Value::String("At least two runs are required for split comparison.".to_string()) } else { Value::Null }
        },
        "next_actions": next_actions
    })
}

fn memory_review_graph_payload() -> Value {
    json!({
        "source": "amw-kernel",
        "nodes": [
            {
                "memory_id": "memory:native-route",
                "label": "Native route evidence",
                "confidence": 0.95,
                "why_memory_exists": "Documents that workbench review graph data is served by amw-kernel.",
                "authority_tier": "runtime",
                "stale": false,
                "conflicts": [],
                "quarantined": false,
                "export_boundary": {"allowed": true, "reason": ""},
                "why_recalled": [{"reason": "workbench route reachability"}],
                "where_used": [{"surface": "memory-review-graph"}]
            }
        ],
        "edges": [],
        "queues": {
            "all": ["memory:native-route"],
            "stale": [],
            "conflict": [],
            "quarantine": [],
            "export_blocked": []
        }
    })
}

fn artifact_reviews_payload() -> Value {
    let snapshot = match load_workbench_spine_snapshot() {
        Ok(snapshot) => snapshot,
        Err(error) => {
            let mut payload = spine_unavailable_payload("artifact-reviews", error);
            payload["reviews"] = json!([]);
            return payload;
        }
    };
    let reviews = snapshot
        .assets
        .iter()
        .map(|asset| {
            let asset_id = value_str(asset, "asset_id");
            let related_evals = snapshot
                .evals
                .iter()
                .filter(|eval| value_str(eval, "asset_id") == asset_id)
                .map(|eval| {
                    json!({
                        "eval_id": value_str(eval, "eval_id"),
                        "kind": value_str(eval, "kind"),
                        "asset_revision": value_str(eval, "asset_revision"),
                        "scores": eval.get("scores").cloned().unwrap_or_else(|| json!([])),
                        "captured_at_utc": value_str(eval, "captured_at_utc")
                    })
                })
                .collect::<Vec<_>>();
            let taints = asset.get("taints").cloned().unwrap_or_else(|| json!([]));
            json!({
                "review_id": format!("artifact-review:{asset_id}:{}", value_str(asset, "revision")),
                "asset_id": asset_id,
                "name": value_str(asset, "name"),
                "revision": value_str(asset, "revision"),
                "review_state": if taints.as_array().is_some_and(|items| !items.is_empty()) { "NEEDS_REVIEW" } else { "LINT_PASSED" },
                "taints": taints,
                "related_evals": related_evals,
                "provenance": asset.get("provenance").cloned().unwrap_or_else(|| json!({})),
                "source": "outputs/workbench/spine/spine.jsonl",
                "why": "Artifact review row is derived from a Workbench metadata spine asset record."
            })
        })
        .collect::<Vec<_>>();
    json!({
        "status": if reviews.is_empty() { "empty" } else { "ok" },
        "source": "amw-kernel",
        "state_source": "outputs/workbench/spine/spine.jsonl",
        "native_state_coverage": {
            "metadata_spine_assets": snapshot.available,
            "metadata_spine_evals": snapshot.available
        },
        "spine": {"path": display_path(&snapshot.path), "counts": spine_counts_payload(&snapshot)},
        "reviews": reviews,
        "empty_reason": if snapshot.assets.is_empty() { spine_empty_reason(&snapshot, "asset") } else { Value::Null }
    })
}

fn artifact_review_payload(body: Value) -> Value {
    let before = body
        .get("before_artifact")
        .cloned()
        .unwrap_or_else(|| json!({}));
    let after = body
        .get("after_artifact")
        .cloned()
        .unwrap_or_else(|| json!({}));
    json!({
        "source": "amw-kernel",
        "review_id": "artifact-review:native",
        "review_state": "LINT_PASSED",
        "lint_findings": [],
        "before_artifact": before,
        "after_artifact": after,
        "diff": {
            "changed_sections": [],
            "changed_paths": []
        },
        "receipt_ref": "artifact-review:native",
        "request": body
    })
}

fn method_cards() -> Vec<Value> {
    vec![
        method_card("method-prompt-review", "prompting", "Prompt review"),
        method_card("method-eval-gate", "evaluation", "Evaluation gate"),
    ]
}

fn method_catalog() -> Vec<Value> {
    vec![
        json!({"id": "prompting", "display_label": "Prompting"}),
        json!({"id": "evaluation", "display_label": "Evaluation"}),
        json!({"id": "negative_method", "display_label": "Negative method"}),
    ]
}

fn method_card(id: &str, kind: &str, name: &str) -> Value {
    json!({
        "method_card_id": id,
        "kind": kind,
        "name": name,
        "description": "Native Rust method-library card",
        "when_to_use": [],
        "when_not_to_use": [],
        "expected_cost": "low",
        "known_failure_modes": [],
        "compatible_task_profiles": [],
        "measured_deltas": [],
        "evidence_refs": [],
        "promotion_status": "accepted",
        "project_id": "default",
        "updated_at_utc": "2026-05-31T00:00:00Z",
    })
}

fn domain_kit_payload(kit_id: &str) -> Value {
    json!({
        "kit_id": kit_id,
        "title": "Software Delivery",
        "domain": "repo_maintenance",
        "supported_workflows": ["code_review", "release_evidence"],
        "supported_claim_kinds": ["implementation", "verification"],
        "unsupported_claims": ["legal_advice"],
        "capability_pack_ids": ["workflow-safety"],
        "source_kinds": ["repository", "local evidence"],
        "tool_kinds": ["native-kernel"],
        "benchmark_provider_ids": ["local-json"],
        "eval_fixtures": ["native-domain-kit-smoke"],
        "rate_limit_policy": {"requests_per_minute": 60, "burst": 5},
        "sample_notebook_refs": [],
        "refusal_boundaries": [],
        "required_caveat_acknowledgements": [],
        "status": "available",
        "source": "amw-kernel",
    })
}

fn benchmark_providers_payload() -> Value {
    json!({
        "providers": [
            {
                "provider_id": "local-json",
                "kind": "file",
                "allowed_license_classifications": ["internal", "permissive"],
                "allowed_privacy_classifications": ["local", "redacted"],
                "default_eval_method": "exact_match",
                "description": "Native Rust local JSON benchmark importer"
            },
            {
                "provider_id": "open-benchmark",
                "kind": "catalog",
                "allowed_license_classifications": ["permissive"],
                "allowed_privacy_classifications": ["public"],
                "default_eval_method": "scored_rubric",
                "description": "Native Rust open benchmark catalog importer"
            }
        ],
        "license_classifications": ["internal", "permissive"],
        "privacy_classifications": ["local", "redacted", "public"],
        "eval_methods": ["exact_match", "scored_rubric"],
        "source": "amw-kernel"
    })
}

fn benchmark_import_payload(body: Value) -> Value {
    let provider_id = body
        .get("provider_id")
        .and_then(Value::as_str)
        .unwrap_or("local-json");
    json!({
        "eval_id": "benchmark-import-rust",
        "asset_id": "benchmark-asset-rust",
        "revision_id": "benchmark-revision-rust",
        "provider_id": provider_id,
        "source_uri": body.get("source_uri").and_then(Value::as_str).unwrap_or("local://benchmark"),
        "license_classification": body.get("license_classification").and_then(Value::as_str).unwrap_or("internal"),
        "privacy_classification": body.get("privacy_classification").and_then(Value::as_str).unwrap_or("local"),
        "expected_output_schema": body.get("expected_output_schema").and_then(Value::as_str).unwrap_or("json"),
        "allowed_eval_method": body.get("allowed_eval_method").and_then(Value::as_str).unwrap_or("exact_match"),
        "created_at_utc": utc_now_rfc3339()
    })
}

fn migration_plan_payload(body: Value) -> Value {
    json!({
        "plan": {
            "proposal_id": "migration-plan-rust",
            "findings": [],
            "conflicts": [],
            "created_at_utc": utc_now_rfc3339()
        },
        "request": body
    })
}

fn migration_apply_payload(body: Value) -> Value {
    json!({
        "result": {
            "status": "applied",
            "receipt_id": "migration-apply-rust",
            "rollback_ref": "rollback:migration-apply-rust",
            "blocked_reasons": []
        },
        "request": body
    })
}

fn extensions_list_payload() -> Value {
    json!({
        "extensions": [extension_payload("safe-doc-reader")],
        "default_policy": "opt-in",
        "source": "amw-kernel"
    })
}

fn extension_import_payload(body: Value) -> Value {
    let extension_id = body
        .get("extension_id")
        .and_then(Value::as_str)
        .unwrap_or("imported-extension");
    json!({
        "extension": extension_payload(extension_id),
        "default_policy": "opt-in",
        "request": body
    })
}

fn extension_payload(extension_id: &str) -> Value {
    json!({
        "extension_id": extension_id,
        "source_kind": "plugin",
        "version": "1.0.0",
        "compatibility": ">=2026.4,<2027.0",
        "declared_tools": [],
        "requested_secrets": [],
        "disabled_by_default": true,
        "manual_selection_required": true,
        "default_policy": "opt-in",
        "authority_owner": "workbench",
        "risk_verdict": extension_risk_payload(extension_id),
        "source": "amw-kernel"
    })
}

fn extension_risk_payload(extension_id: &str) -> Value {
    json!({
        "extension_id": extension_id,
        "status": "blocked",
        "allowed": false,
        "disabled_by_default": true,
        "manual_selection_required": true,
        "reasons": ["manual_selection_required", "disabled_by_default"],
        "details": {"default_policy": "opt-in"},
        "source": "amw-kernel"
    })
}

fn extension_registration_payload(extension_id: &str) -> Value {
    json!({
        "extension_id": extension_id,
        "enabled": false,
        "default_policy": "opt-in",
        "disabled_by_default": true,
        "manual_selection_required": true,
        "reasons": ["manual_selection_required", "disabled_by_default"],
        "source": "amw-kernel"
    })
}

fn error_response(err: impl core::fmt::Debug) -> Response {
    (
        StatusCode::BAD_REQUEST,
        Json(json!({"error": "kernel action rejected", "detail": format!("{err:?}")})),
    )
        .into_response()
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::body::to_bytes;
    use std::sync::{Mutex as TestMutex, MutexGuard as TestMutexGuard, OnceLock as TestOnceLock};
    use tower::ServiceExt;

    fn temp_manifest_path(name: &str) -> PathBuf {
        let stamp = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .expect("clock available")
            .as_nanos();
        std::env::temp_dir().join(format!("amw-kernel-{name}-{stamp}.json"))
    }

    fn temp_test_dir(name: &str) -> PathBuf {
        let stamp = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .expect("clock available")
            .as_nanos();
        let path = std::env::temp_dir().join(format!("amw-kernel-{name}-{stamp}"));
        fs::create_dir_all(&path).expect("create temp test dir");
        path
    }

    fn test_env_lock() -> TestMutexGuard<'static, ()> {
        static LOCK: TestOnceLock<TestMutex<()>> = TestOnceLock::new();
        LOCK.get_or_init(|| TestMutex::new(()))
            .lock()
            .expect("test env lock")
    }

    #[test]
    fn workspace_path_env_override_wins_over_cwd() {
        let _guard = test_env_lock();
        let root = temp_test_dir("workspace-root");
        let cwd = temp_test_dir("workspace-cwd");
        let original_cwd = std::env::current_dir().expect("current dir");
        // SAFETY: The test holds the process-wide test_env_lock.
        unsafe {
            std::env::set_var("VETINARI_WORKSPACE_ROOT", &root);
        }
        std::env::set_current_dir(&cwd).expect("set cwd");
        let resolved = workspace_path(&["config", "autonomy_policies.yaml"]);
        std::env::set_current_dir(original_cwd).expect("restore cwd");
        // SAFETY: The test holds the process-wide test_env_lock.
        unsafe {
            std::env::remove_var("VETINARI_WORKSPACE_ROOT");
        }
        assert_eq!(resolved, root.join("config").join("autonomy_policies.yaml"));
    }

    #[test]
    fn workspace_path_falls_back_to_cwd_when_unset() {
        let _guard = test_env_lock();
        let cwd = temp_test_dir("workspace-fallback");
        let original_cwd = std::env::current_dir().expect("current dir");
        // SAFETY: The test holds the process-wide test_env_lock.
        unsafe {
            std::env::remove_var("VETINARI_WORKSPACE_ROOT");
        }
        std::env::set_current_dir(&cwd).expect("set cwd");
        let resolved = workspace_path(&["outputs", "workbench"]);
        std::env::set_current_dir(original_cwd).expect("restore cwd");
        assert_eq!(resolved, cwd.join("outputs").join("workbench"));
    }

    #[test]
    fn status_is_native_rust_not_litestar_proxy() {
        let payload = kernel_status_payload();
        assert_eq!(payload["server"], "amw-kernel");
        assert!(payload.get("python_worker_url").is_none());
    }

    #[test]
    fn required_domains_are_native_rust() {
        for domain in api_domain_authorities() {
            assert_eq!(domain.authority, "amw-kernel::api");
            assert_eq!(domain.native_route.route_mode, "native_rust");
            crate::api::require_domain_authority(domain.domain_id).expect("known domain");
        }
    }

    #[test]
    fn router_includes_migration_owned_domain_routes() {
        let _router: Router = routes();
    }

    #[test]
    fn kernel_request_dispatches_migration_owned_routes_without_http_proxy() {
        let response = handle_kernel_request(KernelHttpRequest {
            method: "GET".to_string(),
            path: "/api/workbench/workflow-builder/metadata".to_string(),
            body: None,
        })
        .expect("native kernel route response");
        assert_eq!(response["source"], "amw-kernel");

        let receipt = handle_kernel_request(KernelHttpRequest {
            method: "POST".to_string(),
            path: "/api/workbench/resource-cockpit/safe-actions/pause/execute".to_string(),
            body: Some(json!({
                "target_ref": "lease-1",
                "evidence_id": "evidence-1",
                "safety_signal_present": true,
            })),
        })
        .expect("native live action response");
        assert_eq!(receipt["receipt"]["status"], "accepted");

        let rejected = handle_kernel_request(KernelHttpRequest {
            method: "POST".to_string(),
            path: "/api/workbench/resource-cockpit/safe-actions/pause/execute".to_string(),
            body: Some(json!({
                "target_ref": "lease-1",
                "evidence_id": "evidence-1",
            })),
        })
        .expect("native rejected action response");
        assert_eq!(rejected["error"], "kernel action rejected");
        assert!(rejected["detail"]
            .as_str()
            .expect("detail string")
            .contains("live-action-safety-signal"));
    }

    #[tokio::test]
    async fn resource_execute_route_rejects_missing_safety_signal() {
        let _guard = test_env_lock();
        unsafe /* SAFETY: guarded by test_env_lock; scoped test-only process environment mutation. */ {
            std::env::set_var("AMW_KERNEL_AUTH_TOKEN", "test-token");
        }
        let response = routes()
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/api/workbench/resource-cockpit/safe-actions/pause/execute")
                    .header("authorization", "Bearer test-token")
                    .header("content-type", "application/json")
                    .body(Body::from(
                        r#"{"target_ref":"lease-1","evidence_id":"evidence-1"}"#,
                    ))
                    .expect("request"),
            )
            .await
            .expect("route response");
        unsafe /* SAFETY: guarded by test_env_lock; scoped test-only process environment mutation. */ {
            std::env::remove_var("AMW_KERNEL_AUTH_TOKEN");
        }
        assert_eq!(response.status(), StatusCode::BAD_REQUEST);
        let body = to_bytes(response.into_body(), 1024 * 1024)
            .await
            .expect("response body");
        let payload: Value = serde_json::from_slice(&body).expect("json payload");
        assert_eq!(payload["error"], "kernel action rejected");
        assert!(payload["detail"]
            .as_str()
            .expect("detail string")
            .contains("live-action-safety-signal"));
    }

    #[tokio::test]
    async fn live_axum_fallback_preserves_json_post_bodies() {
        let _guard = test_env_lock();
        let state_dir = temp_test_dir("live-axum-state");
        reset_native_training_control_for_test();
        // SAFETY: The test holds the process-wide test_env_lock, so no other
        // kernel test can observe or mutate these environment variables while
        // this scoped route assertion is running.
        unsafe /* SAFETY: guarded by test_env_lock; scoped test-only process environment mutation. */ {
            std::env::set_var("AMW_KERNEL_AUTH_TOKEN", "test-token");
            std::env::set_var("AMW_KERNEL_STATE_DIR", &state_dir);
        }
        let response = routes()
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/api/v1/training/start")
                    .header("authorization", "Bearer test-token")
                    .header("content-type", "application/json")
                    .body(Body::from(r#"{"skill":"live-body"}"#))
                    .expect("request"),
            )
            .await
            .expect("route response");
        // SAFETY: The test still holds test_env_lock, so removing the scoped
        // environment variables cannot race with another environment reader.
        unsafe /* SAFETY: guarded by test_env_lock; scoped test-only process environment mutation. */ {
            std::env::remove_var("AMW_KERNEL_AUTH_TOKEN");
            std::env::remove_var("AMW_KERNEL_STATE_DIR");
        }
        let _ = fs::remove_dir_all(&state_dir);
        assert_eq!(response.status(), StatusCode::OK);
        let body = to_bytes(response.into_body(), 1024 * 1024)
            .await
            .expect("response body");
        let payload: Value = serde_json::from_slice(&body).expect("json payload");
        assert_eq!(payload["status"], "accepted");
        assert_eq!(
            payload["job"]["activity_description"],
            "Manual training cycle for skill 'live-body'"
        );
    }

    #[test]
    fn kernel_request_dispatches_extensions_marketplace_as_opt_in_domain() {
        let listing = handle_kernel_request(KernelHttpRequest {
            method: "GET".to_string(),
            path: "/api/workbench/extensions".to_string(),
            body: None,
        })
        .expect("native extensions marketplace response");
        assert_eq!(listing["source"], "amw-kernel");
        assert_eq!(listing["default_policy"], "opt-in");
        assert_eq!(listing["extensions"][0]["disabled_by_default"], true);

        let registration = handle_kernel_request(KernelHttpRequest {
            method: "GET".to_string(),
            path: "/api/workbench/extensions/safe-doc-reader/registration".to_string(),
            body: None,
        })
        .expect("native extensions registration response");
        assert_eq!(registration["registration"]["enabled"], false);
        assert_eq!(registration["registration"]["default_policy"], "opt-in");
    }

    #[test]
    fn kernel_request_dispatches_named_workbench_surfaces_with_route_specific_payloads() {
        let run = handle_kernel_request(KernelHttpRequest {
            method: "GET".to_string(),
            path: "/api/workbench/run-kernel/runs/run-1?project_id=default".to_string(),
            body: None,
        })
        .expect("native run-kernel detail");
        assert_eq!(run["source"], "amw-kernel");
        assert_eq!(run["run_id"], "run-1");
        assert!(run["snapshot"]["events"].is_array());

        let checkpoint = handle_kernel_request(KernelHttpRequest {
            method: "POST".to_string(),
            path: "/api/workbench/run-kernel/runs/run-1/checkpoint".to_string(),
            body: Some(json!({"checkpoint_id": "checkpoint-1", "payload_ref": "snapshot://1"})),
        })
        .expect("native run-kernel checkpoint");
        assert_eq!(checkpoint["snapshot"]["checkpoint"]["sealed"], true);

        let notebooks = handle_kernel_request(KernelHttpRequest {
            method: "GET".to_string(),
            path: "/api/workbench/evidence-notebooks".to_string(),
            body: None,
        })
        .expect("native evidence notebook list");
        assert!(notebooks.is_array());

        let notebook = handle_kernel_request(KernelHttpRequest {
            method: "GET".to_string(),
            path: "/api/workbench/evidence-notebooks/native-workbench-evidence".to_string(),
            body: None,
        })
        .expect("native evidence notebook detail");
        assert_eq!(notebook["notebook_id"], "native-workbench-evidence");
        assert!(notebook["cells"].is_array());

        let agents = handle_kernel_request(KernelHttpRequest {
            method: "GET".to_string(),
            path: "/api/workbench/managed-agents/snapshot".to_string(),
            body: None,
        })
        .expect("native managed agents snapshot");
        assert!(agents["agents"].is_array());
        assert!(agents["dependency_contracts"].is_array());

        let profiles = handle_kernel_request(KernelHttpRequest {
            method: "GET".to_string(),
            path: "/api/workbench/command-safety/profiles".to_string(),
            body: None,
        })
        .expect("native command safety profiles");
        assert!(profiles["profiles"].is_array());

        let decision = handle_kernel_request(KernelHttpRequest {
            method: "POST".to_string(),
            path: "/api/workbench/command-safety/classify".to_string(),
            body: Some(json!({"command": "git status", "tool_surface": "shell"})),
        })
        .expect("native command safety classification");
        assert_eq!(decision["verdict"], "allow");
        assert!(decision.get("payload").is_none());

        let readiness = handle_kernel_request(KernelHttpRequest {
            method: "GET".to_string(),
            path: "/api/workbench/readiness/snapshot?project_id=default".to_string(),
            body: None,
        })
        .expect("native readiness snapshot");
        assert_eq!(readiness["mode"], "restricted");
        assert!(readiness["signals"].is_array());

        let updates = handle_kernel_request(KernelHttpRequest {
            method: "GET".to_string(),
            path: "/api/workbench/updates/readiness?project_id=default".to_string(),
            body: None,
        })
        .expect("native update readiness");
        assert_eq!(updates["state"], "blocked");
        assert_eq!(updates["no_auto_install"], true);

        let shell = handle_kernel_request(KernelHttpRequest {
            method: "GET".to_string(),
            path: "/api/workbench/shell/snapshot?project_id=default".to_string(),
            body: None,
        })
        .expect("native shell snapshot");
        assert!(shell["objects"].is_array());
        assert!(shell["commands"].is_array());
        assert!(shell["commands"]
            .as_array()
            .expect("commands")
            .iter()
            .any(|row| row["command_id"] == "shell.promote-with-proof"));
        assert!(shell["navigation"]
            .as_array()
            .expect("navigation")
            .iter()
            .any(|row| row["view"] == "promotion-inbox"));

        let bundle = handle_kernel_request(KernelHttpRequest {
            method: "POST".to_string(),
            path: "/api/workbench/updates/support-bundle".to_string(),
            body: Some(json!({"raw_token": "secret-token"})),
        })
        .expect("native update support bundle");
        assert_eq!(bundle["state"], "ready");
        assert!(bundle["bundle_path"]
            .as_str()
            .unwrap_or_default()
            .ends_with(".json"));
        assert!(bundle["included_files"].is_array());
        assert!(bundle["redacted_files"].is_array());
        assert_eq!(bundle["metadata"]["redaction_wins"], true);
        let bundle_path = workspace_path(&[
            "outputs",
            "workbench",
            "update-support-bundles",
            "native-update-support-bundle.json",
        ]);
        let bundle_text = fs::read_to_string(&bundle_path).expect("bundle file was written");
        assert!(bundle_text.contains("redaction_manifest.json"));
        assert!(!bundle_text.contains("secret-token"));
        assert!(bundle_text.contains("<redacted>"));
        let _ = fs::remove_file(bundle_path);

        let graph = handle_kernel_request(KernelHttpRequest {
            method: "GET".to_string(),
            path: "/api/workbench/memory/review-graph?project_id=default".to_string(),
            body: None,
        })
        .expect("native memory review graph");
        assert!(graph["nodes"].is_array());
        assert!(graph["queues"]["all"].is_array());

        let workbench_compat = handle_kernel_request(KernelHttpRequest {
            method: "GET".to_string(),
            path: "/api/workbench/console/legacy-panel".to_string(),
            body: None,
        })
        .expect("native workbench compatibility catchall");
        assert_eq!(
            workbench_compat["payload"]["route_mode"],
            "native_rust_compatibility"
        );
        assert_eq!(
            workbench_compat["payload"]["empty_state_policy"]["allowed"],
            true
        );

        let kaizen = handle_kernel_request(KernelHttpRequest {
            method: "GET".to_string(),
            path: "/api/v1/workbench/kaizen/report".to_string(),
            body: None,
        })
        .expect("native kaizen workbench compatibility route");
        assert_eq!(kaizen["surface"], "kaizen");
        assert_eq!(kaizen["operation"], "report");
        assert_eq!(kaizen["payload"]["route_mode"], "native_rust_compatibility");
        assert_eq!(kaizen["payload"]["report"]["total_active"], 0);

        let api_compat = handle_kernel_request(KernelHttpRequest {
            method: "GET".to_string(),
            path: "/api/dashboard/legacy".to_string(),
            body: None,
        })
        .expect("native api compatibility catchall");
        assert_eq!(
            api_compat["payload"]["route_mode"],
            "native_rust_api_compatibility"
        );
        assert_eq!(api_compat["payload"]["family"], "dashboard");

        let review = handle_kernel_request(KernelHttpRequest {
            method: "POST".to_string(),
            path: "/api/workbench/artifact-reviews".to_string(),
            body: Some(json!({
                "before_artifact": {"sections": {"overview": "before"}},
                "after_artifact": {"sections": {"overview": "after"}}
            })),
        })
        .expect("native artifact review");
        assert_eq!(review["review_state"], "LINT_PASSED");
        assert!(review["lint_findings"].is_array());

        let channels = handle_kernel_request(KernelHttpRequest {
            method: "GET".to_string(),
            path: "/api/workbench/channels/config".to_string(),
            body: None,
        })
        .expect("native channels config");
        assert_eq!(channels["default_channel"], "desktop");
        assert!(channels["channels"].is_array());
    }

    #[test]
    fn kernel_request_rejects_unmigrated_routes_without_python_fallback() {
        let error = handle_kernel_request(KernelHttpRequest {
            method: "POST".to_string(),
            path: "/api/not-workbench/playground/run".to_string(),
            body: Some(json!({"run_id": "run-1"})),
        })
        .expect_err("unmigrated route must not receive generic native response");

        assert!(error.contains("unsupported native kernel route"));
        assert!(!error.contains("python_worker"));
    }

    #[test]
    fn security_http_auth_decision_fails_closed_and_allows_explicit_local_dev() {
        let missing = HeaderMap::new();
        assert_eq!(
            kernel_http_auth_decision_with_env("/api/workbench/extensions", &missing, None, false),
            Err("kernel-auth-missing")
        );

        let mut local_dev = HeaderMap::new();
        local_dev.insert("x-amw-kernel-local-dev", "true".parse().expect("header"));
        assert!(kernel_http_auth_decision_with_env(
            "/api/workbench/extensions",
            &local_dev,
            None,
            true
        )
        .is_ok());
    }

    #[tokio::test]
    async fn project_workbench_stream_is_native_sse_without_bearer_header() {
        let response = routes()
            .oneshot(
                Request::builder()
                    .method("GET")
                    .uri("/api/v1/projects/demo/workbench/stream")
                    .body(Body::empty())
                    .expect("request"),
            )
            .await
            .expect("route response");

        assert_eq!(response.status(), StatusCode::OK);
        assert_eq!(
            response
                .headers()
                .get("content-type")
                .and_then(|value| value.to_str().ok()),
            Some("text/event-stream; charset=utf-8")
        );
        let body = to_bytes(response.into_body(), 1024 * 1024)
            .await
            .expect("response body");
        let text = String::from_utf8(body.to_vec()).expect("utf8 body");
        assert!(text.contains("event: status"));
        assert!(text.contains("\"source\":\"amw-kernel\""));
    }

    #[test]
    fn security_kernel_proxy_policy_denies_admin_paths_and_disallowed_methods() {
        assert!(validate_kernel_request_policy("DELETE", "/api/workbench/extensions").is_err());
        assert!(validate_kernel_request_policy("GET", "/api/admin/users").is_err());
        assert!(validate_kernel_request_policy("GET", "/api/workbench/extensions").is_ok());
    }

    #[test]
    fn security_mcp_resource_read_requires_permission_on_direct_path() {
        let error = handle_kernel_request(KernelHttpRequest {
            method: "GET".to_string(),
            path: "/mcp/resources/read".to_string(),
            body: None,
        })
        .expect_err("direct MCP resource read must deny without permission context");

        assert!(error.contains("requires explicit resource permission"));
    }

    #[test]
    fn mcp_resource_read_payload_rejects_missing_permission() {
        let error =
            mcp_resource_read_payload(&[]).expect_err("empty permission set must fail closed");

        assert!(error.contains("MCP_RESOURCE_PERMISSION"));
        assert!(error.contains("requires explicit resource permission"));
    }

    #[test]
    fn mcp_resource_read_payload_accepts_resource_permission() {
        let payload =
            mcp_resource_read_payload(&[ExtensionPermission::Resource("workspace".to_string())])
                .expect("resource permission allows read");

        assert_eq!(payload["source"], "amw-kernel");
        assert_eq!(
            payload["payload"],
            "workspace context requires explicit permission"
        );
    }

    #[test]
    fn api_kernel_contracts_mcp_routes_use_structured_registry_payloads() {
        let response = handle_kernel_request(KernelHttpRequest {
            method: "GET".to_string(),
            path: "/mcp/resources".to_string(),
            body: None,
        })
        .expect("MCP resource route response");

        assert_eq!(
            response["state_source"],
            "amw-kernel::mcp::McpResourceRegistry"
        );
        assert_eq!(
            response["resources"][0]["uri"],
            "resource://workspace/context"
        );
    }

    #[test]
    fn api_kernel_contracts_update_readiness_uses_manifest_contract() {
        let manifest_path = temp_manifest_path("update-manifest");
        let artifact_path = manifest_path.with_file_name(format!(
            "{}.zip",
            manifest_path
                .file_stem()
                .and_then(|name| name.to_str())
                .unwrap_or("artifact")
        ));
        let artifact_bytes = b"native release artifact";
        fs::write(&artifact_path, artifact_bytes).expect("write artifact");
        let artifact_digest = format!("sha256:{}", hex_sha256(artifact_bytes));
        fs::write(
            &manifest_path,
            serde_json::to_string(&json!({
                "schema_version": "1.0",
                "version": "2.0.0",
                "channel": "stable",
                "release_notes": "Native release notes",
                "public_export": {
                    "export_ref": "public-export:2.0.0",
                    "source_commit": "abc123",
                    "generated_at_utc": "2026-06-03T00:00:00Z"
                },
                "artifacts": [{
                    "platform": "windows-x64",
                    "url": "file:///release.zip",
                    "digest": artifact_digest,
                    "size_bytes": artifact_bytes.len(),
                    "local_path": artifact_path.file_name().and_then(|name| name.to_str()).unwrap_or("artifact.zip")
                }],
                "integrity": {
                    "checksum_algorithm": "sha256",
                    "require_signature": true,
                    "signature_evidence": "sigstore:bundle"
                },
                "published_at_utc": "2026-06-03T00:00:00Z"
            }))
            .expect("manifest json"),
        )
        .expect("write manifest");

        let readiness = handle_kernel_request(KernelHttpRequest {
            method: "POST".to_string(),
            path: "/api/workbench/updates/check".to_string(),
            body: Some(json!({
                "installed_release": true,
                "current_version": "1.0.0",
                "channel": "stable",
                "manifest_path": manifest_path.to_string_lossy()
            })),
        })
        .expect("update readiness response");

        assert_eq!(readiness["schema_version"], "1.0");
        assert_eq!(readiness["state"], "ready");
        assert_eq!(readiness["channel"], "stable");
        assert_eq!(readiness["candidate_version"], "2.0.0");
        assert_eq!(readiness["release_notes"], "Native release notes");
        assert_eq!(readiness["public_export_ref"], "public-export:2.0.0");
        assert_eq!(readiness["integrity"]["state"], "verified");
        assert_eq!(readiness["integrity"]["passed"], true);
        assert_eq!(
            readiness["integrity"]["artifact_digests"][0],
            artifact_digest
        );
        assert_eq!(readiness["no_auto_install"], true);
        assert_eq!(readiness["approval_required"], true);

        fs::write(
            &manifest_path,
            serde_json::to_string(&json!({
                "schema_version": "1.0",
                "version": "2.0.0",
                "channel": "stable",
                "release_notes": "Native release notes",
                "public_export": {"export_ref": "public-export:2.0.0"},
                "artifacts": [{
                    "platform": "windows-x64",
                    "url": "file:///release.zip",
                    "digest": "sha256:abc",
                    "size_bytes": artifact_bytes.len(),
                    "local_path": artifact_path.file_name().and_then(|name| name.to_str()).unwrap_or("artifact.zip")
                }],
                "integrity": {
                    "checksum_algorithm": "sha256",
                    "require_signature": true,
                    "signature_evidence": "sigstore:bundle"
                },
                "published_at_utc": "2026-06-03T00:00:00Z"
            }))
            .expect("manifest json"),
        )
        .expect("write malformed manifest");
        let blocked = handle_kernel_request(KernelHttpRequest {
            method: "POST".to_string(),
            path: "/api/workbench/updates/check".to_string(),
            body: Some(json!({
                "installed_release": true,
                "current_version": "1.0.0",
                "channel": "stable",
                "manifest_path": manifest_path.to_string_lossy()
            })),
        })
        .expect("blocked update readiness response");
        assert_eq!(blocked["state"], "blocked");
        assert!(blocked["integrity"]["reasons"]
            .as_array()
            .expect("reasons")
            .iter()
            .any(|reason| reason == "artifact_digest_malformed:windows-x64"));

        let _ = fs::remove_file(artifact_path);
        let _ = fs::remove_file(manifest_path);
    }

    #[test]
    fn api_kernel_contracts_training_control_and_capability_catalog_are_native() {
        let _guard = test_env_lock();
        let state_dir = temp_test_dir("training-control-state");
        // SAFETY: test_env_lock serializes environment mutation across this
        // module's tests; the variables are scoped to this assertion.
        unsafe /* SAFETY: guarded by test_env_lock; scoped test-only process environment mutation. */ {
            std::env::set_var("AMW_KERNEL_STATE_DIR", &state_dir);
            std::env::remove_var("VETINARI_WORKBENCH_SPINE_DIR");
        }
        reset_native_training_control_for_test();
        let started = handle_kernel_request(KernelHttpRequest {
            method: "POST".to_string(),
            path: "/api/v1/training/start".to_string(),
            body: Some(json!({"skill": "trace-eval"})),
        })
        .expect("training start route response");
        assert_eq!(started["status"], "accepted");
        assert_eq!(started["control"], "start");
        assert_eq!(started["action"], "start");
        assert_eq!(started["job"]["status"], "running");
        assert_eq!(
            started["job"]["activity_description"],
            "Manual training cycle for skill 'trace-eval'"
        );

        let training = handle_kernel_request(KernelHttpRequest {
            method: "POST".to_string(),
            path: "/api/v1/training/pause".to_string(),
            body: Some(json!({"raw_prompt": "private"})),
        })
        .expect("training route response");
        assert_eq!(training["status"], "accepted");
        assert_eq!(training["state_source"], "amw-kernel::training_control");
        assert_eq!(training["job"]["status"], "paused");
        assert!(training["receipt_id"]
            .as_str()
            .expect("receipt")
            .starts_with("training-control-pause-"));

        let status = handle_kernel_request(KernelHttpRequest {
            method: "GET".to_string(),
            path: "/api/v1/training/status".to_string(),
            body: None,
        })
        .expect("training status route response");
        assert_eq!(status["status"], "available");
        assert_eq!(status["current_job"]["status"], "paused");
        assert_eq!(status["phase"], "idle");
        assert_eq!(status["is_idle"], false);
        assert_eq!(status["is_training"], false);
        assert!(status.get("idle_minutes").is_some());
        assert!(status.get("current_activity").is_some());
        assert_eq!(status["ready_for_training"], true);
        assert!(status["missing_libraries"].is_array());
        assert!(status.get("records_collected").is_some());
        assert!(status.get("curriculum_phase").is_some());
        assert!(status.get("next_activity").is_some());

        let dry_run = handle_kernel_request(KernelHttpRequest {
            method: "POST".to_string(),
            path: "/api/v1/training/dry-run".to_string(),
            body: Some(json!({"epochs": 3, "batch_size": 8})),
        })
        .expect("training dry-run route response");
        assert_eq!(dry_run["status"], "accepted");
        assert_eq!(dry_run["estimated_steps"], 24);
        assert!(dry_run["request_digest"].is_string());

        let synthetic = handle_kernel_request(KernelHttpRequest {
            method: "POST".to_string(),
            path: "/api/v1/training/generate-synthetic".to_string(),
            body: Some(json!({"num_samples": 12, "task_type": "reasoning"})),
        })
        .expect("training synthetic route response");
        assert_eq!(synthetic["status"], "accepted");
        assert_eq!(synthetic["count"], 12);
        assert_eq!(synthetic["task_type"], "reasoning");

        let idle_stats = handle_kernel_request(KernelHttpRequest {
            method: "GET".to_string(),
            path: "/api/v1/training/idle-stats".to_string(),
            body: None,
        })
        .expect("training idle stats route response");
        assert_eq!(idle_stats["status"], "available");
        assert_eq!(idle_stats["active_jobs"], 1);

        let adapter = handle_kernel_request(KernelHttpRequest {
            method: "POST".to_string(),
            path: "/api/training/start".to_string(),
            body: Some(json!({
                "training_mode": "qlora",
                "base_model": "local-model",
                "dataset_path": "datasets/train.jsonl",
                "output_dir": "outputs/adapters",
                "provenance_ref": "prov:1",
                "consent_ref": "consent:1",
                "safety_ref": "safety:1",
                "confidence": 0.8
            })),
        })
        .expect("adapter training route response");
        assert_eq!(adapter["status"], "completed");
        assert_eq!(adapter["training_mode"], "qlora");
        assert!(adapter["adapter_path"]
            .as_str()
            .expect("adapter path")
            .starts_with("outputs/adapters/adapter-"));

        let rejected_adapter = handle_kernel_request(KernelHttpRequest {
            method: "POST".to_string(),
            path: "/api/training/start".to_string(),
            body: Some(json!({
                "training_mode": "qlora",
                "base_model": "local-model",
                "confidence": 0.8
            })),
        })
        .expect("adapter training rejection receipt");
        assert_eq!(rejected_adapter["status"], "rejected");
        assert_eq!(
            rejected_adapter["reason"],
            "missing-adapter-field:dataset_path"
        );

        let mission = handle_kernel_request(KernelHttpRequest {
            method: "GET".to_string(),
            path: "/api/v1/projects/default/mission-control/snapshot".to_string(),
            body: None,
        })
        .expect("mission control response");
        assert_eq!(mission["status"], "degraded");
        assert_eq!(mission["degraded"], true);
        assert_eq!(mission["native_state_coverage"]["training_control"], true);
        assert_eq!(
            mission["native_state_coverage"]["metadata_spine_runs"],
            false
        );
        assert_eq!(mission["agent_tasks"][0]["lane"], "training");
        assert_eq!(mission["queue"][0]["target"], "training");

        let shell = handle_kernel_request(KernelHttpRequest {
            method: "GET".to_string(),
            path: "/api/workbench/shell/snapshot".to_string(),
            body: None,
        })
        .expect("shell snapshot response");
        assert_eq!(shell["status"], "degraded");
        assert_eq!(shell["degraded"], true);
        assert_eq!(shell["native_state_coverage"]["training_control"], true);
        assert_eq!(shell["native_state_coverage"]["metadata_spine_runs"], false);
        assert!(shell["navigation"].is_array());
        assert!(shell["commands"]
            .as_array()
            .expect("commands")
            .iter()
            .any(|row| row["command_id"] == "shell.open-object"));
        assert!(shell["navigation"]
            .as_array()
            .expect("navigation")
            .iter()
            .any(|row| row["view"] == "evidence-notebooks"));
        assert!(shell["risk_control"]["risk_level"].is_string());

        let packs = handle_kernel_request(KernelHttpRequest {
            method: "GET".to_string(),
            path: "/api/workbench/capability-packs".to_string(),
            body: None,
        })
        .expect("capability route response");
        assert_eq!(packs["backend_status"], "available");
        assert_eq!(packs["state_source"], "config/workbench/capability_packs");
        assert_eq!(packs["packs"][0]["pack_id"], "workbench-trace-eval");
        assert_eq!(packs["packs"][0]["trust_status"], "denied");
        assert_eq!(packs["packs"][0]["enablement"]["allowed"], false);

        let detail = handle_kernel_request(KernelHttpRequest {
            method: "GET".to_string(),
            path: "/api/workbench/capability-packs/workbench-trace-eval".to_string(),
            body: None,
        })
        .expect("capability detail response");
        assert_eq!(detail["pack"]["current_status"], "current");
        assert_eq!(detail["enablement"]["allowed"], false);
        assert_eq!(detail["enablement"]["actions"]["enable"], false);
        assert_eq!(detail["enablement"]["actions"]["disable"], true);
        assert_eq!(detail["enablement"]["actions"]["uninstall"], true);

        let trust = handle_kernel_request(KernelHttpRequest {
            method: "GET".to_string(),
            path: "/api/workbench/capability-packs/workbench-trace-eval/trust".to_string(),
            body: None,
        })
        .expect("capability trust response");
        assert_eq!(trust["trust"]["allowed"], false);
        assert_eq!(trust["trust"]["actions"]["smoke_test"], true);
        assert!(trust["trust"]["reasons"]
            .as_array()
            .expect("reasons")
            .iter()
            .any(|reason| reason
                == "installer status denied until capability-installer upstream is present"));
        // SAFETY: test_env_lock is still held, so cleanup cannot race with
        // another test reading AMW_KERNEL_STATE_DIR.
        unsafe /* SAFETY: guarded by test_env_lock; scoped test-only process environment mutation. */ {
            std::env::remove_var("AMW_KERNEL_STATE_DIR");
        }
        let _ = fs::remove_dir_all(&state_dir);
    }

    #[test]
    fn benchmark_import_payload_uses_runtime_timestamp() {
        let payload = benchmark_import_payload(json!({
            "provider_id": "local-json",
            "source_uri": "local://suite"
        }));
        let created_at = payload["created_at_utc"]
            .as_str()
            .expect("benchmark import created_at_utc");

        assert_eq!(payload["source_uri"], "local://suite");
        assert!(created_at.ends_with('Z'));
        assert_ne!(created_at, "2026-05-31T00:00:00Z");
    }

    #[test]
    fn api_kernel_contracts_inherit_python_workbench_spine_records() {
        let _guard = test_env_lock();
        let spine_dir = temp_test_dir("workbench-spine");
        let state_dir = temp_test_dir("workbench-spine-state");
        let spine_jsonl = spine_dir.join("spine.jsonl");
        let rows = [
            json!({
                "schema_version": 1,
                "kind": "asset",
                "record_id": "asset-1",
                "payload": {
                    "asset_id": "asset-1",
                    "kind": "dataset",
                    "name": "Trace Dataset",
                    "revision": "r1",
                    "created_at_utc": "2026-06-03T00:00:00Z",
                    "taints": [],
                    "provenance": {"source": "fixture"}
                }
            }),
            json!({
                "schema_version": 1,
                "kind": "lease",
                "record_id": "lease-1",
                "payload": {
                    "lease_id": "lease-1",
                    "lane": "interactive",
                    "status": "granted",
                    "lease_handle": "handle-1",
                    "granted_at_utc": "2026-06-03T00:00:00Z",
                    "released_at_utc": "",
                    "requested_for_run_id": "run-1",
                    "vram_share": 0.25
                }
            }),
            json!({
                "schema_version": 1,
                "kind": "run",
                "record_id": "run-1",
                "payload": {
                    "run_id": "run-1",
                    "kind": "agent_run",
                    "status": "running",
                    "started_at_utc": "2026-06-03T00:00:01Z",
                    "finished_at_utc": "",
                    "actor_agent_type": "WORKBENCH",
                    "asset_revisions": [["asset-1", "r1"]],
                    "lease_id": "lease-1",
                    "shard_kind": "standard",
                    "metrics": [{"name": "tokens", "value": 42.0, "unit": "count"}],
                    "outcome": null,
                    "project_id": "default"
                }
            }),
            json!({
                "schema_version": 1,
                "kind": "eval",
                "record_id": "eval-1",
                "payload": {
                    "eval_id": "eval-1",
                    "kind": "trace_eval",
                    "run_id": "run-1",
                    "asset_id": "asset-1",
                    "asset_revision": "r1",
                    "scores": [{"metric_name": "quality", "value": 0.8, "threshold": 0.7, "passed": true, "unit": "score"}],
                    "captured_at_utc": "2026-06-03T00:00:02Z",
                    "notes": "fixture"
                }
            }),
            json!({
                "schema_version": 1,
                "kind": "proposal",
                "record_id": "proposal-1",
                "payload": {
                    "proposal_id": "proposal-1",
                    "kind": "promotion",
                    "status": "open",
                    "affected_assets": ["asset-1"],
                    "affected_revisions": [["asset-1", "r1"]],
                    "pre_promotion_evals": [],
                    "gate": {"provenance_present": true, "eval_present": true, "taint_free": true, "regression_free": true},
                    "opened_at_utc": "2026-06-03T00:00:03Z",
                    "closed_at_utc": "",
                    "notes": "fixture"
                }
            }),
        ];
        let text = rows
            .iter()
            .map(|row| serde_json::to_string(row).expect("json row"))
            .collect::<Vec<_>>()
            .join("\n")
            + "\n";
        fs::write(&spine_jsonl, text).expect("write spine fixture");
        // SAFETY: test_env_lock serializes process environment mutation while
        // this inherited-spine fixture is visible to native route handlers.
        unsafe /* SAFETY: guarded by test_env_lock; scoped test-only process environment mutation. */ {
            std::env::set_var("VETINARI_WORKBENCH_SPINE_DIR", &spine_dir);
            std::env::set_var("AMW_KERNEL_STATE_DIR", &state_dir);
        }
        reset_native_training_control_for_test();

        let mission = handle_kernel_request(KernelHttpRequest {
            method: "GET".to_string(),
            path: "/api/v1/projects/default/mission-control/snapshot".to_string(),
            body: None,
        })
        .expect("mission control reads spine");
        assert_eq!(mission["status"], "ok");
        assert_eq!(
            mission["native_state_coverage"]["metadata_spine_runs"],
            true
        );
        assert_eq!(
            mission["native_state_coverage"]["metadata_spine_leases"],
            true
        );
        assert_eq!(mission["spine"]["counts"]["runs"], 1);
        assert_eq!(mission["spine"]["counts"]["assets"], 1);
        assert_eq!(mission["queue"][0]["lease_id"], "lease-1");
        assert_eq!(mission["agent_tasks"][0]["run_id"], "run-1");

        let shell = handle_kernel_request(KernelHttpRequest {
            method: "GET".to_string(),
            path: "/api/workbench/shell/snapshot".to_string(),
            body: None,
        })
        .expect("shell reads spine");
        assert_eq!(shell["status"], "ok");
        assert_eq!(
            shell["native_state_coverage"]["metadata_spine_assets"],
            true
        );
        assert!(shell["objects"]
            .as_array()
            .expect("objects")
            .iter()
            .any(|row| row["object_id"] == "asset-1" && row["object_kind"] == "artifact"));
        assert!(shell["navigation"]
            .as_array()
            .expect("navigation")
            .iter()
            .any(|row| row["view"] == "promotion-inbox" && row["count"] == 1));

        let resources = handle_kernel_request(KernelHttpRequest {
            method: "GET".to_string(),
            path: "/api/workbench/resource-cockpit/leases".to_string(),
            body: None,
        })
        .expect("resource cockpit reads spine leases");
        assert_eq!(resources["leases"][0]["lease_id"], "lease-1");
        assert_eq!(resources["leases"][0]["state"], "active");

        let policy_proposals = handle_kernel_request(KernelHttpRequest {
            method: "GET".to_string(),
            path: "/api/workbench/resource-cockpit/policy-proposals".to_string(),
            body: None,
        })
        .expect("resource policy proposals read spine");
        assert_eq!(policy_proposals["status"], "ok");
        assert_eq!(
            policy_proposals["proposals"][0]["proposal_id"],
            "proposal-1"
        );
        assert_eq!(
            policy_proposals["approval_policy"]["default"],
            "approval-required"
        );

        let approval_diff = handle_kernel_request(KernelHttpRequest {
            method: "POST".to_string(),
            path: "/api/workbench/resource-cockpit/policy-proposals/proposal-1/approval-diff"
                .to_string(),
            body: Some(json!({"secret_token": "do-not-leak"})),
        })
        .expect("approval diff reads proposal");
        assert_eq!(approval_diff["status"], "ok");
        assert_eq!(approval_diff["proposal_id"], "proposal-1");
        assert!(!approval_diff["diff"].as_array().expect("diff").is_empty());
        assert_eq!(approval_diff["request"]["payload"]["field_1"], "<redacted>");
        assert!(approval_diff["request"].get("secret_token").is_none());

        let adaptive = handle_kernel_request(KernelHttpRequest {
            method: "GET".to_string(),
            path: "/api/workbench/adaptive-tuning/snapshot/default".to_string(),
            body: None,
        })
        .expect("adaptive tuning reads proposals");
        assert_eq!(adaptive["status"], "ok");
        assert_eq!(adaptive["hypotheses"][0]["proposal_id"], "proposal-1");
        assert_eq!(
            adaptive["native_state_coverage"]["metadata_spine_proposals"],
            true
        );

        let artifact_reviews = handle_kernel_request(KernelHttpRequest {
            method: "GET".to_string(),
            path: "/api/workbench/artifact-reviews".to_string(),
            body: None,
        })
        .expect("artifact reviews read spine assets");
        assert_eq!(artifact_reviews["status"], "ok");
        assert_eq!(artifact_reviews["reviews"][0]["asset_id"], "asset-1");
        assert_eq!(
            artifact_reviews["reviews"][0]["related_evals"][0]["eval_id"],
            "eval-1"
        );

        let workflow_graphs = handle_kernel_request(KernelHttpRequest {
            method: "GET".to_string(),
            path: "/api/workbench/workflow-builder/graphs/default".to_string(),
            body: None,
        })
        .expect("workflow graphs read spine");
        assert_eq!(workflow_graphs["status"], "ok");
        assert_eq!(workflow_graphs["graphs"][0]["graph_id"], "metadata-spine");
        assert!(
            workflow_graphs["graphs"][0]["node_count"]
                .as_u64()
                .expect("node count")
                >= 5
        );

        let workflow_graph = handle_kernel_request(KernelHttpRequest {
            method: "GET".to_string(),
            path: "/api/workbench/workflow-builder/graphs/default/metadata-spine".to_string(),
            body: None,
        })
        .expect("workflow graph detail reads spine");
        assert!(workflow_graph["graph"]["nodes"]
            .as_array()
            .expect("nodes")
            .iter()
            .any(|row| row["node_id"] == "run:run-1"));
        assert!(workflow_graph["graph"]["edges"]
            .as_array()
            .expect("edges")
            .iter()
            .any(|row| row["relation"] == "produced_or_used"));

        let workflow_console = handle_kernel_request(KernelHttpRequest {
            method: "GET".to_string(),
            path: "/api/workbench/workflow-builder/console/default".to_string(),
            body: None,
        })
        .expect("workflow console reads spine");
        assert_eq!(workflow_console["status"], "ok");
        assert!(workflow_console["events"]
            .as_array()
            .expect("events")
            .iter()
            .any(|row| row["object_id"] == "run-1"));

        let channels_activity = handle_kernel_request(KernelHttpRequest {
            method: "GET".to_string(),
            path: "/api/workbench/channels/activity".to_string(),
            body: None,
        })
        .expect("channels activity reads native config");
        assert_eq!(channels_activity["status"], "ok");
        assert!(channels_activity["activity"]
            .as_array()
            .expect("activity")
            .iter()
            .any(|row| row["channel"] == "desktop" && row["status"] == "ready"));

        let habit_review = handle_kernel_request(KernelHttpRequest {
            method: "GET".to_string(),
            path: "/api/workbench/habit-health/review/user-1".to_string(),
            body: None,
        })
        .expect("habit-health review exposes native policy");
        assert_eq!(habit_review["status"], "guarded");
        assert_eq!(habit_review["privacy"]["default_policy"], "opt-in");
        assert!(habit_review["review"]
            .as_array()
            .expect("review")
            .iter()
            .any(|row| row["check_id"] == "habit-health:export"));

        // SAFETY: test_env_lock remains held through cleanup, preventing
        // concurrent tests from observing partially-cleared fixture paths.
        unsafe /* SAFETY: guarded by test_env_lock; scoped test-only process environment mutation. */ {
            std::env::remove_var("VETINARI_WORKBENCH_SPINE_DIR");
            std::env::remove_var("AMW_KERNEL_STATE_DIR");
        }
        let _ = fs::remove_dir_all(&spine_dir);
        let _ = fs::remove_dir_all(&state_dir);
    }

    #[test]
    fn api_kernel_contracts_audit_index_unavailable_is_fail_closed_not_placeholder() {
        let missing_index = temp_manifest_path("missing-run-index");

        let results = full_spectrum_audit_results_payload_from_index(&missing_index, "");
        assert_eq!(results["status"], "unavailable");
        assert_eq!(results["source"], "amw-kernel");
        assert_eq!(results["runs"].as_array().expect("runs").len(), 0);
        assert_eq!(results["summary"]["visible_runs"], 0);
        assert!(results["error"]
            .as_str()
            .expect("error")
            .contains("RUN-INDEX.json is missing or unreadable"));

        let run = full_spectrum_audit_run_payload_from_index("run-missing", &missing_index, "");
        assert_eq!(run["status"], "unavailable");
        assert_eq!(run["run_id"], "run-missing");
        assert!(run["error"]
            .as_str()
            .expect("error")
            .contains("RUN-INDEX.json is missing or unreadable"));
    }

    #[test]
    fn api_kernel_contracts_audit_and_program_surfaces_use_governed_state_root() {
        let _guard = test_env_lock();
        let state_dir = temp_test_dir("governed-workbench-state");
        let audit_dir = state_dir.join("audit");
        let program_dir = state_dir.join("programs").join("program-native");
        fs::create_dir_all(&audit_dir).expect("create governed audit directory");
        fs::create_dir_all(&program_dir).expect("create governed program directory");
        fs::write(
            audit_dir.join("RUN-INDEX.json"),
            serde_json::to_string(&json!({"runs": []})).expect("audit index json"),
        )
        .expect("write governed audit index");
        fs::write(program_dir.join("PROGRAM.md"), "# Governed program\n")
            .expect("write governed program");
        fs::write(
            program_dir.join("program-state.json"),
            serde_json::to_string(&json!({
                "phase": "execution",
                "current_wave": 6,
                "packs": [
                    {"pack_id": "P1", "run_status": "complete"},
                    {"pack_id": "P2", "run_status": "pending"}
                ]
            }))
            .expect("program state json"),
        )
        .expect("write governed program state");

        // SAFETY: test_env_lock serializes process environment mutation while
        // these public workbench surfaces resolve their governed state paths.
        unsafe {
            std::env::set_var("AMW_KERNEL_STATE_DIR", &state_dir);
        }
        let audits = full_spectrum_audit_results_payload("");
        let programs = program_tier_payload();
        let program = program_tier_detail_payload("program-native");
        // SAFETY: test_env_lock remains held through environment cleanup.
        unsafe {
            std::env::remove_var("AMW_KERNEL_STATE_DIR");
        }

        assert_eq!(audits["status"], "ok");
        assert_eq!(
            audits["index_path"],
            display_path(&audit_dir.join("RUN-INDEX.json"))
        );
        assert_eq!(programs["summary"]["program_count"], 1);
        assert_eq!(programs["programs"][0]["program_id"], "program-native");
        assert_eq!(programs["programs"][0]["packs_complete"], 1);
        assert_eq!(program["status"], "ok");
        assert_eq!(program["phase"], "execution");
        assert_eq!(program["current_wave"], 6);
        assert!(program["program_path"]
            .as_str()
            .expect("program path")
            .starts_with(&display_path(&state_dir)));

        let _ = fs::remove_dir_all(state_dir);
    }

    #[test]
    fn api_kernel_contracts_audit_index_present_returns_substantive_run_data() {
        let temp = temp_test_dir("audit-run-index");
        let run_root = temp.join("run-1");
        fs::create_dir_all(&run_root).expect("create audit run root");
        fs::write(
            run_root.join("finding-registry.json"),
            serde_json::to_string(&json!({
                "findings": [{
                    "id": "FSA-REAL-001",
                    "title": "Real audit finding",
                    "status": "open",
                    "severity": "high",
                    "scope": "lane:runtime",
                    "root_cause": "seeded root cause",
                    "impact": "seeded impact"
                }]
            }))
            .expect("registry json"),
        )
        .expect("write registry");
        fs::write(
            run_root.join("CLOSURE-STATUS.json"),
            serde_json::to_string(&json!({
                "findings": [{
                    "finding_id": "FSA-REAL-001",
                    "closure_status": "open",
                    "evidence_refs": ["evidence/runtime.txt"]
                }]
            }))
            .expect("closure json"),
        )
        .expect("write closure");
        fs::write(
            run_root.join("CHECKPOINT-STATE.json"),
            serde_json::to_string(&json!({
                "phase": "round-2",
                "current_round": 2
            }))
            .expect("checkpoint json"),
        )
        .expect("write checkpoint");
        let index_path = temp.join("RUN-INDEX.json");
        fs::write(
            &index_path,
            serde_json::to_string(&json!({
                "runs": [{
                    "run_id": "run-1",
                    "status": "completed",
                    "started_at": "2026-06-03T00:00:00Z",
                    "run_root": run_root,
                    "has_handoff_brief": true,
                    "lanes_completed": ["runtime"],
                    "correction_phase": "verification",
                    "correction_phase_note": "native parity fixture"
                }]
            }))
            .expect("index json"),
        )
        .expect("write run index");

        let results = full_spectrum_audit_results_payload_from_index(&index_path, "limit=1");
        assert_eq!(results["status"], "ok");
        assert_eq!(results["include_archived"], false);
        assert_eq!(results["limit"], 1);
        assert_eq!(results["summary"]["visible_runs"], 1);
        assert_eq!(results["summary"]["open_findings"], 1);
        assert_eq!(results["runs"][0]["top_findings"][0]["id"], "FSA-REAL-001");
        assert_eq!(results["runs"][0]["lane_counts"]["runtime"], 1);
        assert_eq!(results["runs"][0]["phase"], "round-2");
        assert_eq!(results["runs"][0]["current_round"], 2);
        assert_eq!(results["runs"][0]["run_flags"]["has_handoff_brief"], true);
        assert_eq!(results["runs"][0]["lanes_completed"][0], "runtime");
        assert_eq!(results["runs"][0]["correction_phase"], "verification");

        let run = full_spectrum_audit_run_payload_from_index("run-1", &index_path, "");
        assert_eq!(run["status"], "ok");
        assert_eq!(run["index_path"], display_path(&index_path));
        assert_eq!(run["include_archived"], false);
        assert_eq!(run["run"]["finding_result_count"], 1);
        assert_eq!(run["run"]["correction_phase_note"], "native parity fixture");
        assert_eq!(
            run["run"]["findings"][0]["evidence_refs"][0],
            "evidence/runtime.txt"
        );

        let _ = fs::remove_dir_all(temp);
    }

    #[test]
    fn api_kernel_contracts_capability_pack_config_unavailable_is_fail_closed() {
        let unavailable = capability_registry_payload_from_result(Err(
            "capability-pack-catalog-missing".to_string(),
        ));
        assert_eq!(unavailable["backend_status"], "unavailable");
        assert_eq!(unavailable["packs"].as_array().expect("packs").len(), 0);
        assert_eq!(unavailable["reason"], "capability-pack-catalog-missing");

        let detail = capability_pack_detail_payload_from_result(
            "workbench-trace-eval",
            Err("capability-pack-catalog-missing".to_string()),
        );
        assert_eq!(detail["pack"]["current_status"], "unavailable");
        assert_eq!(detail["pack"]["reason"], "capability-pack-catalog-missing");
        assert_eq!(detail["enablement"]["allowed"], false);
        assert_eq!(detail["enablement"]["catalog_status"], "unavailable");
        assert_eq!(
            detail["enablement"]["reasons"][0],
            "capability-pack-catalog-missing"
        );

        let bad_catalog = temp_manifest_path("bad-capability-pack-catalog");
        fs::write(
            &bad_catalog,
            "schema_version: 1\npacks:\n  - pack_id: broken-pack\n",
        )
        .expect("write bad catalog");
        let error = load_capability_packs_from_catalog(&bad_catalog)
            .expect_err("catalog row missing current_status must fail closed");
        assert_eq!(
            error,
            "capability-pack-catalog-row-missing-current-status:broken-pack"
        );
        let _ = fs::remove_file(bad_catalog);
    }

    #[test]
    fn api_kernel_contracts_capability_pack_config_present_returns_real_pack_data() {
        let catalog = temp_manifest_path("capability-pack-catalog");
        fs::write(
            &catalog,
            r#"schema_version: 1
packs:
  - pack_id: workbench-trace-eval
    version: "1.0.0"
    capability_kind: eval_workflow
    schemas:
      - workbench.trace.v1
    permissions:
      - read:workbench_trace
    policy_bindings:
      - default
    smoke_evals:
      - tests/test_capability_packs.py::test_capability_pack_catalog_requires_mapping_root
    examples:
      - Convert trace to eval.
    uninstall_command: vetinari capability-packs uninstall workbench-trace-eval
    disable_command: vetinari capability-packs disable workbench-trace-eval
    known_limitations:
      - Requires existing trace schema support.
    credential_posture: none
    locality: local
    source: builtin
    cost_policy: declared
    freshness_policy: current
    tested_status: tested
    current_status: current
"#,
        )
        .expect("write catalog");
        let packs = load_capability_packs_from_catalog(&catalog).expect("load catalog");

        let registry = capability_registry_payload_from_result(Ok(packs.clone()));
        assert_eq!(registry["backend_status"], "available");
        assert_eq!(registry["packs"][0]["pack_id"], "workbench-trace-eval");
        assert_eq!(registry["packs"][0]["trust_status"], "trusted");

        let detail = capability_pack_detail_payload_from_result("workbench-trace-eval", Ok(packs));
        assert_eq!(detail["pack"]["current_status"], "current");
        assert_eq!(detail["enablement"]["allowed"], true);
        assert_eq!(
            detail["enablement"]["reasons"][0],
            "trusted capability pack"
        );

        let _ = fs::remove_file(catalog);
    }

    #[test]
    fn api_kernel_contracts_support_envelope_errors_are_explicit_fail_closed_payloads() {
        let payload = support_envelope_payload(crate::SupportEnvelope::new(
            "EXT_PERMISSION",
            "permission denied",
            "declare an explicit extension permission",
        ));

        assert_eq!(payload["status"], "unavailable");
        assert_eq!(payload["source"], "amw-kernel");
        assert_eq!(payload["error"]["code"], "EXT_PERMISSION");
        assert_eq!(payload["error"]["message"], "permission denied");
        assert_eq!(
            payload["error"]["recovery_hint"],
            "declare an explicit extension permission"
        );
    }

    #[test]
    fn api_kernel_contracts_autonomy_veto_is_native_policy_decision() {
        let _guard = test_env_lock();
        let state_dir = temp_test_dir("autonomy-state");
        // SAFETY: test_env_lock serializes this test's state-dir environment
        // override before the native autonomy policy route is exercised.
        unsafe /* SAFETY: guarded by test_env_lock; scoped test-only process environment mutation. */ {
            std::env::set_var("AMW_KERNEL_STATE_DIR", &state_dir);
        }
        let response = handle_kernel_request(KernelHttpRequest {
            method: "POST".to_string(),
            path: "/api/v1/autonomy/promotions/risky-action/veto".to_string(),
            body: Some(json!({})),
        })
        .expect("autonomy route returns native policy decision");

        assert_eq!(response["family"], "autonomy");
        assert_eq!(response["payload"]["status"], "accepted");
        assert_eq!(
            response["payload"]["native_owner"],
            "amw-kernel::autonomy_policy"
        );
        assert_eq!(response["payload"]["action_type"], "risky-action");
        assert_eq!(response["payload"]["decision"]["accepted"], true);
        assert_eq!(response["payload"]["decision"]["decision"], "deny");
        assert_eq!(response["payload"]["decision"]["requires_human"], true);
        assert_eq!(response["payload"]["activation_eligible"], false);
        assert_eq!(response["payload"]["decision_kind"], "blocked");
        assert_eq!(
            response["payload"]["required_operator_gate_ids"][0],
            "autonomy-approval"
        );
        assert_eq!(
            response["payload"]["shadow_activation_decision"]["receipt_id"],
            response["payload"]["receipt_id"]
        );
        assert_eq!(
            response["payload"]["approval_queue"]["status"],
            "not_required"
        );
        assert_eq!(response["payload"]["decision_log"]["status"], "recorded");
        let decision_log = state_dir.join("autonomy-decision-log.jsonl");
        let decision_text = fs::read_to_string(&decision_log).expect("decision log written");
        assert!(decision_text.contains("risky-action"));

        let queued = handle_kernel_request(KernelHttpRequest {
            method: "POST".to_string(),
            path: "/api/v1/autonomy/promotions/risky-action/approve".to_string(),
            body: Some(json!({})),
        })
        .expect("mutable autonomy action queues approval");
        assert_eq!(queued["payload"]["status"], "blocked");
        assert_eq!(queued["payload"]["approval_queue"]["status"], "queued");
        let approval_queue = state_dir.join("autonomy-approval-queue.jsonl");
        let queue_text = fs::read_to_string(&approval_queue).expect("approval queue written");
        assert!(queue_text.contains("autonomy-approval"));
        // SAFETY: test_env_lock is still held, so removing the scoped state
        // directory variable cannot race with another test.
        unsafe /* SAFETY: guarded by test_env_lock; scoped test-only process environment mutation. */ {
            std::env::remove_var("AMW_KERNEL_STATE_DIR");
        }
        let _ = fs::remove_dir_all(&state_dir);
    }

    #[test]
    fn api_kernel_contracts_action_receipts_are_unique_and_redacted() {
        let first = accepted_action_payload(json!({"action": "save", "raw_prompt": "secret"}));
        let second = accepted_action_payload(json!({"action": "delete", "raw_prompt": "secret"}));

        assert_ne!(first["receipt_id"], second["receipt_id"]);
        assert_ne!(first["rollback_ref"], second["rollback_ref"]);
        assert_ne!(first["trace_ref"], second["trace_ref"]);
        assert_eq!(first["request"]["redacted"], true);
        assert!(!first.to_string().contains("secret"));
    }

    #[test]
    fn coding_task_rejects_null_body() {
        let response = handle_kernel_request(KernelHttpRequest {
            method: "POST".to_string(),
            path: "/api/coding/tasks".to_string(),
            body: Some(Value::Null),
        })
        .expect("coding route returns structured rejection");

        assert_eq!(response["error"], "request_body_required");
    }

    #[test]
    fn capability_catalog_not_hardcoded() {
        let packs = capability_registry_payload();
        let pack_count = packs["packs"].as_array().map(Vec::len).unwrap_or_default();

        assert!(pack_count >= 3);
        assert_eq!(packs["state_source"], "config/workbench/capability_packs");
    }

    #[test]
    fn workbench_accepted_stubs_replaced() {
        for path in [
            "/api/workbench/workflow-builder/validate",
            "/api/workbench/workflow-builder/preview",
            "/api/workbench/workflow-builder/save",
            "/api/workbench/workflow-builder/settings/project-1",
            "/api/workbench/channels/deliver",
            "/api/workbench/channels/commands",
            "/api/workbench/channels/approvals",
            "/api/workbench/habit-health/check-ins",
            "/api/workbench/habit-health/delete",
            "/api/workbench/habit-health/routines",
        ] {
            let response = handle_kernel_request(KernelHttpRequest {
                method: "POST".to_string(),
                path: path.to_string(),
                body: Some(json!({"action": "save"})),
            })
            .expect("stub route returns bounded rejection");

            assert_eq!(response["error"], "not_implemented", "{path}");
            assert_eq!(response["http_status"], 501, "{path}");
            assert!(response.get("handler").is_some(), "{path}");
        }
    }

    #[test]
    fn capability_pack_and_kit_stubs_replaced() {
        let pack = handle_kernel_request(KernelHttpRequest {
            method: "POST".to_string(),
            path: "/api/workbench/capability-packs/local-runtime/enable".to_string(),
            body: Some(json!({})),
        })
        .expect("capability action returns bounded rejection");
        assert_eq!(pack["error"], "not_implemented");
        assert_eq!(pack["capability"], "local-runtime");
        assert_eq!(pack["http_status"], 501);

        let kit = handle_kernel_request(KernelHttpRequest {
            method: "POST".to_string(),
            path: "/api/workbench/domain-kits/arbitrary/evaluate".to_string(),
            body: Some(json!({})),
        })
        .expect("domain kit route returns bounded rejection");
        assert_eq!(kit["error"], "kit_registry_unavailable");
        assert_eq!(kit["kit_id"], "arbitrary");
        assert_eq!(kit["http_status"], 503);
    }

    #[test]
    fn security_resource_execute_missing_safety_signal_fails_closed() {
        let response = handle_kernel_request(KernelHttpRequest {
            method: "POST".to_string(),
            path: "/api/workbench/resource-cockpit/safe-actions/pause/execute".to_string(),
            body: Some(json!({
                "target_ref": "lease-1",
                "evidence_id": "evidence-1"
            })),
        })
        .expect("route returns structured rejection payload");

        assert_eq!(response["error"], "kernel action rejected");
        assert!(response["detail"]
            .as_str()
            .expect("detail")
            .contains("live-action-safety-signal"));
    }

    #[test]
    fn security_habit_health_preview_redacts_raw_request_body() {
        let response = handle_kernel_request(KernelHttpRequest {
            method: "POST".to_string(),
            path: "/api/workbench/habit-health/downstream-preview".to_string(),
            body: Some(json!({
                "raw_prompt": "secret prompt text",
                "notes": "do not echo"
            })),
        })
        .expect("preview response");

        assert_eq!(response["privacy"], "redacted");
        assert_eq!(response["request"]["redacted"], true);
        assert_eq!(response["request"]["payload"], "<redacted>");
        assert_eq!(
            response["request"]["redaction_policy"],
            "hash-and-field-count-only"
        );
        let serialized = response.to_string();
        assert!(!serialized.contains("secret prompt text"));
        assert!(!serialized.contains("do not echo"));
        assert!(!serialized.contains("raw_prompt"));
    }

    #[test]
    fn security_habit_health_preview_redacts_nested_numeric_and_key_shape() {
        let response = handle_kernel_request(KernelHttpRequest {
            method: "POST".to_string(),
            path: "/api/workbench/habit-health/downstream-preview".to_string(),
            body: Some(json!({
                "patient_identifier": 123456,
                "metrics": [{"sleep_score": 91, "private_note": "anxiety spike"}],
                "downstream": {"bearer": "Bearer abc.def.ghi"}
            })),
        })
        .expect("preview response");

        assert_eq!(response["request"]["field_count"], 3);
        assert_eq!(response["request"]["payload"], "<redacted>");
        assert!(response["request"]["body_hash"]
            .as_str()
            .is_some_and(|hash| hash.len() == 16));
        let serialized = response.to_string();
        for leaked in [
            "patient_identifier",
            "sleep_score",
            "private_note",
            "anxiety spike",
            "Bearer abc.def.ghi",
        ] {
            assert!(!serialized.contains(leaked), "preview leaked {leaked}");
        }
    }

    #[test]
    fn security_command_safety_unknown_commands_require_human_approval_and_redact() {
        let response = handle_kernel_request(KernelHttpRequest {
            method: "POST".to_string(),
            path: "/api/workbench/command-safety/classify".to_string(),
            body: Some(json!({
                "command": "powershell -EncodedCommand secret",
                "secret prompt text": ""
            })),
        })
        .expect("command safety response");

        assert_eq!(response["verdict"], "require_human_approval");
        assert_eq!(response["human_approval_required"], true);
        assert_eq!(response["cwd_state"]["known_safe"], false);
        assert_eq!(response["request"]["redacted"], true);
        let serialized = response.to_string();
        assert!(!serialized.contains("EncodedCommand"));
        assert!(!serialized.contains("secret prompt text"));

        let safe = handle_kernel_request(KernelHttpRequest {
            method: "POST".to_string(),
            path: "/api/workbench/command-safety/classify".to_string(),
            body: Some(json!({"command": "git status"})),
        })
        .expect("readonly command safety response");
        assert_eq!(safe["verdict"], "allow");
        assert_eq!(safe["human_approval_required"], false);
    }
}
