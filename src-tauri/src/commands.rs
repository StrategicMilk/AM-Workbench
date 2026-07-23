use crate::lifecycle::{CommandOrigin, LifecycleCommandRequest, LifecycleShellHost};
use amw_kernel::{
    api_domain_authorities, execute_live_action, handle_kernel_request, require_domain_authority,
    workbench_surface_policies, KernelHttpRequest, LiveActionSignal,
};
use serde_json::{json, Value};
use std::sync::{Mutex, OnceLock};

static LIFECYCLE_HOST: OnceLock<Mutex<LifecycleShellHost>> = OnceLock::new();

fn lifecycle_host() -> &'static Mutex<LifecycleShellHost> {
    LIFECYCLE_HOST.get_or_init(|| Mutex::new(LifecycleShellHost::default()))
}

#[tauri::command]
pub fn workbench_lifecycle_command(payload: Value) -> Result<Value, String> {
    let mut request: LifecycleCommandRequest = serde_json::from_value(payload)
        .map_err(|err| format!("invalid lifecycle payload: {err}"))?;
    request = request
        .with_origin(CommandOrigin::Tauri)
        .without_renderer_admin_equivalence();
    let mut guarded = lifecycle_host()
        .lock()
        .map_err(|_| "lifecycle host lock poisoned".to_string())?;
    let result = guarded.execute(request);
    serde_json::to_value(result).map_err(|err| format!("failed to encode lifecycle result: {err}"))
}

#[tauri::command]
pub fn vetinari_status() -> Result<Value, String> {
    Ok(json!({
        "status": "ok",
        "server": "amw-kernel",
        "version": env!("CARGO_PKG_VERSION"),
        "api_domains": api_domain_authorities().len(),
        "workbench_surfaces": workbench_surface_policies().len(),
    }))
}

#[tauri::command]
pub fn vetinari_api_domains() -> Result<Value, String> {
    Ok(json!({
        "domains": api_domain_authorities()
            .iter()
            .map(|domain| json!({
                "domain_id": domain.domain_id,
                "authority": domain.authority,
                "state_store": domain.state_store,
                "route_prefix": domain.native_route.route_prefix,
                "route_module": domain.native_route.route_module,
                "route_mode": domain.native_route.route_mode,
                "required_signals": domain.required_signals,
            }))
            .collect::<Vec<_>>(),
    }))
}

#[tauri::command]
pub fn vetinari_require_domain(payload: Value) -> Result<Value, String> {
    let domain_id = payload
        .get("domain_id")
        .and_then(Value::as_str)
        .ok_or_else(|| "missing domain_id".to_string())?;
    let domain = require_domain_authority(domain_id).map_err(|err| format!("{err:?}"))?;
    Ok(json!({
        "domain_id": domain.domain_id,
        "authority": domain.authority,
        "state_store": domain.state_store,
        "required_signals": domain.required_signals,
    }))
}

#[tauri::command]
pub fn vetinari_workbench_surfaces() -> Result<Value, String> {
    Ok(json!({
        "surfaces": workbench_surface_policies()
            .iter()
            .map(|surface| json!({
                "surface_id": surface.surface_id,
                "default_policy": surface.default_policy,
                "live_action_path": surface.live_action_path,
                "rollback_path": surface.rollback_path,
                "downstream_consumer": surface.downstream_consumer,
            }))
            .collect::<Vec<_>>(),
    }))
}

#[tauri::command]
pub fn vetinari_resource_action(payload: Value) -> Result<Value, String> {
    let action_id = payload
        .get("action_id")
        .and_then(Value::as_str)
        .ok_or_else(|| "missing action_id".to_string())?;
    let target_ref = payload
        .get("target_ref")
        .and_then(Value::as_str)
        .ok_or_else(|| "missing target_ref".to_string())?;
    let evidence_id = payload
        .get("evidence_id")
        .and_then(Value::as_str)
        .ok_or_else(|| "missing evidence_id".to_string())?;
    let safety_signal_present = payload
        .get("safety_signal_present")
        .and_then(Value::as_bool)
        .unwrap_or(false);
    let approval_ref = payload
        .get("approval_ref")
        .and_then(Value::as_str)
        .map(str::to_string);

    let receipt = execute_live_action(
        action_id,
        LiveActionSignal {
            target_ref: target_ref.to_string(),
            evidence_id: evidence_id.to_string(),
            safety_signal_present,
            approval_ref,
        },
    )
    .map_err(|err| format!("{err:?}"))?;
    Ok(json!({
        "receipt_id": receipt.receipt_id,
        "target_ref": receipt.target_ref,
        "status": receipt.status,
        "rollback_ref": receipt.rollback_ref,
    }))
}

#[tauri::command]
pub fn vetinari_kernel_request(payload: Value) -> Result<Value, String> {
    let method = payload
        .get("method")
        .and_then(Value::as_str)
        .ok_or_else(|| "missing method".to_string())?;
    let path = payload
        .get("path")
        .and_then(Value::as_str)
        .ok_or_else(|| "missing path".to_string())?;
    validate_tauri_kernel_proxy_policy(method, path)?;
    let body = payload.get("body").cloned();
    handle_kernel_request(KernelHttpRequest {
        method: method.to_string(),
        path: path.to_string(),
        body,
    })
}

#[tauri::command]
pub fn vetinari_training_status() -> Result<Value, String> {
    handle_kernel_request(KernelHttpRequest {
        method: "GET".to_string(),
        path: "/api/v1/training/status".to_string(),
        body: None,
    })
}

#[tauri::command]
pub fn vetinari_mcp_tools() -> Result<Value, String> {
    handle_kernel_request(KernelHttpRequest {
        method: "GET".to_string(),
        path: "/mcp/tools".to_string(),
        body: None,
    })
}

#[tauri::command]
pub fn vetinari_mission_control_snapshot(payload: Value) -> Result<Value, String> {
    let project_id = payload
        .get("project_id")
        .and_then(Value::as_str)
        .filter(|value| !value.trim().is_empty())
        .ok_or_else(|| "missing project_id".to_string())?;
    validate_project_id(project_id)?;
    handle_kernel_request(KernelHttpRequest {
        method: "GET".to_string(),
        path: format!("/api/v1/projects/{project_id}/mission-control/snapshot"),
        body: None,
    })
}

pub fn configure_builder<R: tauri::Runtime>(builder: tauri::Builder<R>) -> tauri::Builder<R> {
    builder.invoke_handler(tauri::generate_handler![
        workbench_lifecycle_command,
        vetinari_status,
        vetinari_api_domains,
        vetinari_require_domain,
        vetinari_workbench_surfaces,
        vetinari_resource_action,
        vetinari_kernel_request,
        vetinari_training_status,
        vetinari_mcp_tools,
        vetinari_mission_control_snapshot,
    ])
}

fn is_native_project_route(route: &str) -> bool {
    let segments: Vec<&str> = route.trim_matches('/').split('/').collect();
    matches!(
        segments.as_slice(),
        ["api", "v1", "projects", _, "mission-control", ..]
            | ["api", "v1", "projects", _, "workbench", ..]
    )
}

fn is_v1_workbench_gateway_policy_route(route: &str) -> bool {
    let segments: Vec<&str> = route.trim_matches('/').split('/').collect();
    match segments.as_slice() {
        ["api", "v1", "workbench", project_id, "gateway-policy", ..] => {
            validate_project_id(project_id).is_ok()
        }
        _ => false,
    }
}

fn route_is_or_child(route: &str, prefix: &str) -> bool {
    route == prefix || route.starts_with(&format!("{prefix}/"))
}

const ENGINE_GET_ROUTES: &[&str] = &[
    "/api/v1/engine/health",
    "/api/v1/engine/metrics",
    "/api/v1/engine/version",
    "/api/v1/engine/agent-stream",
];
const ENGINE_POST_ROUTES: &[&str] = &["/api/v1/engine/agent-stream/cancel"];

fn is_engine_proxy_route(method: &str, route: &str) -> bool {
    match method {
        "GET" => ENGINE_GET_ROUTES.contains(&route),
        "POST" => ENGINE_POST_ROUTES.contains(&route),
        _ => false,
    }
}

fn validate_project_id(project_id: &str) -> Result<(), String> {
    let valid = project_id.len() <= 128
        && project_id.chars().enumerate().all(|(idx, ch)| {
            ch.is_ascii_alphanumeric() || (idx > 0 && matches!(ch, '-' | '_' | '.'))
        });
    if valid {
        Ok(())
    } else {
        Err("invalid project_id".to_string())
    }
}

fn validate_tauri_kernel_proxy_policy(method: &str, path: &str) -> Result<(), String> {
    let method = method.to_ascii_uppercase();
    if !matches!(method.as_str(), "GET" | "POST") {
        return Err(format!(
            "tauri kernel proxy denied: method {method} is not allowed"
        ));
    }
    let route = path.split('?').next().unwrap_or(path);
    let allowed_workbench_prefixes = [
        "/api/workbench/approval-chain",
        "/api/workbench/artifact-reviews",
        "/api/workbench/chat-mode",
        "/api/workbench/command-safety",
        "/api/workbench/console",
        "/api/workbench/context-enrichment",
        "/api/workbench/conversation",
        "/api/workbench/domain-review",
        "/api/workbench/evidence-assets",
        "/api/workbench/evidence-notebooks",
        "/api/workbench/experiment-lab",
        "/api/workbench/method-library",
        "/api/workbench/adaptive-tuning",
        "/api/workbench/resource-cockpit",
        "/api/workbench/capability-packs",
        "/api/workbench/domain-kits",
        "/api/workbench/workflow-builder",
        "/api/workbench/channels",
        "/api/workbench/benchmark",
        "/api/workbench/extensions",
        "/api/workbench/habit-health",
        "/api/workbench/knowledge_vault",
        "/api/workbench/managed-agents",
        "/api/workbench/memory",
        "/api/workbench/memory_refinement",
        "/api/workbench/mode-templates",
        "/api/workbench/model-choices",
        "/api/workbench/playground",
        "/api/workbench/policy-explainability",
        "/api/workbench/preference-cards",
        "/api/workbench/private-ai",
        "/api/workbench/prompt-engineering",
        "/api/workbench/query",
        "/api/workbench/rag",
        "/api/workbench/readiness",
        "/api/workbench/repro-capsules",
        "/api/workbench/run-kernel",
        "/api/workbench/shell",
        "/api/workbench/source-cards",
        "/api/workbench/status",
        "/api/workbench/tool-cards",
        "/api/workbench/tool-guides",
        "/api/workbench/tool-output-squasher",
        "/api/workbench/updates",
        "/api/workbench/work-graph",
        "/api/v1/workbench/annotation",
        "/api/v1/workbench/launcher",
        "/api/v1/workbench/migration",
        "/api/v1/workbench/onboarding",
    ];
    let allowed = route == "/api/v1/kernel/status"
        || route == "/health"
        || route == "/ready"
        || route == "/mcp/tools"
        || route_is_or_child(route, "/api/audit")
        || route_is_or_child(route, "/api/intake")
        || route_is_or_child(route, "/api/models")
        || route_is_or_child(route, "/api/training")
        || route_is_or_child(route, "/api/v1/training")
        || route_is_or_child(route, "/api/v1/workflows")
        || route_is_or_child(route, "/api/v1/autonomy")
        || is_engine_proxy_route(method.as_str(), route)
        || allowed_workbench_prefixes
            .iter()
            .any(|prefix| route_is_or_child(route, prefix))
        || is_native_project_route(route)
        || is_v1_workbench_gateway_policy_route(route);
    if allowed {
        Ok(())
    } else {
        Err(format!("tauri kernel proxy denied: {method} {route}"))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn tauri_kernel_proxy_policy_allows_named_routes_and_denies_arbitrary_http() {
        assert!(validate_tauri_kernel_proxy_policy("GET", "/api/workbench/extensions").is_ok());
        assert!(validate_tauri_kernel_proxy_policy("GET", "/health").is_ok());
        assert!(validate_tauri_kernel_proxy_policy("GET", "/ready").is_ok());
        assert!(validate_tauri_kernel_proxy_policy("GET", "/api/audit").is_ok());
        assert!(
            validate_tauri_kernel_proxy_policy("GET", "/api/audit/full-spectrum/results").is_ok()
        );
        assert!(validate_tauri_kernel_proxy_policy("GET", "/api/v1/workbench").is_err());
        assert!(validate_tauri_kernel_proxy_policy("GET", "/api/models/hub/search").is_ok());
        assert!(validate_tauri_kernel_proxy_policy("POST", "/api/intake").is_ok());
        assert!(validate_tauri_kernel_proxy_policy("POST", "/api/v1/workflows").is_ok());
        assert!(
            validate_tauri_kernel_proxy_policy("GET", "/api/v1/workbench/launcher/status").is_ok()
        );
        assert!(
            validate_tauri_kernel_proxy_policy("GET", "/api/v1/workbench/onboarding/health")
                .is_ok()
        );
        assert!(validate_tauri_kernel_proxy_policy(
            "GET",
            "/api/v1/workbench/default/gateway-policy/profiles"
        )
        .is_ok());
        assert!(
            validate_tauri_kernel_proxy_policy("POST", "/api/v1/workbench/migration/plan").is_ok()
        );
        assert!(validate_tauri_kernel_proxy_policy("POST", "/api/v1/training/pause").is_ok());
        assert!(validate_tauri_kernel_proxy_policy("GET", "/api/v1/kernel/status").is_ok());
        assert!(validate_tauri_kernel_proxy_policy("GET", "/api/v1/engine/health").is_ok());
        assert!(validate_tauri_kernel_proxy_policy("GET", "/api/v1/engine/agent-stream").is_ok());
        assert!(
            validate_tauri_kernel_proxy_policy("POST", "/api/v1/engine/agent-stream/cancel")
                .is_ok()
        );
        assert!(validate_tauri_kernel_proxy_policy("GET", "/api/v1/engine/admin").is_err());
        assert!(
            validate_tauri_kernel_proxy_policy("POST", "/api/v1/engine/agent-stream").is_err()
        );
        assert!(validate_tauri_kernel_proxy_policy("DELETE", "/api/workbench/extensions").is_err());
        assert!(validate_tauri_kernel_proxy_policy("GET", "/api/admin/users").is_err());
        assert!(validate_tauri_kernel_proxy_policy("GET", "/api/projects").is_err());
        assert!(
            validate_tauri_kernel_proxy_policy("POST", "/api/workbench/unreviewed-action").is_err()
        );
        assert!(validate_tauri_kernel_proxy_policy("GET", "/api/v1/projects/default").is_err());
        assert!(validate_tauri_kernel_proxy_policy(
            "GET",
            "/api/v1/projects/default/mission-control/snapshot"
        )
        .is_ok());
        assert!(validate_tauri_kernel_proxy_policy("GET", "https://example.invalid/api").is_err());
    }

    #[test]
    fn tauri_mission_control_requires_explicit_project_id() {
        assert_eq!(
            vetinari_mission_control_snapshot(json!({})).expect_err("missing project id"),
            "missing project_id"
        );
        assert_eq!(
            vetinari_mission_control_snapshot(json!({"project_id": "default/../../admin"}))
                .expect_err("path-like project id"),
            "invalid project_id"
        );
        let snapshot = vetinari_mission_control_snapshot(json!({"project_id": "default"}))
            .expect("snapshot route");
        assert_eq!(snapshot["project_id"], "default");
    }

    #[test]
    fn tauri_training_status_returns_native_control_state() {
        let status = vetinari_training_status().expect("training status route");
        assert_eq!(status["status"], "available");
        assert_eq!(status["state_source"], "amw-kernel::training_control");
        assert!(status.get("is_idle").is_some());
        assert!(status.get("ready_for_training").is_some());
        assert!(status["missing_libraries"].is_array());
        assert!(status.get("current_job").is_some());
        assert!(status.get("records_collected").is_some());
        assert_ne!(status["status"], "ok");
    }
}
