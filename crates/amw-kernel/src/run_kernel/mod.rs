use crate::error::KernelError;
use crate::scheduler::{LeaseRequest, RustScheduler, SchedulerLease};

#[derive(Debug, Clone, Eq, PartialEq)]
pub struct RunKernelAdmission {
    pub lease: SchedulerLease,
    pub sandbox_execution_allowed: bool,
}

pub fn admit_run(
    scheduler: &mut RustScheduler,
    request: LeaseRequest,
) -> Result<RunKernelAdmission, KernelError> {
    require_sandbox_execution_policy(&request)?;
    let sandbox_execution_allowed = sandbox_execution_allowed_for_lane(&request.lane)?;
    let lease = scheduler.request_lease(request)?;
    Ok(RunKernelAdmission {
        lease,
        sandbox_execution_allowed,
    })
}

pub fn require_sandbox_execution_policy(request: &LeaseRequest) -> Result<(), KernelError> {
    if !request.telemetry_present {
        return Err(KernelError::MissingSignal("sandbox-policy-telemetry"));
    }
    if matches!(
        request.lane.as_str(),
        "sandbox_denied" | "sandbox-denied" | "blocked" | "deny"
    ) {
        return Err(KernelError::UnavailableSignal("sandbox-policy-denied"));
    }
    sandbox_execution_allowed_for_lane(&request.lane)?;
    Ok(())
}

fn sandbox_execution_allowed_for_lane(lane: &str) -> Result<bool, KernelError> {
    match lane {
        "interactive" | "background" => Ok(true),
        _ => Err(KernelError::UnavailableSignal(
            "sandbox-policy-workload-type",
        )),
    }
}

pub fn record_sandbox_outcome(
    scheduler: &mut RustScheduler,
    lease_id: &str,
    output_present: bool,
) -> Result<String, KernelError> {
    Ok(scheduler.complete(lease_id, output_present)?.receipt_id)
}

#[cfg(test)]
mod run_kernel_scheduler_tests {
    use super::*;
    use crate::scheduler::SchedulerConfig;

    #[test]
    fn run_kernel_requires_scheduler_lease_before_execution() {
        let mut scheduler = RustScheduler::new(SchedulerConfig {
            max_active_leases: 1,
            lease_ttl_ticks: 10,
        })
        .expect("scheduler");

        let admission = admit_run(
            &mut scheduler,
            LeaseRequest {
                workload_id: "sandbox-work".to_string(),
                lane: "interactive".to_string(),
                requested_units: 1,
                telemetry_present: true,
            },
        )
        .expect("admission");

        assert!(admission.sandbox_execution_allowed);
        let receipt = record_sandbox_outcome(&mut scheduler, &admission.lease.lease_id, true)
            .expect("sandbox receipt");
        assert!(receipt.starts_with("receipt-ok-"));
    }

    #[test]
    fn security_run_kernel_denies_absent_policy_telemetry_before_lease() {
        let mut scheduler = RustScheduler::new(SchedulerConfig {
            max_active_leases: 1,
            lease_ttl_ticks: 10,
        })
        .expect("scheduler");

        let denied = admit_run(
            &mut scheduler,
            LeaseRequest {
                workload_id: "sandbox-work".to_string(),
                lane: "interactive".to_string(),
                requested_units: 1,
                telemetry_present: false,
            },
        )
        .expect_err("missing sandbox policy telemetry denies admission");

        assert_eq!(
            denied,
            KernelError::MissingSignal("sandbox-policy-telemetry")
        );
        assert_eq!(scheduler.snapshot().active_count, 0);
    }

    #[test]
    fn security_run_kernel_denies_explicit_sandbox_policy_before_lease() {
        let mut scheduler = RustScheduler::new(SchedulerConfig {
            max_active_leases: 1,
            lease_ttl_ticks: 10,
        })
        .expect("scheduler");

        let denied = admit_run(
            &mut scheduler,
            LeaseRequest {
                workload_id: "sandbox-work".to_string(),
                lane: "sandbox-denied".to_string(),
                requested_units: 1,
                telemetry_present: true,
            },
        )
        .expect_err("explicit sandbox policy denial denies admission");

        assert_eq!(
            denied,
            KernelError::UnavailableSignal("sandbox-policy-denied")
        );
        assert_eq!(scheduler.snapshot().active_count, 0);
    }

    #[test]
    fn run_kernel_allows_declared_workload_types_only() {
        let mut scheduler = RustScheduler::new(SchedulerConfig {
            max_active_leases: 2,
            lease_ttl_ticks: 10,
        })
        .expect("scheduler");

        for lane in ["interactive", "background"] {
            let admission = admit_run(
                &mut scheduler,
                LeaseRequest {
                    workload_id: format!("{lane}-work"),
                    lane: lane.to_string(),
                    requested_units: 1,
                    telemetry_present: true,
                },
            )
            .expect("declared workload type admitted");
            assert!(admission.sandbox_execution_allowed);
        }

        let denied = admit_run(
            &mut scheduler,
            LeaseRequest {
                workload_id: "unknown-work".to_string(),
                lane: "unknown_custom_lane".to_string(),
                requested_units: 1,
                telemetry_present: true,
            },
        )
        .expect_err("unknown workload type denied");
        assert_eq!(
            denied,
            KernelError::UnavailableSignal("sandbox-policy-workload-type")
        );
    }
}
