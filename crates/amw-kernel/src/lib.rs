pub mod command {
    #[derive(Debug, Clone, Eq, PartialEq)]
    pub enum KernelCommand {
        StartWorker { id: String },
        StopWorker { id: String },
        PersistCheckpoint { checkpoint_id: String },
        RollbackToCheckpoint { checkpoint_id: String },
    }
}

pub mod api;
pub mod engine_runtime;
pub mod extensions;
pub mod files;
pub mod ipc;
pub mod mcp;
pub mod resource_cockpit;
pub mod resources;
pub mod run_kernel;
pub mod sandbox;
pub mod scheduler;
pub mod spine;
pub mod storage;
pub mod workbench_surfaces;

pub mod error {
    #[derive(Debug, Clone, Eq, PartialEq)]
    pub enum KernelError {
        MissingSignal(&'static str),
        UnknownSignal(String),
        UnavailableSignal(&'static str),
        CorruptState(&'static str),
        InvalidTransition {
            from: &'static str,
            to: &'static str,
        },
        ResourceBudgetExceeded {
            requested: u32,
            available: u32,
        },
        LeaseAlreadyAcquired(String),
        MissingWorkerOutput(String),
        DuplicateReceipt(String),
        UpstreamUnavailable(String),
    }
}

pub mod lifecycle {
    use crate::error::KernelError;

    #[derive(Debug, Clone, Copy, Eq, PartialEq)]
    pub enum KernelState {
        Initialized,
        Ready,
        Running,
        Paused,
        Recovering,
        FailedClosed,
        Stopped,
    }

    #[derive(Debug, Clone, Copy, Eq, PartialEq)]
    pub struct LifecycleTransition {
        pub from: KernelState,
        pub to: KernelState,
    }

    impl KernelState {
        pub const fn as_str(self) -> &'static str {
            match self {
                Self::Initialized => "initialized",
                Self::Ready => "ready",
                Self::Running => "running",
                Self::Paused => "paused",
                Self::Recovering => "recovering",
                Self::FailedClosed => "failed_closed",
                Self::Stopped => "stopped",
            }
        }

        pub fn transition_to(self, to: KernelState) -> Result<LifecycleTransition, KernelError> {
            let allowed = matches!(
                (self, to),
                (Self::Initialized, Self::Ready)
                    | (Self::Ready, Self::Running)
                    | (Self::Running, Self::Paused)
                    | (Self::Paused, Self::Running)
                    | (Self::Running, Self::Recovering)
                    | (Self::Recovering, Self::Ready)
                    | (_, Self::FailedClosed)
                    | (Self::Ready, Self::Stopped)
                    | (Self::Paused, Self::Stopped)
                    | (Self::FailedClosed, Self::Stopped)
            );
            if allowed {
                Ok(LifecycleTransition { from: self, to })
            } else {
                Err(KernelError::InvalidTransition {
                    from: self.as_str(),
                    to: to.as_str(),
                })
            }
        }
    }
}

pub mod resource {
    use crate::error::KernelError;

    #[derive(Debug, Clone, Copy, Eq, PartialEq)]
    pub struct ResourceBudget {
        available_units: u32,
    }

    impl ResourceBudget {
        pub const fn new(available_units: u32) -> Self {
            Self { available_units }
        }

        pub const fn available_units(&self) -> u32 {
            self.available_units
        }

        pub fn reserve(&self, requested: u32) -> Result<Self, KernelError> {
            if requested <= self.available_units {
                Ok(Self::new(self.available_units - requested))
            } else {
                Err(KernelError::ResourceBudgetExceeded {
                    requested,
                    available: self.available_units,
                })
            }
        }
    }

    #[derive(Debug, Clone, Eq, PartialEq)]
    pub struct WorkerLease {
        worker_id: String,
        acquired: bool,
    }

    impl WorkerLease {
        pub fn acquire(
            worker_id: impl Into<String>,
            existing: Option<&Self>,
        ) -> Result<Self, KernelError> {
            let worker_id = worker_id.into();
            if existing.is_some_and(|lease| lease.acquired && lease.worker_id == worker_id) {
                return Err(KernelError::LeaseAlreadyAcquired(worker_id));
            }
            Ok(Self {
                worker_id,
                acquired: true,
            })
        }

        pub fn release(mut self) -> Result<String, KernelError> {
            if !self.acquired {
                return Err(KernelError::MissingWorkerOutput(self.worker_id));
            }
            self.acquired = false;
            Ok(self.worker_id)
        }

        pub fn complete_with_output(mut self, output_present: bool) -> Result<String, KernelError> {
            if !self.acquired || !output_present {
                return Err(KernelError::MissingWorkerOutput(self.worker_id));
            }
            self.acquired = false;
            Ok(self.worker_id)
        }
    }
}

pub mod receipt {
    #[derive(Debug, Clone, Eq, PartialEq)]
    pub struct KernelReceipt {
        pub id: String,
        pub command_kind: &'static str,
        pub checkpoint_id: Option<String>,
    }
}

pub mod event {
    #[derive(Debug, Clone, Eq, PartialEq)]
    pub enum KernelEvent {
        WorkerStarted(String),
        WorkerStopped(String),
        CheckpointPersisted(String),
        RollbackRequested(String),
        FailedClosed(String),
    }
}

pub mod supervisor {
    use crate::error::KernelError;

    #[derive(Debug, Clone, Eq, PartialEq)]
    pub enum SignalStatus<T> {
        Available(T),
        Missing,
        Unknown(String),
        Unavailable,
        Corrupt,
    }

    pub fn require_signal<T>(
        name: &'static str,
        signal: SignalStatus<T>,
    ) -> Result<T, KernelError> {
        match signal {
            SignalStatus::Available(value) => Ok(value),
            SignalStatus::Missing => Err(KernelError::MissingSignal(name)),
            SignalStatus::Unknown(value) => Err(KernelError::UnknownSignal(value)),
            SignalStatus::Unavailable => Err(KernelError::UnavailableSignal(name)),
            SignalStatus::Corrupt => Err(KernelError::CorruptState(name)),
        }
    }
}

pub use api::{
    api_domain_authorities, require_domain_authority,
    routes::{build_router, handle_kernel_request, KernelHttpRequest},
    ApiDomainAuthority, NativeRouteAuthority,
};
pub use command::KernelCommand;
pub use error::KernelError;
pub use event::KernelEvent;
pub use extensions::{
    admit_extension_manifest, local_development_signature, rollback_extension_admission,
    ExtensionAdmission, ExtensionManifest, ExtensionPermission, ExtensionReceipt, SupportEnvelope,
};
pub use lifecycle::{KernelState, LifecycleTransition};
pub use mcp::{
    list_mcp_resources, read_mcp_resource, McpResource, McpResourceRegistry, McpStreamSession,
};
pub use receipt::KernelReceipt;
pub use resource::{ResourceBudget, WorkerLease};
pub use resource_cockpit::{execute_live_action, LiveActionReceipt, LiveActionSignal};
pub use resources::{
    evaluate_resource_request, ResourceDecision, ResourceSignal, ResourceSnapshot,
};
pub use run_kernel::{admit_run, record_sandbox_outcome, RunKernelAdmission};
pub use scheduler::{
    require_lease_request, LeaseRequest, RustScheduler, SchedulerConfig, SchedulerLease,
    SchedulerReceipt, SchedulerSnapshot,
};
pub use supervisor::{require_signal, SignalStatus};
pub use workbench_surfaces::{workbench_surface_policies, WorkbenchSurfacePolicy};

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn lifecycle_rejects_invalid_transition() {
        let err = KernelState::Initialized
            .transition_to(KernelState::Running)
            .expect_err("initialized cannot jump directly to running");
        assert_eq!(
            err,
            KernelError::InvalidTransition {
                from: "initialized",
                to: "running"
            }
        );
    }

    #[test]
    fn lifecycle_allows_recovery_path() {
        let transition = KernelState::Running
            .transition_to(KernelState::Recovering)
            .expect("running can move to recovery");
        assert_eq!(transition.to, KernelState::Recovering);
    }

    #[test]
    fn missing_unknown_unavailable_and_corrupt_signals_fail_closed() {
        assert!(matches!(
            require_signal::<()>("authorization", SignalStatus::Missing),
            Err(KernelError::MissingSignal("authorization"))
        ));
        assert!(matches!(
            require_signal::<()>("confidence", SignalStatus::Unknown("mystery".to_string())),
            Err(KernelError::UnknownSignal(value)) if value == "mystery"
        ));
        assert!(matches!(
            require_signal::<()>("provenance", SignalStatus::Unavailable),
            Err(KernelError::UnavailableSignal("provenance"))
        ));
        assert!(matches!(
            require_signal::<()>("checkpoint", SignalStatus::Corrupt),
            Err(KernelError::CorruptState("checkpoint"))
        ));
    }

    #[test]
    fn resource_budget_and_worker_lease_fail_closed() {
        let budget = ResourceBudget::new(3);
        assert!(matches!(
            budget.reserve(4),
            Err(KernelError::ResourceBudgetExceeded {
                requested: 4,
                available: 3
            })
        ));

        let lease = WorkerLease::acquire("worker-a", None).expect("first lease succeeds");
        assert!(matches!(
            WorkerLease::acquire("worker-a", Some(&lease)),
            Err(KernelError::LeaseAlreadyAcquired(worker)) if worker == "worker-a"
        ));

        assert!(matches!(
            lease.complete_with_output(false),
            Err(KernelError::MissingWorkerOutput(worker)) if worker == "worker-a"
        ));
    }
}
