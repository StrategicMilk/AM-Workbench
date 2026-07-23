use amw_tauri_shell::{
    vetinari_kernel_request, vetinari_mission_control_snapshot, vetinari_training_status,
    workbench_lifecycle_command, LifecycleAction, LifecycleCommandRequest,
};

#[test]
fn tauri_contracts_lifecycle_action_is_typed_and_support_bundle_is_reachable() {
    let request: LifecycleCommandRequest = serde_json::from_value(serde_json::json!({
        "action": "support_bundle",
        "origin": "tauri",
        "admin_equivalent": true
    }))
    .expect("typed lifecycle action deserializes");
    assert_eq!(request.action, LifecycleAction::SupportBundle);

    let unknown = serde_json::from_value::<LifecycleCommandRequest>(
        serde_json::json!({"action": "unbounded_shell", "origin": "tauri"}),
    );
    assert!(unknown.is_err());

    let renderer_result = workbench_lifecycle_command(serde_json::json!({
        "action": "support_bundle",
        "admin_equivalent": true
    }))
    .expect("renderer admin equivalence is stripped");
    assert_eq!(renderer_result["accepted"], false);
    assert_eq!(
        renderer_result["denial_reason"],
        "admin-equivalent confirmation required"
    );
}

#[test]
fn tauri_contracts_kernel_proxy_and_mission_control_fail_closed() {
    let allowed = vetinari_kernel_request(serde_json::json!({
        "method": "GET",
        "path": "/api/workbench/extensions"
    }))
    .expect("allowed workbench route");
    assert_eq!(allowed["source"], "amw-kernel");

    let denied = vetinari_kernel_request(serde_json::json!({
        "method": "GET",
        "path": "/api/admin/users"
    }))
    .expect_err("admin route denied by Tauri proxy policy");
    assert!(denied.contains("tauri kernel proxy denied"));

    assert_eq!(
        vetinari_mission_control_snapshot(serde_json::json!({}))
            .expect_err("missing project id fails closed"),
        "missing project_id"
    );
}

#[test]
fn tauri_contracts_training_and_autonomy_are_native_control_surfaces() {
    let training = vetinari_training_status().expect("training status route");
    assert_eq!(training["status"], "available");
    assert_eq!(training["state_source"], "amw-kernel::training_control");
    assert!(training.get("is_idle").is_some());
    assert!(training.get("ready_for_training").is_some());
    assert!(training["missing_libraries"].is_array());
    assert!(training.get("current_job").is_some());
    assert!(training.get("records_collected").is_some());

    let autonomy = vetinari_kernel_request(serde_json::json!({
        "method": "POST",
        "path": "/api/v1/autonomy/promotions/risky-action/veto",
        "body": {}
    }))
    .expect("autonomy route returns native veto decision");
    assert_eq!(autonomy["payload"]["status"], "accepted");
    assert_eq!(
        autonomy["payload"]["native_owner"],
        "amw-kernel::autonomy_policy"
    );
    assert_eq!(autonomy["payload"]["decision"]["decision"], "deny");
    assert_eq!(autonomy["payload"]["activation_eligible"], false);
    assert_eq!(autonomy["payload"]["decision_kind"], "blocked");
    assert_eq!(
        autonomy["payload"]["shadow_activation_decision"]["receipt_id"],
        autonomy["payload"]["receipt_id"]
    );
}
