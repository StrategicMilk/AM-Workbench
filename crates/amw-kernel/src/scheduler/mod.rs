use std::collections::{BTreeMap, VecDeque};
use std::str::FromStr;

use crate::error::KernelError;

const RECEIPT_RETENTION_MULTIPLIER: usize = 16;
const MIN_RECEIPT_RETENTION: usize = 16;
const MAX_RECEIPT_RETENTION: usize = 4096;

#[derive(Debug, Clone, Eq, PartialEq)]
pub struct SchedulerConfig {
    pub max_active_leases: usize,
    pub lease_ttl_ticks: u64,
}

#[derive(Debug, Clone, Eq, PartialEq)]
pub struct LeaseRequest {
    pub workload_id: String,
    pub lane: String,
    pub requested_units: u32,
    pub telemetry_present: bool,
}

#[derive(Debug, Clone, Eq, PartialEq)]
pub struct SchedulerLease {
    pub lease_id: String,
    pub workload_id: String,
    pub lane: String,
    pub expires_at_tick: u64,
}

#[derive(Debug, Clone, Eq, PartialEq)]
pub struct SchedulerReceipt {
    pub receipt_id: String,
    pub lease_id: String,
    pub outcome: SchedulerOutcome,
    pub rollback_performed: bool,
}

#[derive(Debug, Clone, Copy, Eq, PartialEq)]
pub enum SchedulerOutcome {
    Cancelled,
    Ok,
    Expired,
}

impl SchedulerOutcome {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Cancelled => "cancelled",
            Self::Ok => "ok",
            Self::Expired => "expired",
        }
    }
}

impl FromStr for SchedulerOutcome {
    type Err = KernelError;

    fn from_str(value: &str) -> Result<Self, Self::Err> {
        match value {
            "cancelled" => Ok(Self::Cancelled),
            "ok" => Ok(Self::Ok),
            "expired" => Ok(Self::Expired),
            other => Err(KernelError::UnknownSignal(format!(
                "scheduler-outcome:{other}"
            ))),
        }
    }
}

#[derive(Debug, Default, Clone, Eq, PartialEq)]
pub struct SchedulerSnapshot {
    pub active_count: usize,
    pub queued_count: usize,
    pub receipt_count: usize,
}

#[derive(Debug, Clone)]
pub struct RustScheduler {
    config: SchedulerConfig,
    active: BTreeMap<String, SchedulerLease>,
    queue: VecDeque<LeaseRequest>,
    receipts: VecDeque<SchedulerReceipt>,
    receipt_retention: usize,
    current_tick: u64,
    next_lease: u64,
    restarted: bool,
}

impl RustScheduler {
    pub fn new(config: SchedulerConfig) -> Result<Self, KernelError> {
        if config.max_active_leases == 0 || config.lease_ttl_ticks == 0 {
            return Err(KernelError::CorruptState("scheduler-config"));
        }
        let receipt_retention = receipt_retention_for_capacity(config.max_active_leases);
        Ok(Self {
            config,
            active: BTreeMap::new(),
            queue: VecDeque::new(),
            receipts: VecDeque::new(),
            receipt_retention,
            current_tick: 0,
            next_lease: 1,
            restarted: false,
        })
    }

    pub fn request_lease(&mut self, request: LeaseRequest) -> Result<SchedulerLease, KernelError> {
        require_lease_request(&request)?;
        self.expire_stale_leases();
        if self.active.len() >= self.config.max_active_leases {
            // The public API returns either an immediate lease or an error. Do
            // not enqueue work here: a later silent promotion would create an
            // active lease whose id was never returned to the caller.
            return Err(KernelError::UnavailableSignal("scheduler-capacity"));
        }
        let lease = self.grant_lease(request);
        Ok(lease)
    }

    pub fn cancel(&mut self, lease_id: &str) -> Result<SchedulerReceipt, KernelError> {
        let lease = self
            .active
            .remove(lease_id)
            .ok_or_else(|| KernelError::MissingWorkerOutput(lease_id.to_string()))?;
        let receipt = SchedulerReceipt {
            receipt_id: format!("receipt-cancel-{}", lease.lease_id),
            lease_id: lease.lease_id,
            outcome: SchedulerOutcome::Cancelled,
            rollback_performed: true,
        };
        self.push_receipt(receipt.clone());
        Ok(receipt)
    }

    pub fn complete(
        &mut self,
        lease_id: &str,
        output_present: bool,
    ) -> Result<SchedulerReceipt, KernelError> {
        if !output_present {
            return Err(KernelError::MissingWorkerOutput(lease_id.to_string()));
        }
        let lease = self
            .active
            .remove(lease_id)
            .ok_or_else(|| KernelError::MissingWorkerOutput(lease_id.to_string()))?;
        let receipt = SchedulerReceipt {
            receipt_id: format!("receipt-ok-{}", lease.lease_id),
            lease_id: lease.lease_id,
            outcome: SchedulerOutcome::Ok,
            rollback_performed: false,
        };
        self.push_receipt(receipt.clone());
        Ok(receipt)
    }

    pub fn advance_ticks(&mut self, ticks: u64) {
        self.current_tick = self.current_tick.saturating_add(ticks);
        self.expire_stale_leases();
    }

    pub fn restart_from_receipts(
        receipts: Vec<SchedulerReceipt>,
        config: SchedulerConfig,
    ) -> Result<Self, KernelError> {
        let mut scheduler = Self::new(config)?;
        scheduler.restarted = true;
        for receipt in receipts {
            receipt.outcome.as_str().parse::<SchedulerOutcome>()?;
            scheduler.push_receipt(receipt);
        }
        Ok(scheduler)
    }

    pub fn snapshot(&self) -> SchedulerSnapshot {
        SchedulerSnapshot {
            active_count: self.active.len(),
            queued_count: self.queue.len(),
            receipt_count: self.receipts.len(),
        }
    }

    pub fn restarted(&self) -> bool {
        self.restarted
    }

    fn expire_stale_leases(&mut self) {
        let expired: Vec<String> = self
            .active
            .iter()
            .filter(|(_, lease)| lease.expires_at_tick <= self.current_tick)
            .map(|(id, _)| id.clone())
            .collect();
        for lease_id in expired {
            self.active.remove(&lease_id);
            self.push_receipt(SchedulerReceipt {
                receipt_id: format!("receipt-expired-{lease_id}"),
                lease_id,
                outcome: SchedulerOutcome::Expired,
                rollback_performed: true,
            });
        }
    }

    fn push_receipt(&mut self, receipt: SchedulerReceipt) {
        self.receipts.push_back(receipt);
        while self.receipts.len() > self.receipt_retention {
            self.receipts.pop_front();
        }
    }

    fn grant_lease(&mut self, request: LeaseRequest) -> SchedulerLease {
        let lease = SchedulerLease {
            lease_id: format!("lease-{}", self.next_lease),
            workload_id: request.workload_id,
            lane: request.lane,
            expires_at_tick: self.current_tick + self.config.lease_ttl_ticks,
        };
        self.next_lease += 1;
        self.active.insert(lease.lease_id.clone(), lease.clone());
        lease
    }
}

fn receipt_retention_for_capacity(max_active_leases: usize) -> usize {
    max_active_leases
        .saturating_mul(RECEIPT_RETENTION_MULTIPLIER)
        .max(MIN_RECEIPT_RETENTION)
        .min(MAX_RECEIPT_RETENTION)
}

pub fn require_lease_request(request: &LeaseRequest) -> Result<(), KernelError> {
    if request.workload_id.trim().is_empty() {
        return Err(KernelError::MissingSignal("workload-id"));
    }
    if request.lane.trim().is_empty() {
        return Err(KernelError::MissingSignal("lane"));
    }
    if request.requested_units == 0 {
        return Err(KernelError::UnknownSignal("requested-units".to_string()));
    }
    if !request.telemetry_present {
        return Err(KernelError::MissingSignal("scheduler-telemetry"));
    }
    Ok(())
}

#[cfg(test)]
mod scheduler_tests {
    use super::*;

    fn request(id: &str) -> LeaseRequest {
        LeaseRequest {
            workload_id: id.to_string(),
            lane: "interactive".to_string(),
            requested_units: 1,
            telemetry_present: true,
        }
    }

    #[test]
    fn scheduler_rejects_capacity_without_orphaning_a_queued_lease() {
        let mut scheduler = RustScheduler::new(SchedulerConfig {
            max_active_leases: 1,
            lease_ttl_ticks: 10,
        })
        .expect("valid scheduler");
        let lease = scheduler
            .request_lease(request("workload-a"))
            .expect("first lease");
        assert!(matches!(
            scheduler.request_lease(request("workload-b")),
            Err(KernelError::UnavailableSignal("scheduler-capacity"))
        ));
        assert_eq!(scheduler.snapshot().queued_count, 0);

        let receipt = scheduler
            .cancel(&lease.lease_id)
            .expect("cancel emits receipt");

        assert_eq!(receipt.outcome, SchedulerOutcome::Cancelled);
        assert!(receipt.rollback_performed);
        assert_eq!(scheduler.snapshot().active_count, 0);
    }

    #[test]
    fn scheduler_capacity_rejection_does_not_later_promote_unreturned_leases() {
        let mut scheduler = RustScheduler::new(SchedulerConfig {
            max_active_leases: 1,
            lease_ttl_ticks: 10,
        })
        .expect("valid scheduler");
        let active = scheduler
            .request_lease(request("workload-a"))
            .expect("first lease");
        assert!(scheduler.request_lease(request("workload-b")).is_err());
        assert!(scheduler.request_lease(request("workload-c")).is_err());

        scheduler
            .cancel(&active.lease_id)
            .expect("cancel releases capacity");

        assert_eq!(scheduler.snapshot().active_count, 0);
        assert_eq!(scheduler.snapshot().queued_count, 0);
    }

    #[test]
    fn scheduler_expires_stale_lease_and_recovers_after_restart() {
        let mut scheduler = RustScheduler::new(SchedulerConfig {
            max_active_leases: 1,
            lease_ttl_ticks: 2,
        })
        .expect("valid scheduler");
        let lease = scheduler
            .request_lease(request("workload-a"))
            .expect("lease");

        scheduler.advance_ticks(3);

        assert_eq!(scheduler.snapshot().active_count, 0);
        assert_eq!(scheduler.snapshot().receipt_count, 1);
        let restarted = RustScheduler::restart_from_receipts(
            vec![SchedulerReceipt {
                receipt_id: "receipt-expired".to_string(),
                lease_id: lease.lease_id,
                outcome: SchedulerOutcome::Expired,
                rollback_performed: true,
            }],
            SchedulerConfig {
                max_active_leases: 1,
                lease_ttl_ticks: 2,
            },
        )
        .expect("restart");
        assert!(restarted.restarted());
        assert_eq!(restarted.snapshot().receipt_count, 1);
    }

    #[test]
    fn scheduler_rejects_missing_telemetry_and_zero_capacity() {
        assert!(matches!(
            RustScheduler::new(SchedulerConfig {
                max_active_leases: 0,
                lease_ttl_ticks: 1,
            }),
            Err(KernelError::CorruptState("scheduler-config"))
        ));
        let mut scheduler = RustScheduler::new(SchedulerConfig {
            max_active_leases: 1,
            lease_ttl_ticks: 1,
        })
        .expect("valid scheduler");
        let mut bad = request("workload-a");
        bad.telemetry_present = false;
        assert!(matches!(
            scheduler.request_lease(bad),
            Err(KernelError::MissingSignal("scheduler-telemetry"))
        ));
    }

    #[test]
    fn scheduler_kernel_contracts_type_and_bound_receipts() {
        assert_eq!(
            "cancelled".parse::<SchedulerOutcome>(),
            Ok(SchedulerOutcome::Cancelled)
        );
        assert!("unknown".parse::<SchedulerOutcome>().is_err());

        let mut scheduler = RustScheduler::new(SchedulerConfig {
            max_active_leases: 1,
            lease_ttl_ticks: 1,
        })
        .expect("valid scheduler");

        for index in 0..40 {
            let lease = scheduler
                .request_lease(request(&format!("workload-{index}")))
                .expect("lease");
            scheduler
                .complete(&lease.lease_id, true)
                .expect("completion receipt");
        }

        assert_eq!(scheduler.snapshot().receipt_count, 16);
    }

    #[test]
    fn scheduler_receipt_retention_has_absolute_ceiling() {
        let scheduler = RustScheduler::new(SchedulerConfig {
            max_active_leases: usize::MAX,
            lease_ttl_ticks: 1,
        })
        .expect("valid scheduler");

        assert_eq!(scheduler.receipt_retention, MAX_RECEIPT_RETENTION);
    }
}
