pub mod routes;

use crate::error::KernelError;

#[derive(Debug, Clone, Eq, PartialEq)]
pub struct NativeRouteAuthority {
    pub route_prefix: &'static str,
    pub route_module: &'static str,
    pub route_mode: &'static str,
}

#[derive(Debug, Clone, Eq, PartialEq)]
pub struct ApiDomainAuthority {
    pub domain_id: &'static str,
    pub authority: &'static str,
    pub state_store: &'static str,
    pub native_route: NativeRouteAuthority,
    pub required_signals: &'static [&'static str],
}

const REQUIRED_WORKBENCH_SIGNALS: &[&str] = &["authorization", "provenance", "rollback", "receipt"];

const API_DOMAIN_AUTHORITIES: &[ApiDomainAuthority] = &[
    domain(
        "method_library",
        "metadata_spine",
        "/api/workbench/method-library",
    ),
    domain(
        "adaptive_tuning",
        "workbench_adaptive_tuning_store",
        "/api/workbench/adaptive-tuning",
    ),
    domain(
        "resource_cockpit",
        "resource_cockpit_receipts",
        "/api/workbench/resource-cockpit",
    ),
    domain(
        "capability_packs",
        "workbench_capability_catalog",
        "/api/workbench/capability-packs",
    ),
    domain(
        "domain_kits",
        "workbench_domain_kits_catalog",
        "/api/workbench/domain-kits",
    ),
    domain(
        "workflow_builder",
        "workbench_workflow_store",
        "/api/workbench/workflow-builder",
    ),
    domain(
        "channels",
        "workbench_channel_registry",
        "/api/workbench/channels",
    ),
    domain(
        "benchmark_importer",
        "workbench_benchmark_import_receipts",
        "/api/workbench/benchmark",
    ),
    domain(
        "migration_wizard",
        "workbench_migration_checkpoints",
        "/api/v1/workbench/migration",
    ),
    domain(
        "habit_health",
        "workbench_habit_health_opt_in_store",
        "/api/workbench/habit-health",
    ),
    domain(
        "extensions_marketplace",
        "workbench_extension_marketplace_catalog",
        "/api/workbench/extensions",
    ),
    domain(
        "training_controls",
        "training_control_receipts",
        "/api/v1/training",
    ),
    domain("mcp_transport", "mcp_resource_registry", "/mcp"),
    domain(
        "mission_control",
        "workbench_spine",
        "/api/v1/projects/:project_id/mission-control",
    ),
];

pub fn api_domain_authorities() -> &'static [ApiDomainAuthority] {
    API_DOMAIN_AUTHORITIES
}

const fn domain(
    domain_id: &'static str,
    state_store: &'static str,
    route_prefix: &'static str,
) -> ApiDomainAuthority {
    ApiDomainAuthority {
        domain_id,
        authority: "amw-kernel::api",
        state_store,
        native_route: NativeRouteAuthority {
            route_prefix,
            route_module: "crates/amw-kernel/src/api/routes/workbench_domains.rs",
            route_mode: "native_rust",
        },
        required_signals: REQUIRED_WORKBENCH_SIGNALS,
    }
}

pub fn require_domain_authority(
    domain_id: &str,
) -> Result<&'static ApiDomainAuthority, KernelError> {
    api_domain_authorities()
        .iter()
        .find(|authority| authority.domain_id == domain_id)
        .ok_or_else(|| KernelError::UnknownSignal(domain_id.to_string()))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn required_domains_have_kernel_authority_and_native_routes() {
        let authorities = api_domain_authorities();
        assert!(authorities
            .iter()
            .any(|domain| domain.domain_id == "migration_wizard"));
        assert!(authorities
            .iter()
            .any(|domain| domain.domain_id == "extensions_marketplace"));
        assert!(authorities
            .iter()
            .all(|domain| domain.authority.starts_with("amw-kernel")));
        assert!(authorities
            .iter()
            .all(|domain| domain.native_route.route_mode == "native_rust"));
        assert!(authorities.iter().all(|domain| {
            domain.native_route.route_prefix.starts_with("/api/")
                || domain.native_route.route_prefix == "/mcp"
        }));
    }

    #[test]
    fn api_kernel_contracts_route_prefix_policy_allows_only_api_and_mcp_authorities() {
        for domain in api_domain_authorities() {
            let prefix = domain.native_route.route_prefix;
            assert!(
                prefix.starts_with("/api/") || prefix == "/mcp",
                "unexpected native route prefix for {}: {}",
                domain.domain_id,
                prefix
            );
        }

        let synthetic = NativeRouteAuthority {
            route_prefix: "/admin",
            route_module: "synthetic",
            route_mode: "native_rust",
        };
        assert!(!synthetic.route_prefix.starts_with("/api/") && synthetic.route_prefix != "/mcp");
    }

    #[test]
    fn unknown_domain_fails_closed() {
        assert!(matches!(
            require_domain_authority("unknown"),
            Err(KernelError::UnknownSignal(value)) if value == "unknown"
        ));
    }
}
