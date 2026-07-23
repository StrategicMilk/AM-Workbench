use amw_tauri_shell::configure_builder;

fn main() {
    configure_builder(tauri::Builder::default())
        .run(tauri::generate_context!())
        .expect("failed to run AM Workbench Tauri shell");
}

#[cfg(test)]
mod tests {
    use amw_tauri_shell::{
        configure_builder, vetinari_api_domains, vetinari_kernel_request, vetinari_mcp_tools,
        vetinari_mission_control_snapshot, vetinari_require_domain, vetinari_resource_action,
        vetinari_status, vetinari_training_status, vetinari_workbench_surfaces,
        workbench_lifecycle_command,
    };

    #[test]
    fn command_rejects_renderer_admin_equivalence() {
        let result = workbench_lifecycle_command(
            serde_json::json!({"action": "stop", "admin_equivalent": true}),
        )
        .expect("command returns JSON result");

        assert_eq!(result["accepted"], false);
        assert_eq!(result["decision_action"], "block_awaiting_consent");
    }

    #[test]
    fn tauri_builder_registers_lifecycle_invoke_handler() {
        let _app = configure_builder(tauri::test::mock_builder())
            .build(tauri::test::mock_context(tauri::test::noop_assets()))
            .expect("Tauri test app builds with lifecycle invoke handler");
    }

    #[test]
    fn tauri_commands_call_native_kernel_modules() {
        let status = vetinari_status().expect("native kernel status");
        assert_eq!(status["server"], "amw-kernel");
        assert!(status["api_domains"].as_u64().unwrap_or_default() >= 10);

        let domains = vetinari_api_domains().expect("native kernel domains");
        assert!(domains["domains"]
            .as_array()
            .expect("domains array")
            .iter()
            .any(|row| row["domain_id"] == "migration_wizard"));

        let domain = vetinari_require_domain(serde_json::json!({"domain_id": "resource_cockpit"}))
            .expect("domain authority");
        assert_eq!(domain["authority"], "amw-kernel::api");

        let surfaces = vetinari_workbench_surfaces().expect("native surface policies");
        assert!(surfaces["surfaces"]
            .as_array()
            .expect("surfaces array")
            .iter()
            .any(|row| row["surface_id"] == "habit_health" && row["default_policy"] == "opt-in"));
        assert!(surfaces["surfaces"]
            .as_array()
            .expect("surfaces array")
            .iter()
            .any(|row| row["surface_id"] == "extensions_marketplace"
                && row["default_policy"] == "opt-in"));

        let receipt = vetinari_resource_action(serde_json::json!({
            "action_id": "pause",
            "target_ref": "lease-1",
            "evidence_id": "evidence-1",
            "safety_signal_present": true,
        }))
        .expect("resource action receipt");
        assert_eq!(receipt["status"], "accepted");

        let kernel_response = vetinari_kernel_request(serde_json::json!({
            "method": "GET",
            "path": "/api/workbench/workflow-builder/metadata"
        }))
        .expect("native kernel request");
        assert_eq!(kernel_response["source"], "amw-kernel");

        let extensions_response = vetinari_kernel_request(serde_json::json!({
            "method": "GET",
            "path": "/api/workbench/extensions"
        }))
        .expect("native extensions marketplace request");
        assert_eq!(extensions_response["default_policy"], "opt-in");
        assert_eq!(
            extensions_response["extensions"][0]["disabled_by_default"],
            true
        );

        let training = vetinari_training_status().expect("native training status");
        assert_eq!(training["source"], "amw-kernel");
        assert_eq!(training["status"], "available");
        assert_eq!(training["state_source"], "amw-kernel::training_control");
        assert!(training.get("is_idle").is_some());
        assert!(training.get("ready_for_training").is_some());
        assert!(training["missing_libraries"].is_array());
        assert!(training.get("current_job").is_some());
        assert!(training.get("records_collected").is_some());

        let mcp_tools = vetinari_mcp_tools().expect("native MCP tools");
        assert_eq!(mcp_tools["source"], "amw-kernel");

        let mission = vetinari_mission_control_snapshot(serde_json::json!({
            "project_id": "project-1"
        }))
        .expect("native mission control");
        assert_eq!(mission["source"], "amw-kernel");
        assert_eq!(mission["project_id"], "project-1");
    }
}
