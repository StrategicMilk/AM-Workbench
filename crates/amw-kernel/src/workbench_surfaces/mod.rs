use serde::Serialize;
use std::str::FromStr;

#[derive(Debug, Clone, Eq, PartialEq)]
pub struct WorkbenchSurfacePolicy {
    pub surface_id: WorkbenchSurfaceId,
    pub default_policy: SurfaceDefaultPolicy,
    pub live_action_path: &'static str,
    pub rollback_path: &'static str,
    pub downstream_consumer: &'static str,
}

#[derive(Debug, Clone, Copy, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum WorkbenchSurfaceId {
    MethodLibrary,
    AdaptiveTuning,
    ResourceCockpit,
    CapabilityPacks,
    DomainKits,
    WorkflowBuilder,
    Channels,
    BenchmarkImporter,
    MigrationWizard,
    HabitHealth,
    ExtensionsMarketplace,
}

impl WorkbenchSurfaceId {
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::MethodLibrary => "method_library",
            Self::AdaptiveTuning => "adaptive_tuning",
            Self::ResourceCockpit => "resource_cockpit",
            Self::CapabilityPacks => "capability_packs",
            Self::DomainKits => "domain_kits",
            Self::WorkflowBuilder => "workflow_builder",
            Self::Channels => "channels",
            Self::BenchmarkImporter => "benchmark_importer",
            Self::MigrationWizard => "migration_wizard",
            Self::HabitHealth => "habit_health",
            Self::ExtensionsMarketplace => "extensions_marketplace",
        }
    }
}

#[derive(Debug, Clone, Copy, Eq, PartialEq, Serialize)]
#[serde(rename_all = "kebab-case")]
pub enum SurfaceDefaultPolicy {
    DefaultOn,
    OptIn,
}

impl SurfaceDefaultPolicy {
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::DefaultOn => "default-on",
            Self::OptIn => "opt-in",
        }
    }
}

impl FromStr for SurfaceDefaultPolicy {
    type Err = &'static str;

    fn from_str(value: &str) -> Result<Self, Self::Err> {
        match value {
            "default-on" => Ok(Self::DefaultOn),
            "opt-in" => Ok(Self::OptIn),
            _ => Err("unknown workbench surface policy"),
        }
    }
}

const WORKBENCH_SURFACE_POLICIES: &[WorkbenchSurfacePolicy] = &[
    surface(
        WorkbenchSurfaceId::MethodLibrary,
        SurfaceDefaultPolicy::DefaultOn,
        "vetinari.workbench.method_library.MethodLibrary.list_cards",
        "read-only-derived-view",
        "ui/svelte/src/views/MethodLibraryView.svelte",
    ),
    surface(
        WorkbenchSurfaceId::AdaptiveTuning,
        SurfaceDefaultPolicy::DefaultOn,
        "vetinari.workbench.adaptive_tuning.engine.AdaptiveTuningEngine.evaluate",
        "vetinari.workbench.adaptive_tuning.store",
        "ui/svelte/src/views/WorkbenchAdaptiveTuningView.svelte",
    ),
    surface(
        WorkbenchSurfaceId::ResourceCockpit,
        SurfaceDefaultPolicy::DefaultOn,
        "crates/amw-kernel/src/resource_cockpit/mod.rs::execute_live_action",
        "LiveActionReceipt.rollback_ref",
        "ui/svelte/src/views/WorkbenchResourceCockpitView.svelte",
    ),
    surface(
        WorkbenchSurfaceId::CapabilityPacks,
        SurfaceDefaultPolicy::DefaultOn,
        "vetinari.workbench.capability_packs.CapabilityPackService.enable_pack",
        "CapabilityPackService.disable_pack",
        "ui/svelte/src/views/CapabilityPacksView.svelte",
    ),
    surface(
        WorkbenchSurfaceId::DomainKits,
        SurfaceDefaultPolicy::DefaultOn,
        "vetinari.workbench.domain_kits.DomainKitService.evaluate_request_support",
        "denied verdict with caveat acknowledgement recovery",
        "ui/svelte/src/views/DomainKitsView.svelte",
    ),
    surface(
        WorkbenchSurfaceId::WorkflowBuilder,
        SurfaceDefaultPolicy::DefaultOn,
        "vetinari.workbench.workflow_builder.api_service.WorkflowBuilderService.save_workflow",
        "vetinari.workbench.workflow_builder.persistence",
        "ui/svelte/src/views/WorkbenchWorkflowBuilderView.svelte",
    ),
    surface(
        WorkbenchSurfaceId::Channels,
        SurfaceDefaultPolicy::DefaultOn,
        "vetinari.workbench.channels.delivery.ChannelDeliveryService.deliver",
        "vetinari.workbench.channels.approvals",
        "ui/svelte/src/views/WorkbenchChannelsView.svelte",
    ),
    surface(
        WorkbenchSurfaceId::BenchmarkImporter,
        SurfaceDefaultPolicy::DefaultOn,
        "vetinari.workbench.benchmark_importer.load_benchmark_importer_catalog",
        "catalog validation refusal",
        "ui/svelte/src/views/BenchmarkImporterView.svelte",
    ),
    surface(
        WorkbenchSurfaceId::MigrationWizard,
        SurfaceDefaultPolicy::DefaultOn,
        "vetinari.workbench.migration.runtime.run_migration_plan",
        "vetinari.workbench.migration.runtime.rollback_staged_migration",
        "ui/svelte/src/views/WorkbenchMigrationWizardView.svelte",
    ),
    surface(
        WorkbenchSurfaceId::HabitHealth,
        SurfaceDefaultPolicy::OptIn,
        "vetinari.workbench.habit_health.runtime.HabitHealthRuntime.evaluate",
        "vetinari.workbench.habit_health.store",
        "ui/svelte/src/views/WorkbenchHabitHealthView.svelte",
    ),
    surface(
        WorkbenchSurfaceId::ExtensionsMarketplace,
        SurfaceDefaultPolicy::OptIn,
        "vetinari.workbench.plugin_runtime.registration.PluginRegistrationService.register_extension",
        "vetinari.workbench.plugin_runtime.registration.PluginRegistrationService.evaluate_registration",
        "ui/svelte/src/views/WorkbenchExtensionsView.svelte",
    ),
];

pub fn workbench_surface_policies() -> &'static [WorkbenchSurfacePolicy] {
    WORKBENCH_SURFACE_POLICIES
}

const KNOWN_SERVICE_PREFIXES: &[&str] = &[
    "vetinari.workbench.",
    "crates/amw-kernel/src/",
    "LiveActionReceipt.",
    "CapabilityPackService.",
    "denied verdict",
    "catalog validation refusal",
];

pub fn validate_surface_policy_paths() -> Result<(), String> {
    for policy in WORKBENCH_SURFACE_POLICIES {
        if !KNOWN_SERVICE_PREFIXES
            .iter()
            .any(|prefix| policy.live_action_path.starts_with(prefix))
        {
            return Err(format!(
                "unknown live action service prefix for {}",
                policy.surface_id.as_str()
            ));
        }
    }
    Ok(())
}

const fn surface(
    surface_id: WorkbenchSurfaceId,
    default_policy: SurfaceDefaultPolicy,
    live_action_path: &'static str,
    rollback_path: &'static str,
    downstream_consumer: &'static str,
) -> WorkbenchSurfacePolicy {
    WorkbenchSurfacePolicy {
        surface_id,
        default_policy,
        live_action_path,
        rollback_path,
        downstream_consumer,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn habit_health_remains_opt_in() {
        let habit_health = workbench_surface_policies()
            .iter()
            .find(|surface| surface.surface_id == WorkbenchSurfaceId::HabitHealth)
            .expect("habit health policy exists");
        assert_eq!(habit_health.default_policy, SurfaceDefaultPolicy::OptIn);
    }

    #[test]
    fn migration_wizard_is_default_on() {
        let migration_wizard = workbench_surface_policies()
            .iter()
            .find(|surface| surface.surface_id == WorkbenchSurfaceId::MigrationWizard)
            .expect("migration wizard policy exists");
        assert_eq!(
            migration_wizard.default_policy,
            SurfaceDefaultPolicy::DefaultOn
        );
    }

    #[test]
    fn extensions_marketplace_remains_opt_in() {
        let marketplace = workbench_surface_policies()
            .iter()
            .find(|surface| surface.surface_id == WorkbenchSurfaceId::ExtensionsMarketplace)
            .expect("extensions marketplace policy exists");
        assert_eq!(marketplace.default_policy, SurfaceDefaultPolicy::OptIn);
    }

    #[test]
    fn resource_cockpit_policy_points_to_live_rollback_field() {
        let resource_cockpit = workbench_surface_policies()
            .iter()
            .find(|surface| surface.surface_id == WorkbenchSurfaceId::ResourceCockpit)
            .expect("resource cockpit policy exists");

        assert_eq!(
            resource_cockpit.rollback_path,
            "LiveActionReceipt.rollback_ref"
        );
    }

    #[test]
    fn surface_policy_unknown_text_fails_closed() {
        assert_eq!(
            "default-on".parse::<SurfaceDefaultPolicy>(),
            Ok(SurfaceDefaultPolicy::DefaultOn)
        );
        assert!("default".parse::<SurfaceDefaultPolicy>().is_err());
    }

    #[test]
    fn workbench_surface_typed_enum() {
        assert_eq!(WorkbenchSurfaceId::MethodLibrary.as_str(), "method_library");
        assert!(workbench_surface_policies()
            .iter()
            .any(|surface| surface.surface_id == WorkbenchSurfaceId::AdaptiveTuning));
    }

    #[test]
    fn workbench_surface_policy_paths_validated() {
        validate_surface_policy_paths().expect("static policies use known prefixes");
        let unknown = "unknown.service.path.Action";
        assert!(!KNOWN_SERVICE_PREFIXES
            .iter()
            .any(|prefix| unknown.starts_with(prefix)));
    }
}
