//! Bounded admission queue with one policy shared by admission and batching.

use std::collections::{BTreeMap, VecDeque};

use serde::{Deserialize, Serialize};

use crate::hw::budget::{MemoryAmount, MemoryLedger, MemoryPurpose, ReservationId};

use super::{EventSink, SchedError, SchedEvent};

/// Number of injected scheduling steps per one-class priority promotion.
pub const AGING_STEP_QUANTUM: u64 = 64;
/// Fixed-point denominator for role shares.
pub const POLICY_SHARE_DENOMINATOR: u32 = 100;
/// Interactive share of each non-Eval batch.
pub const INTERACTIVE_BATCH_SHARE: u32 = 50;
/// Worker share of each non-Eval batch.
pub const WORKER_BATCH_SHARE: u32 = 35;
/// Background share of each non-Eval batch.
pub const BACKGROUND_BATCH_SHARE: u32 = 15;
/// Maximum share of queued and contended active slots held by one principal.
pub const PRINCIPAL_FAIR_SHARE_PERCENT: usize = 50;

#[derive(Clone, Copy, Debug, Deserialize, Eq, Ord, PartialEq, PartialOrd, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum PriorityClass {
    InteractiveBlocking,
    Interactive,
    Worker,
    Eval,
    Background,
}

impl PriorityClass {
    pub(crate) const fn rank(self) -> u8 {
        match self {
            Self::InteractiveBlocking => 0,
            Self::Interactive => 1,
            Self::Worker => 2,
            Self::Eval => 3,
            Self::Background => 4,
        }
    }

    pub(crate) const fn policy_group(self) -> PolicyGroup {
        match self {
            Self::InteractiveBlocking | Self::Interactive => PolicyGroup::Interactive,
            Self::Worker => PolicyGroup::Worker,
            Self::Eval => PolicyGroup::Eval,
            Self::Background => PolicyGroup::Background,
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, Ord, PartialEq, PartialOrd)]
pub(crate) enum PolicyGroup {
    Interactive,
    Worker,
    Eval,
    Background,
}

/// Pinned admission and per-step batching policy.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct PriorityPolicy {
    slot_count: usize,
    batch_token_budget: u32,
}

impl PriorityPolicy {
    pub fn new(slot_count: usize, batch_token_budget: u32) -> Result<Self, SchedError> {
        if slot_count == 0 || batch_token_budget == 0 {
            return Err(SchedError::InvalidRequest(
                "slot count and batch token budget must be positive",
            ));
        }
        Ok(Self {
            slot_count,
            batch_token_budget,
        })
    }

    pub const fn slot_count(self) -> usize {
        self.slot_count
    }

    pub const fn batch_token_budget(self) -> u32 {
        self.batch_token_budget
    }

    /// Interactive can use all slots, Worker 75%, Background 25%, Eval one.
    pub fn active_quota(self, priority: PriorityClass) -> usize {
        match priority.policy_group() {
            PolicyGroup::Interactive => self.slot_count,
            PolicyGroup::Worker => ceil_share(self.slot_count, 75),
            PolicyGroup::Background => ceil_share(self.slot_count, 25),
            PolicyGroup::Eval => 1,
        }
    }

    /// Returns the active-slot fair share used while multiple principals contend.
    pub fn principal_active_quota(self) -> usize {
        ceil_share(self.slot_count, PRINCIPAL_FAIR_SHARE_PERCENT)
    }

    pub(crate) const fn initial_batch_share(self, group: PolicyGroup) -> u32 {
        match group {
            PolicyGroup::Interactive => INTERACTIVE_BATCH_SHARE,
            PolicyGroup::Worker => WORKER_BATCH_SHARE,
            PolicyGroup::Background => BACKGROUND_BATCH_SHARE,
            PolicyGroup::Eval => POLICY_SHARE_DENOMINATOR,
        }
    }
}

fn ceil_share(total: usize, percentage: usize) -> usize {
    total.saturating_mul(percentage).div_ceil(100).max(1)
}

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub struct ActiveClassCounts {
    interactive: usize,
    worker: usize,
    eval: usize,
    background: usize,
}

/// Active sequence counts keyed by authenticated scheduler principal.
#[derive(Clone, Debug, Default, Eq, PartialEq)]
pub struct ActivePrincipalCounts {
    counts: BTreeMap<String, usize>,
}

impl ActivePrincipalCounts {
    /// Returns the active sequence count for one principal.
    pub fn count(&self, principal_id: &str) -> usize {
        self.counts.get(principal_id).copied().unwrap_or_default()
    }

    /// Records one active sequence for a principal.
    pub fn add(&mut self, principal_id: &str) {
        *self.counts.entry(principal_id.to_owned()).or_default() += 1;
    }
}

impl ActiveClassCounts {
    pub fn total(self) -> usize {
        self.interactive
            .saturating_add(self.worker)
            .saturating_add(self.eval)
            .saturating_add(self.background)
    }

    pub fn count(self, priority: PriorityClass) -> usize {
        match priority.policy_group() {
            PolicyGroup::Interactive => self.interactive,
            PolicyGroup::Worker => self.worker,
            PolicyGroup::Eval => self.eval,
            PolicyGroup::Background => self.background,
        }
    }

    pub fn can_admit(self, policy: PriorityPolicy, priority: PriorityClass) -> bool {
        let total = self.total();
        if priority == PriorityClass::Eval {
            return total == 0;
        }
        if self.eval > 0 {
            return false;
        }
        total < policy.slot_count() && self.count(priority) < policy.active_quota(priority)
    }

    pub(crate) fn add(&mut self, priority: PriorityClass) {
        match priority.policy_group() {
            PolicyGroup::Interactive => self.interactive += 1,
            PolicyGroup::Worker => self.worker += 1,
            PolicyGroup::Eval => self.eval += 1,
            PolicyGroup::Background => self.background += 1,
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct AdmissionRequest {
    pub request_id: u64,
    pub principal_id: String,
    pub priority: PriorityClass,
    pub prompt_tokens: u32,
    pub max_tokens: u32,
    /// Native sampled-token feed steps; fresh prompts use `max_tokens - 1`.
    pub decode_steps: u32,
    pub context_limit: u32,
    pub kv_bytes: u64,
}

#[derive(Debug)]
struct Queued {
    request: AdmissionRequest,
    enqueued_step: u64,
    order: u64,
}

#[derive(Debug, Eq, PartialEq)]
pub struct AdmissionReceipt {
    pub request: AdmissionRequest,
    reservation: ReservationId,
    enqueued_step: u64,
    order: u64,
    queue_steps: u64,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct AdmissionReleaseReceipt {
    pub request_id: u64,
    pub released_kv_bytes: u64,
}

impl AdmissionReceipt {
    pub fn emit_admission(&self, sink: &mut impl EventSink) {
        sink.emit(SchedEvent::Admission {
            request_id: self.request.request_id,
            queue_ms: self.queue_steps,
            priority_class: self.request.priority,
        });
    }

    pub fn release(
        &self,
        ledger: &mut MemoryLedger,
    ) -> Result<AdmissionReleaseReceipt, SchedError> {
        ledger
            .release(self.reservation)
            .map_err(|_| SchedError::LedgerInvariant("admission reservation was not live"))?;
        Ok(AdmissionReleaseReceipt {
            request_id: self.request.request_id,
            released_kv_bytes: self.request.kv_bytes,
        })
    }

    pub(crate) fn is_committed_in(&self, ledger: &MemoryLedger) -> bool {
        ledger.is_committed(self.reservation)
    }
}

#[derive(Debug)]
pub struct AdmissionQueue {
    capacity: usize,
    per_principal_capacity: usize,
    entries: VecDeque<Queued>,
    next_order: u64,
    last_admitted_principal: Option<String>,
}

impl AdmissionQueue {
    pub fn new(capacity: usize) -> Result<Self, SchedError> {
        if capacity == 0 {
            return Err(SchedError::InvalidRequest(
                "queue capacity must be positive",
            ));
        }
        Ok(Self {
            capacity,
            per_principal_capacity: ceil_share(capacity, PRINCIPAL_FAIR_SHARE_PERCENT),
            entries: VecDeque::new(),
            next_order: 0,
            last_admitted_principal: None,
        })
    }

    pub fn len(&self) -> usize {
        self.entries.len()
    }

    pub fn is_empty(&self) -> bool {
        self.entries.is_empty()
    }

    pub fn remove_request(&mut self, request_id: u64) -> Result<AdmissionRequest, SchedError> {
        let index = self
            .entries
            .iter()
            .position(|queued| queued.request.request_id == request_id)
            .ok_or(SchedError::InvalidRequest("queued request id is unknown"))?;
        self.entries
            .remove(index)
            .map(|queued| queued.request)
            .ok_or(SchedError::InvalidRequest("queued request id is unknown"))
    }

    pub fn enqueue(&mut self, request: AdmissionRequest, now_step: u64) -> Result<(), SchedError> {
        let requested = request
            .prompt_tokens
            .checked_add(request.max_tokens)
            .ok_or(SchedError::ContextOverflow {
                requested: u32::MAX,
                limit: request.context_limit,
            })?;
        if request.decode_steps > request.max_tokens {
            return Err(SchedError::InvalidRequest(
                "decode steps exceed requested output tokens",
            ));
        }
        if requested == 0 {
            return Err(SchedError::InvalidRequest(
                "admission must contain prompt or decode work",
            ));
        }
        if requested > request.context_limit {
            return Err(SchedError::ContextOverflow {
                requested,
                limit: request.context_limit,
            });
        }
        if self.entries.len() >= self.capacity {
            return Err(SchedError::QueueFull);
        }
        if request.principal_id.is_empty() {
            return Err(SchedError::InvalidRequest(
                "scheduler principal id must not be empty",
            ));
        }
        if self
            .entries
            .iter()
            .any(|queued| queued.request.request_id == request.request_id)
        {
            return Err(SchedError::InvalidRequest("duplicate request id"));
        }
        if self
            .entries
            .iter()
            .filter(|queued| queued.request.principal_id == request.principal_id)
            .count()
            >= self.per_principal_capacity
        {
            return Err(SchedError::QuotaFull {
                priority: request.priority,
            });
        }
        let order = self.next_order;
        self.next_order = self.next_order.saturating_add(1);
        self.entries.push_back(Queued {
            request,
            enqueued_step: now_step,
            order,
        });
        Ok(())
    }

    /// Admits one request without removing it until the ledger commit succeeds.
    pub fn admit(
        &mut self,
        ledger: &mut MemoryLedger,
        policy: PriorityPolicy,
        active: ActiveClassCounts,
        active_principals: &ActivePrincipalCounts,
        now_step: u64,
        _sink: &mut impl EventSink,
    ) -> Result<Option<AdmissionReceipt>, SchedError> {
        let Some(index) = self.select_index(policy, active, active_principals, now_step) else {
            return Ok(None);
        };
        let queued = &self.entries[index];
        let requested_bytes = queued.request.kv_bytes;
        let reservation = ledger
            .reserve(MemoryPurpose::KvCache, MemoryAmount::ram(requested_bytes))
            .map_err(|_| SchedError::Oom { requested_bytes })?;
        if ledger.commit(reservation).is_err() {
            let _ = ledger.release(reservation);
            return Err(SchedError::Oom { requested_bytes });
        }
        let queued = self
            .entries
            .remove(index)
            .expect("selected queue entry remains present during ledger commit");
        self.last_admitted_principal = Some(queued.request.principal_id.clone());
        Ok(Some(AdmissionReceipt {
            request: queued.request,
            reservation,
            enqueued_step: queued.enqueued_step,
            order: queued.order,
            queue_steps: now_step.saturating_sub(queued.enqueued_step),
        }))
    }

    /// Restores a failed materialization to its original admission age/order.
    pub fn rollback_admission(
        &mut self,
        receipt: AdmissionReceipt,
        ledger: &mut MemoryLedger,
    ) -> Result<AdmissionReleaseReceipt, SchedError> {
        let released = receipt.release(ledger)?;
        self.entries.push_back(Queued {
            request: receipt.request,
            enqueued_step: receipt.enqueued_step,
            order: receipt.order,
        });
        Ok(released)
    }

    fn select_index(
        &self,
        policy: PriorityPolicy,
        active: ActiveClassCounts,
        active_principals: &ActivePrincipalCounts,
        now_step: u64,
    ) -> Option<usize> {
        self.entries
            .iter()
            .enumerate()
            .filter(|(_, queued)| active.can_admit(policy, queued.request.priority))
            .filter(|(_, queued)| {
                let contended = self
                    .entries
                    .iter()
                    .any(|candidate| candidate.request.principal_id != queued.request.principal_id);
                !contended
                    || active_principals.count(&queued.request.principal_id)
                        < policy.principal_active_quota()
            })
            .min_by_key(|(_, queued)| {
                let waited = now_step.saturating_sub(queued.enqueued_step);
                let promotions = u8::try_from(waited / AGING_STEP_QUANTUM).unwrap_or(u8::MAX);
                let effective_rank = queued.request.priority.rank().saturating_sub(promotions);
                let active_for_principal = active_principals.count(&queued.request.principal_id);
                let admitted_last = self.last_admitted_principal.as_deref()
                    == Some(queued.request.principal_id.as_str());
                (
                    effective_rank,
                    active_for_principal,
                    admitted_last,
                    queued.enqueued_step,
                    queued.order,
                )
            })
            .map(|(index, _)| index)
    }

    pub(crate) fn next_admissible_request_id(
        &self,
        policy: PriorityPolicy,
        active: ActiveClassCounts,
        active_principals: &ActivePrincipalCounts,
        now_step: u64,
    ) -> Option<u64> {
        self.select_index(policy, active, active_principals, now_step)
            .map(|index| self.entries[index].request.request_id)
    }

    pub(crate) fn contains_priority(&self, priority: PriorityClass) -> bool {
        self.entries
            .iter()
            .any(|queued| queued.request.priority == priority)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn request(id: u64, priority: PriorityClass) -> AdmissionRequest {
        AdmissionRequest {
            request_id: id,
            principal_id: "principal-a".to_owned(),
            priority,
            prompt_tokens: 10,
            max_tokens: 10,
            decode_steps: 9,
            context_limit: 20,
            kv_bytes: 10,
        }
    }

    fn policy() -> PriorityPolicy {
        PriorityPolicy::new(4, 100).unwrap()
    }

    #[test]
    fn sched_queue_strict_priority_before_aging() {
        let mut queue = AdmissionQueue::new(4).unwrap();
        queue
            .enqueue(request(1, PriorityClass::Background), 0)
            .unwrap();
        queue
            .enqueue(request(2, PriorityClass::InteractiveBlocking), 1)
            .unwrap();
        let mut ledger = MemoryLedger::new(MemoryAmount::ram(100));
        let admitted = queue
            .admit(
                &mut ledger,
                policy(),
                ActiveClassCounts::default(),
                &ActivePrincipalCounts::default(),
                2,
                &mut Vec::new(),
            )
            .unwrap()
            .unwrap();
        assert_eq!(admitted.request.request_id, 2);
    }

    #[test]
    fn sched_queue_aging_promotes_one_class_per_quantum() {
        let mut queue = AdmissionQueue::new(8).unwrap();
        queue
            .enqueue(request(1, PriorityClass::Background), 0)
            .unwrap();
        queue
            .enqueue(
                request(2, PriorityClass::Interactive),
                AGING_STEP_QUANTUM * 3,
            )
            .unwrap();
        let mut ledger = MemoryLedger::new(MemoryAmount::ram(100));
        let admitted = queue
            .admit(
                &mut ledger,
                policy(),
                ActiveClassCounts::default(),
                &ActivePrincipalCounts::default(),
                AGING_STEP_QUANTUM * 4,
                &mut Vec::new(),
            )
            .unwrap()
            .unwrap();
        assert_eq!(admitted.request.request_id, 1);
    }

    #[test]
    fn sched_queue_quota_filter_does_not_mutate_queue() {
        let mut queue = AdmissionQueue::new(4).unwrap();
        queue.enqueue(request(1, PriorityClass::Eval), 0).unwrap();
        let mut active = ActiveClassCounts::default();
        active.add(PriorityClass::Eval);
        let mut ledger = MemoryLedger::new(MemoryAmount::ram(100));
        assert!(queue
            .admit(
                &mut ledger,
                policy(),
                active,
                &ActivePrincipalCounts::default(),
                1,
                &mut Vec::new(),
            )
            .unwrap()
            .is_none());
        assert_eq!(queue.len(), 1);
        assert_eq!(ledger.available().ram_bytes, 100);
    }

    #[test]
    fn sched_queue_eval_blocks_and_is_blocked_by_other_active_work() {
        let policy = policy();
        let mut eval_active = ActiveClassCounts::default();
        eval_active.add(PriorityClass::Eval);
        assert!(!eval_active.can_admit(policy, PriorityClass::InteractiveBlocking));
        assert!(!eval_active.can_admit(policy, PriorityClass::Worker));

        let mut worker_active = ActiveClassCounts::default();
        worker_active.add(PriorityClass::Worker);
        assert!(!worker_active.can_admit(policy, PriorityClass::Eval));
        assert!(ActiveClassCounts::default().can_admit(policy, PriorityClass::Eval));
    }

    #[test]
    fn sched_queue_ledger_refusal_leaves_request_queued() {
        let mut queue = AdmissionQueue::new(1).unwrap();
        let mut oversized = request(1, PriorityClass::Worker);
        oversized.kv_bytes = 101;
        queue.enqueue(oversized, 0).unwrap();
        let mut ledger = MemoryLedger::new(MemoryAmount::ram(100));
        assert_eq!(
            queue.admit(
                &mut ledger,
                policy(),
                ActiveClassCounts::default(),
                &ActivePrincipalCounts::default(),
                1,
                &mut Vec::new(),
            ),
            Err(SchedError::Oom {
                requested_bytes: 101
            })
        );
        assert_eq!(queue.len(), 1);
        assert_eq!(ledger.available().ram_bytes, 100);
    }

    #[test]
    fn sched_admission_receipt_releases_exact_reservation() {
        let mut queue = AdmissionQueue::new(1).unwrap();
        queue
            .enqueue(request(4, PriorityClass::Interactive), 2)
            .unwrap();
        let mut ledger = MemoryLedger::new(MemoryAmount::ram(100));
        let receipt = queue
            .admit(
                &mut ledger,
                policy(),
                ActiveClassCounts::default(),
                &ActivePrincipalCounts::default(),
                7,
                &mut Vec::new(),
            )
            .unwrap()
            .unwrap();
        assert_eq!(ledger.available().ram_bytes, 90);
        let released = receipt.release(&mut ledger).unwrap();
        assert_eq!(released.request_id, 4);
        assert_eq!(ledger.available().ram_bytes, 100);
    }

    #[test]
    fn sched_admission_rollback_preserves_queue_age_and_releases_ledger() {
        let mut queue = AdmissionQueue::new(2).unwrap();
        queue.enqueue(request(4, PriorityClass::Worker), 2).unwrap();
        let mut ledger = MemoryLedger::new(MemoryAmount::ram(100));
        let receipt = queue
            .admit(
                &mut ledger,
                policy(),
                ActiveClassCounts::default(),
                &ActivePrincipalCounts::default(),
                7,
                &mut Vec::new(),
            )
            .unwrap()
            .unwrap();
        queue.rollback_admission(receipt, &mut ledger).unwrap();
        assert_eq!(queue.len(), 1);
        assert_eq!(ledger.available().ram_bytes, 100);
        let admitted = queue
            .admit(
                &mut ledger,
                policy(),
                ActiveClassCounts::default(),
                &ActivePrincipalCounts::default(),
                10,
                &mut Vec::new(),
            )
            .unwrap()
            .unwrap();
        assert_eq!(admitted.request.request_id, 4);
    }

    #[test]
    fn sched_queue_rejects_overflow_and_duplicate_ids() {
        let mut queue = AdmissionQueue::new(2).unwrap();
        let mut overflow = request(1, PriorityClass::Worker);
        overflow.prompt_tokens = u32::MAX;
        assert!(matches!(
            queue.enqueue(overflow, 0),
            Err(SchedError::ContextOverflow { .. })
        ));
        queue.enqueue(request(2, PriorityClass::Worker), 0).unwrap();
        assert_eq!(
            queue.enqueue(request(2, PriorityClass::Background), 0),
            Err(SchedError::InvalidRequest("duplicate request id"))
        );
    }

    #[test]
    fn sched_queue_capacity_accepts_n_rejects_n_plus_one_and_recovers() {
        let mut queue = AdmissionQueue::new(4).unwrap();
        for id in 1..=4 {
            let mut queued = request(id, PriorityClass::Worker);
            queued.principal_id = if id <= 2 {
                "principal-a".to_owned()
            } else {
                "principal-b".to_owned()
            };
            queue.enqueue(queued, id).unwrap();
        }
        let mut overflow = request(5, PriorityClass::Worker);
        overflow.principal_id = "principal-c".to_owned();
        assert_eq!(queue.enqueue(overflow, 5), Err(SchedError::QueueFull));
        assert_eq!(queue.len(), 4);

        queue.remove_request(1).unwrap();
        let mut recovered = request(5, PriorityClass::Worker);
        recovered.principal_id = "principal-a".to_owned();
        queue.enqueue(recovered, 6).unwrap();
        assert_eq!(queue.len(), 4);
    }

    #[test]
    fn sched_queue_principal_share_preserves_capacity_for_a_second_principal() {
        let mut queue = AdmissionQueue::new(4).unwrap();
        queue.enqueue(request(1, PriorityClass::Worker), 0).unwrap();
        queue.enqueue(request(2, PriorityClass::Worker), 0).unwrap();
        assert_eq!(
            queue.enqueue(request(3, PriorityClass::Worker), 0),
            Err(SchedError::QuotaFull {
                priority: PriorityClass::Worker,
            })
        );

        for id in 3..=4 {
            let mut second = request(id, PriorityClass::Worker);
            second.principal_id = "principal-b".to_owned();
            queue.enqueue(second, 0).unwrap();
        }
        assert_eq!(queue.len(), 4);
    }

    #[test]
    fn sched_queue_contended_active_share_admits_underrepresented_principal() {
        let mut queue = AdmissionQueue::new(4).unwrap();
        queue.enqueue(request(1, PriorityClass::Worker), 0).unwrap();
        let mut second = request(2, PriorityClass::Worker);
        second.principal_id = "principal-b".to_owned();
        queue.enqueue(second, 0).unwrap();
        let mut active_classes = ActiveClassCounts::default();
        active_classes.add(PriorityClass::Worker);
        active_classes.add(PriorityClass::Worker);
        let mut active_principals = ActivePrincipalCounts::default();
        active_principals.add("principal-a");
        active_principals.add("principal-a");
        let mut ledger = MemoryLedger::new(MemoryAmount::ram(100));

        let admitted = queue
            .admit(
                &mut ledger,
                policy(),
                active_classes,
                &active_principals,
                1,
                &mut Vec::new(),
            )
            .unwrap()
            .unwrap();

        assert_eq!(admitted.request.principal_id, "principal-b");
    }
}
