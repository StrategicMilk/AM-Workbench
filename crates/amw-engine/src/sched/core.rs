//! Composite scheduler ownership boundary used by the blocking model worker.

use std::{
    collections::{BTreeMap, BTreeSet},
    path::PathBuf,
};

use crate::hw::budget::{MemoryAmount, MemoryLedger};

use super::{
    ActivePrincipalCounts, ActiveSequence, AdmissionQueue, AdmissionReceipt,
    AdmissionReleaseReceipt, AdmissionRequest, BatchLoop, BatchPlan, BatchStep, DecodeProgress,
    EventSink, KvManager, KvSessionRestoreOptions, NamedPrefixRegistry, PrefixReusePlan,
    PrefixSnapshot, PriorityClass, PriorityPolicy, ReadmissionReceipt, SchedError, SeqId,
    SequenceBackend, SequenceReleaseReceipt, SessionContinuation, Slot, SlotState, StaticKvPolicy,
    TerminationReason,
};

#[derive(Clone, Debug)]
pub struct SchedulerCoreConfig {
    pub slot_count: usize,
    pub native_sequence_capacity: u32,
    pub queue_capacity: usize,
    pub batch_token_budget: u32,
    pub preemption_enabled: bool,
    pub kv_capacity_cells: u32,
    pub kv_bytes_per_cell: u64,
    pub admission_memory: MemoryAmount,
    pub kv_memory: MemoryAmount,
    pub session_dir: PathBuf,
    pub prefix_capacity: usize,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct CoreAdmissionReceipt {
    pub request_id: u64,
    pub seq_id: SeqId,
    pub slot_id: Option<usize>,
    pub priority: PriorityClass,
    pub prefix_hit_tokens: u32,
    pub max_output_tokens: u32,
    pub decode_steps: u32,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct CoreReleaseReceipt {
    pub sequence: SequenceReleaseReceipt,
    pub admission: AdmissionReleaseReceipt,
    pub released_kv_cells: u32,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct CoreResumeReceipt {
    pub seq_id: SeqId,
    pub slot_id: usize,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct CoreSuspensionReceipt {
    pub seq_id: SeqId,
    pub released_slot_id: usize,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct CoreReadmissionReceipt {
    pub request_id: u64,
    pub sequence: ReadmissionReceipt,
    pub admission: AdmissionReleaseReceipt,
    pub remaining_prefill_tokens: u32,
    pub remaining_decode_steps: u32,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct CoreAdmissionOutcome {
    pub admitted: Option<CoreAdmissionReceipt>,
    pub readmissions: Vec<CoreReadmissionReceipt>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct SchedulerSnapshot {
    pub queue_depth: usize,
    pub active: Vec<CoreAdmissionReceipt>,
    pub slots: Vec<(usize, SlotState)>,
    pub kv_used_cells: u32,
    pub prefixes: Vec<PrefixSnapshot>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct CoreStepReceipt {
    pub batch: BatchStep,
    pub released: Vec<CoreReleaseReceipt>,
    pub suspended: Vec<CoreSuspensionReceipt>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ScratchSequenceReceipt {
    pub seq_id: SeqId,
    pub cells: u32,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct CoreSessionRestoreReceipt {
    pub admission: CoreAdmissionReceipt,
    pub continuation_token: i32,
    pub continuation_position: u32,
    pub appended_prompt_tokens: u32,
}

/// Admission, identity, and timing inputs for restoring one scheduler session.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct CoreSessionRestoreOptions {
    pub request: AdmissionRequest,
    pub expected_model_fingerprint: [u8; 32],
    pub expected_owner_fingerprint: [u8; 32],
    pub appended_prompt_tokens: u32,
    pub now_step: u64,
}

#[derive(Debug, Eq, PartialEq)]
pub struct CoreAdmissionFailure {
    pub request_id: u64,
    pub error: SchedError,
    pub readmissions: Vec<CoreReadmissionReceipt>,
}

#[derive(Debug)]
struct ActiveLease {
    request_id: u64,
    priority: PriorityClass,
    slot_id: Option<usize>,
    prefix_hit_tokens: u32,
    max_output_tokens: u32,
    decode_steps: u32,
    pressure_evictable: bool,
    admission: AdmissionReceipt,
}

#[derive(Clone, Copy, Debug)]
enum SessionRestoreSource<'a> {
    Stored(&'a str),
    Payload(&'a [u8]),
}

#[derive(Debug)]
struct MaterializedAdmission {
    receipt: CoreAdmissionReceipt,
    readmissions: Vec<CoreReadmissionReceipt>,
}

#[derive(Debug)]
struct MaterializeAdmissionFailure {
    error: SchedError,
    admission: AdmissionReceipt,
    readmissions: Vec<CoreReadmissionReceipt>,
}

impl MaterializeAdmissionFailure {
    fn boxed(
        error: SchedError,
        admission: AdmissionReceipt,
        readmissions: Vec<CoreReadmissionReceipt>,
    ) -> Box<Self> {
        Box::new(Self {
            error,
            admission,
            readmissions,
        })
    }
}

#[derive(Debug)]
pub struct SchedulerCore {
    policy: PriorityPolicy,
    queue: AdmissionQueue,
    admission_ledger: MemoryLedger,
    slots: Vec<Slot>,
    slot_owners: Vec<Option<SeqId>>,
    batch: BatchLoop,
    kv: KvManager,
    kv_policy: StaticKvPolicy,
    prefixes: NamedPrefixRegistry,
    active: BTreeMap<SeqId, ActiveLease>,
    scratch_sequences: BTreeSet<SeqId>,
    suspended_sequences: BTreeSet<SeqId>,
}

impl SchedulerCore {
    pub fn new(config: SchedulerCoreConfig) -> Result<Self, SchedError> {
        if config.kv_bytes_per_cell == 0 {
            return Err(SchedError::InvalidRequest(
                "KV bytes per cell must be positive",
            ));
        }
        let policy = PriorityPolicy::new(config.slot_count, config.batch_token_budget)?;
        let queue = AdmissionQueue::new(config.queue_capacity)?;
        let kv = KvManager::new(
            config.kv_capacity_cells,
            config.native_sequence_capacity,
            config.session_dir,
            MemoryLedger::new(config.kv_memory),
        )?;
        let prefixes = NamedPrefixRegistry::new(config.prefix_capacity)?;
        Ok(Self {
            policy,
            queue,
            admission_ledger: MemoryLedger::new(config.admission_memory),
            slots: (0..config.slot_count).map(Slot::new).collect(),
            slot_owners: vec![None; config.slot_count],
            batch: BatchLoop::new(policy, config.preemption_enabled),
            kv,
            kv_policy: StaticKvPolicy {
                bytes_per_cell: config.kv_bytes_per_cell,
            },
            prefixes,
            active: BTreeMap::new(),
            scratch_sequences: BTreeSet::new(),
            suspended_sequences: BTreeSet::new(),
        })
    }

    pub fn submit(&mut self, request: AdmissionRequest, now_step: u64) -> Result<(), SchedError> {
        let priority = request.priority;
        self.queue.enqueue(request, now_step)?;
        if priority == PriorityClass::InteractiveBlocking {
            self.refresh_blocking_arrival_signal();
        }
        Ok(())
    }

    /// Admits one queued request and rolls back all acquired resources on error.
    pub fn admit(
        &mut self,
        backend: &mut impl SequenceBackend,
        now_step: u64,
        sink: &mut impl EventSink,
    ) -> Result<Option<CoreAdmissionReceipt>, SchedError> {
        self.admit_identified(backend, now_step, sink)
            .map_err(|failure| failure.error)
    }

    pub fn admit_identified(
        &mut self,
        backend: &mut impl SequenceBackend,
        now_step: u64,
        sink: &mut impl EventSink,
    ) -> Result<Option<CoreAdmissionReceipt>, CoreAdmissionFailure> {
        self.admit_with_prefix_identified(backend, now_step, None, sink)
    }

    /// Admits one request and transactionally materializes a matched prefix.
    pub fn admit_with_prefix(
        &mut self,
        backend: &mut impl SequenceBackend,
        now_step: u64,
        prefix: Option<PrefixReusePlan>,
        sink: &mut impl EventSink,
    ) -> Result<Option<CoreAdmissionReceipt>, SchedError> {
        self.admit_with_prefix_identified(backend, now_step, prefix, sink)
            .map_err(|failure| failure.error)
    }

    pub fn admit_with_prefix_identified(
        &mut self,
        backend: &mut impl SequenceBackend,
        now_step: u64,
        prefix: Option<PrefixReusePlan>,
        sink: &mut impl EventSink,
    ) -> Result<Option<CoreAdmissionReceipt>, CoreAdmissionFailure> {
        self.admit_with_prefix_mode(backend, now_step, prefix, false, sink)
            .map(|outcome| outcome.admitted)
    }

    /// Admits one request and explicitly converts eligible suspended Background KV into
    /// typed readmission receipts only when the new allocation encounters pressure.
    pub fn admit_with_prefix_under_pressure_identified(
        &mut self,
        backend: &mut impl SequenceBackend,
        now_step: u64,
        prefix: Option<PrefixReusePlan>,
        sink: &mut impl EventSink,
    ) -> Result<CoreAdmissionOutcome, CoreAdmissionFailure> {
        self.admit_with_prefix_mode(backend, now_step, prefix, true, sink)
    }

    fn admit_with_prefix_mode(
        &mut self,
        backend: &mut impl SequenceBackend,
        now_step: u64,
        prefix: Option<PrefixReusePlan>,
        allow_pressure_eviction: bool,
        sink: &mut impl EventSink,
    ) -> Result<CoreAdmissionOutcome, CoreAdmissionFailure> {
        let active = self.batch.active_counts();
        let active_principals = self.active_principal_counts();
        let Some(request_id) = self.queue.next_admissible_request_id(
            self.policy,
            active,
            &active_principals,
            now_step,
        ) else {
            return Ok(CoreAdmissionOutcome {
                admitted: None,
                readmissions: Vec::new(),
            });
        };
        if prefix.as_ref().is_some_and(|plan| {
            plan.request_id() != request_id || !self.prefixes.validates_reuse(plan)
        }) {
            return Err(CoreAdmissionFailure {
                request_id,
                error: SchedError::InvalidRequest(
                    "prefix reuse proof is stale, mismatched, or no longer pinned",
                ),
                readmissions: Vec::new(),
            });
        }
        let Some(admission) = self
            .queue
            .admit(
                &mut self.admission_ledger,
                self.policy,
                active,
                &active_principals,
                now_step,
                sink,
            )
            .map_err(|error| CoreAdmissionFailure {
                request_id,
                error,
                readmissions: Vec::new(),
            })?
        else {
            return Ok(CoreAdmissionOutcome {
                admitted: None,
                readmissions: Vec::new(),
            });
        };
        match self.materialize_admission(backend, admission, prefix, allow_pressure_eviction, sink)
        {
            Ok(materialized) => {
                self.refresh_blocking_arrival_signal();
                Ok(CoreAdmissionOutcome {
                    admitted: Some(materialized.receipt),
                    readmissions: materialized.readmissions,
                })
            }
            Err(failure) => {
                let MaterializeAdmissionFailure {
                    error,
                    admission,
                    readmissions,
                } = *failure;
                self.queue
                    .rollback_admission(admission, &mut self.admission_ledger)
                    .map_err(|error| CoreAdmissionFailure {
                        request_id,
                        error,
                        readmissions: readmissions.clone(),
                    })?;
                Err(CoreAdmissionFailure {
                    request_id,
                    error,
                    readmissions,
                })
            }
        }
    }

    pub fn drop_queued(&mut self, request_id: u64) -> Result<AdmissionRequest, SchedError> {
        let request = self.queue.remove_request(request_id)?;
        self.refresh_blocking_arrival_signal();
        Ok(request)
    }

    pub fn next_admissible_request_id(&self, now_step: u64) -> Option<u64> {
        let active_principals = self.active_principal_counts();
        self.queue.next_admissible_request_id(
            self.policy,
            self.batch.active_counts(),
            &active_principals,
            now_step,
        )
    }

    pub fn plan_step(&self) -> BatchPlan {
        self.batch.plan_step()
    }

    /// Commits a non-speculative backend-successful step.
    pub fn commit_step(
        &mut self,
        backend: &mut impl SequenceBackend,
        plan: BatchPlan,
        sink: &mut impl EventSink,
    ) -> Result<CoreStepReceipt, SchedError> {
        let decode_progress = plan
            .work
            .iter()
            .filter(|work| work.phase == super::SequencePhase::Decode)
            .map(|work| DecodeProgress {
                seq_id: work.seq_id,
                compute_tokens: 1,
                output_tokens: 1,
            })
            .collect::<Vec<_>>();
        self.commit_step_with_progress(backend, plan, &decode_progress, sink)
    }

    /// Commits actual backend work against the immutable per-sequence grants.
    pub fn commit_step_with_progress(
        &mut self,
        backend: &mut impl SequenceBackend,
        plan: BatchPlan,
        decode_progress: &[DecodeProgress],
        sink: &mut impl EventSink,
    ) -> Result<CoreStepReceipt, SchedError> {
        for seq_id in &plan.preempted_sequence_ids {
            if self.kv.sequence_priority(*seq_id) != Some(PriorityClass::Background) {
                return Err(SchedError::InvalidRequest(
                    "preemption plan does not own Background KV state",
                ));
            }
            let lease = self
                .active
                .get(seq_id)
                .ok_or(SchedError::UnknownSequence(*seq_id))?;
            let slot_id = lease.slot_id.ok_or(SchedError::InvalidRequest(
                "preemption plan sequence has no physical slot",
            ))?;
            if self.suspended_sequences.contains(seq_id)
                || self.slot_owners[slot_id] != Some(*seq_id)
                || !matches!(
                    self.slots[slot_id].state(),
                    SlotState::Prefill | SlotState::Decode
                )
            {
                return Err(SchedError::InvalidRequest(
                    "preemption plan does not own an active physical slot",
                ));
            }
        }
        let queue_depth = self.queue.len();
        let batch = self
            .batch
            .commit_step(plan, decode_progress, queue_depth, sink)?;
        let mut suspended = Vec::with_capacity(batch.preempted_sequence_ids.len());
        for seq_id in &batch.preempted_sequence_ids {
            self.kv.mark_preempted(*seq_id, true)?;
            let lease = self
                .active
                .get(seq_id)
                .ok_or(SchedError::UnknownSequence(*seq_id))?;
            let slot_id = lease.slot_id.ok_or(SchedError::InvalidRequest(
                "preempted sequence lost its physical slot",
            ))?;
            self.slots[slot_id].suspend(sink)?;
            self.slot_owners[slot_id] = None;
            self.active
                .get_mut(seq_id)
                .ok_or(SchedError::UnknownSequence(*seq_id))?
                .slot_id = None;
            self.suspended_sequences.insert(*seq_id);
            suspended.push(CoreSuspensionReceipt {
                seq_id: *seq_id,
                released_slot_id: slot_id,
            });
        }
        let releasing: std::collections::BTreeSet<_> = batch
            .release_receipts
            .iter()
            .map(|receipt| receipt.seq_id)
            .collect();
        for seq_id in &batch.sequence_ids {
            if releasing.contains(seq_id) {
                continue;
            }
            if let Some(lease) = self.active.get(seq_id) {
                let slot_id = lease.slot_id.ok_or(SchedError::InvalidRequest(
                    "planned sequence does not own a physical slot",
                ))?;
                let slot = &mut self.slots[slot_id];
                let prefill_complete = self
                    .batch
                    .active()
                    .iter()
                    .find(|sequence| sequence.seq_id == *seq_id)
                    .is_some_and(|sequence| sequence.prefill_tokens_remaining == 0);
                if slot.state() == SlotState::Prefill && prefill_complete {
                    slot.transition(SlotState::Decode, sink)?;
                }
            }
        }
        let mut released = Vec::with_capacity(batch.release_receipts.len());
        for receipt in &batch.release_receipts {
            released.push(self.cleanup_sequence(backend, receipt.clone(), sink)?);
        }
        self.refresh_blocking_arrival_signal();
        Ok(CoreStepReceipt {
            batch,
            released,
            suspended,
        })
    }

    pub fn cancel(
        &mut self,
        backend: &mut impl SequenceBackend,
        seq_id: SeqId,
        sink: &mut impl EventSink,
    ) -> Result<CoreReleaseReceipt, SchedError> {
        let receipt = if self.batch.active().iter().any(|item| item.seq_id == seq_id) {
            self.batch.cancel(seq_id)?
        } else {
            let lease = self
                .active
                .get(&seq_id)
                .ok_or(SchedError::UnknownSequence(seq_id))?;
            SequenceReleaseReceipt {
                seq_id,
                slot_id: lease.slot_id,
                priority: lease.priority,
                reason: TerminationReason::Cancelled,
            }
        };
        let released = self.cleanup_sequence(backend, receipt, sink)?;
        self.refresh_blocking_arrival_signal();
        Ok(released)
    }

    /// Terminates active generation early and cleans every owned resource.
    pub fn terminate(
        &mut self,
        backend: &mut impl SequenceBackend,
        seq_id: SeqId,
        reason: TerminationReason,
        sink: &mut impl EventSink,
    ) -> Result<CoreReleaseReceipt, SchedError> {
        let receipt = if self.batch.active().iter().any(|item| item.seq_id == seq_id) {
            self.batch.terminate(seq_id, reason)?
        } else {
            let lease = self
                .active
                .get(&seq_id)
                .ok_or(SchedError::UnknownSequence(seq_id))?;
            SequenceReleaseReceipt {
                seq_id,
                slot_id: lease.slot_id,
                priority: lease.priority,
                reason,
            }
        };
        let released = self.cleanup_sequence(backend, receipt, sink)?;
        self.refresh_blocking_arrival_signal();
        Ok(released)
    }

    pub fn resume(&mut self, seq_id: SeqId, sink: &mut impl EventSink) -> Result<(), SchedError> {
        let sequence = self
            .batch
            .active()
            .iter()
            .find(|sequence| sequence.seq_id == seq_id)
            .ok_or(SchedError::UnknownSequence(seq_id))?;
        if sequence.priority != PriorityClass::Background || !sequence.preempted {
            return Err(SchedError::InvalidRequest(
                "only a preempted Background sequence may resume",
            ));
        }
        if self.kv.sequence_priority(seq_id) != Some(PriorityClass::Background) {
            return Err(SchedError::InvalidRequest(
                "resumed sequence does not own Background KV state",
            ));
        }
        if !self.suspended_sequences.contains(&seq_id) {
            return Err(SchedError::InvalidRequest(
                "resumed sequence does not own suspended slot state",
            ));
        }
        self.resume_one(seq_id, sink).map(|_| ())
    }

    /// Rebinds one suspended Background sequence after the caller owns a global permit.
    pub fn resume_next(
        &mut self,
        sink: &mut impl EventSink,
    ) -> Result<Option<CoreResumeReceipt>, SchedError> {
        let blocking_active = self
            .active
            .values()
            .any(|lease| lease.priority == PriorityClass::InteractiveBlocking);
        if blocking_active
            || self
                .queue
                .contains_priority(PriorityClass::InteractiveBlocking)
        {
            return Ok(None);
        }
        let Some(seq_id) = self.suspended_sequences.iter().next().copied() else {
            return Ok(None);
        };
        if !self
            .slots
            .iter()
            .any(|slot| slot.state() == SlotState::Idle)
        {
            return Ok(None);
        }
        self.resume_one(seq_id, sink).map(Some)
    }

    pub fn set_eval_deadline(
        &mut self,
        seq_id: SeqId,
        deadline_step: u64,
    ) -> Result<(), SchedError> {
        self.batch.set_eval_deadline(seq_id, deadline_step)
    }

    /// Retries cleanup for a sequence whose prior backend removal failed.
    pub fn remove(
        &mut self,
        backend: &mut impl SequenceBackend,
        seq_id: SeqId,
        reason: TerminationReason,
        sink: &mut impl EventSink,
    ) -> Result<CoreReleaseReceipt, SchedError> {
        let lease = self
            .active
            .get(&seq_id)
            .ok_or(SchedError::UnknownSequence(seq_id))?;
        let receipt = SequenceReleaseReceipt {
            seq_id,
            slot_id: lease.slot_id,
            priority: lease.priority,
            reason,
        };
        let released = self.cleanup_sequence(backend, receipt, sink)?;
        self.refresh_blocking_arrival_signal();
        Ok(released)
    }

    pub fn drain(
        &mut self,
        backend: &mut impl SequenceBackend,
        sink: &mut impl EventSink,
    ) -> Result<Vec<CoreReleaseReceipt>, SchedError> {
        let batch_receipts: BTreeMap<_, _> = self
            .batch
            .drain()
            .into_iter()
            .map(|receipt| (receipt.seq_id, receipt))
            .collect();
        let receipts: Vec<_> = self
            .active
            .iter()
            .map(|(&seq_id, lease)| {
                batch_receipts
                    .get(&seq_id)
                    .cloned()
                    .unwrap_or(SequenceReleaseReceipt {
                        seq_id,
                        slot_id: lease.slot_id,
                        priority: lease.priority,
                        reason: TerminationReason::Drained,
                    })
            })
            .collect();
        let mut released = Vec::with_capacity(receipts.len());
        for receipt in receipts {
            released.push(self.cleanup_sequence(backend, receipt, sink)?);
        }
        Ok(released)
    }

    pub fn snapshot(&self) -> SchedulerSnapshot {
        SchedulerSnapshot {
            queue_depth: self.queue.len(),
            active: self
                .active
                .iter()
                .map(|(&seq_id, lease)| CoreAdmissionReceipt {
                    request_id: lease.request_id,
                    seq_id,
                    slot_id: lease.slot_id,
                    priority: lease.priority,
                    prefix_hit_tokens: lease.prefix_hit_tokens,
                    max_output_tokens: lease.max_output_tokens,
                    decode_steps: lease.decode_steps,
                })
                .collect(),
            slots: self
                .slots
                .iter()
                .map(|slot| (slot.id(), slot.state()))
                .collect(),
            kv_used_cells: self.kv.used_cells(),
            prefixes: self.prefixes.snapshot(),
        }
    }

    pub const fn background_evicted(&self) -> u64 {
        self.kv.background_evicted()
    }

    pub fn register_prefix(
        &mut self,
        name: impl Into<String>,
        tokens: Vec<i32>,
        cells: u32,
        sink: &mut impl EventSink,
    ) -> Result<(), SchedError> {
        self.prefixes.register(name, tokens, cells, sink)
    }

    pub fn pin_prefix(
        &mut self,
        name: &str,
        sink: &mut impl EventSink,
    ) -> Result<SeqId, SchedError> {
        self.prefixes
            .pin_with_kv(name, &mut self.kv, &mut self.kv_policy, sink)
    }

    pub fn unpin_prefix(
        &mut self,
        backend: &mut impl SequenceBackend,
        name: &str,
        sink: &mut impl EventSink,
    ) -> Result<bool, SchedError> {
        self.prefixes
            .unpin_with_kv(name, &mut self.kv, backend, sink)
    }

    pub fn match_prefix(
        &mut self,
        name: &str,
        tokens: &[i32],
        sink: &mut impl EventSink,
    ) -> Result<bool, SchedError> {
        self.prefixes.match_tokens(name, tokens, sink)
    }

    pub fn match_prefix_for_reuse(
        &mut self,
        request_id: u64,
        name: &str,
        tokens: &[i32],
        sink: &mut impl EventSink,
    ) -> Result<Option<PrefixReusePlan>, SchedError> {
        self.prefixes
            .match_for_reuse(request_id, name, tokens, sink)
    }

    pub fn save_session(
        &self,
        backend: &mut impl SequenceBackend,
        session_id: &str,
        seq_id: SeqId,
        continuation: SessionContinuation,
        owner_fingerprint: [u8; 32],
    ) -> Result<PathBuf, SchedError> {
        if !self.active.contains_key(&seq_id) {
            return Err(SchedError::UnknownSequence(seq_id));
        }
        self.kv
            .save_session(backend, session_id, seq_id, continuation, owner_fingerprint)
    }

    /// Encodes an active native sequence for a process-wide persistence authority.
    pub fn export_session_payload(
        &self,
        backend: &mut impl SequenceBackend,
        seq_id: SeqId,
        continuation: SessionContinuation,
        owner_fingerprint: [u8; 32],
    ) -> Result<Vec<u8>, SchedError> {
        if !self.active.contains_key(&seq_id) {
            return Err(SchedError::UnknownSequence(seq_id));
        }
        self.kv
            .export_session_payload(backend, seq_id, continuation, owner_fingerprint)
    }

    /// Returns the exact opaque payload size before native export allocation.
    pub fn session_payload_size(
        &self,
        backend: &mut impl SequenceBackend,
        seq_id: SeqId,
    ) -> Result<u64, SchedError> {
        if !self.active.contains_key(&seq_id) {
            return Err(SchedError::UnknownSequence(seq_id));
        }
        self.kv.session_payload_size(backend, seq_id)
    }

    /// Persists a model-bound empty session before the first generation.
    pub fn create_session(
        &self,
        session_id: &str,
        model_fingerprint: [u8; 32],
        owner_fingerprint: [u8; 32],
    ) -> Result<PathBuf, SchedError> {
        self.kv
            .create_session(session_id, model_fingerprint, owner_fingerprint)
    }

    /// Reserves a collision-free native sequence for speculative KV work.
    pub fn reserve_scratch_sequence(
        &mut self,
        cells: u32,
        priority: PriorityClass,
        sink: &mut impl EventSink,
    ) -> Result<ScratchSequenceReceipt, SchedError> {
        let seq_id = self
            .kv
            .allocate(cells, priority, &mut self.kv_policy, sink)?;
        self.scratch_sequences.insert(seq_id);
        Ok(ScratchSequenceReceipt { seq_id, cells })
    }

    /// Clears native scratch state before releasing its exact ledger ownership.
    pub fn release_scratch_sequence(
        &mut self,
        backend: &mut impl SequenceBackend,
        seq_id: SeqId,
        sink: &mut impl EventSink,
    ) -> Result<ScratchSequenceReceipt, SchedError> {
        if !self.scratch_sequences.contains(&seq_id) {
            return Err(SchedError::UnknownSequence(seq_id));
        }
        let removed = self.kv.remove(backend, seq_id, sink)?;
        self.scratch_sequences.remove(&seq_id);
        Ok(ScratchSequenceReceipt {
            seq_id,
            cells: removed.released_cells,
        })
    }

    /// Releases scheduler ownership after a transaction has proven scratch KV empty.
    pub fn release_empty_scratch_sequence(
        &mut self,
        backend: &mut impl SequenceBackend,
        seq_id: SeqId,
        sink: &mut impl EventSink,
    ) -> Result<ScratchSequenceReceipt, SchedError> {
        if !self.scratch_sequences.contains(&seq_id) {
            return Err(SchedError::UnknownSequence(seq_id));
        }
        if backend.sequence_position_max(seq_id)? != -1 {
            return Err(SchedError::InvalidRequest(
                "scratch allocation still owns native sequence state",
            ));
        }
        let cells = self
            .kv
            .sequence_cells(seq_id)
            .ok_or(SchedError::UnknownSequence(seq_id))?;
        self.kv.discard_allocation(seq_id, sink)?;
        self.scratch_sequences.remove(&seq_id);
        Ok(ScratchSequenceReceipt { seq_id, cells })
    }

    pub fn session_ids(&self) -> Result<Vec<String>, SchedError> {
        self.kv.session_ids()
    }

    pub fn session_has_state(&self, session_id: &str) -> Result<bool, SchedError> {
        self.kv.session_has_state(session_id)
    }

    pub fn session_owner_fingerprint(&self, session_id: &str) -> Result<[u8; 32], SchedError> {
        self.kv.session_owner_fingerprint(session_id)
    }

    pub fn delete_session(
        &self,
        session_id: &str,
        owner_fingerprint: [u8; 32],
    ) -> Result<(), SchedError> {
        self.kv.delete_session(session_id, owner_fingerprint)
    }

    /// Returns the persisted token and position required to resume a session.
    pub fn session_continuation(
        &self,
        session_id: &str,
    ) -> Result<SessionContinuation, SchedError> {
        self.kv.session_continuation(session_id)
    }

    /// Reads continuation metadata from an opaque process-wide snapshot.
    pub fn session_payload_continuation(
        payload: &[u8],
        expected_model_fingerprint: [u8; 32],
        expected_owner_fingerprint: [u8; 32],
    ) -> Result<(PriorityClass, SessionContinuation), SchedError> {
        KvManager::session_payload_continuation(
            payload,
            expected_model_fingerprint,
            expected_owner_fingerprint,
        )
    }

    /// Restores a saved native sequence into a new admitted slot.
    pub fn restore_session(
        &mut self,
        backend: &mut impl SequenceBackend,
        session_id: &str,
        options: CoreSessionRestoreOptions,
        sink: &mut impl EventSink,
    ) -> Result<CoreSessionRestoreReceipt, SchedError> {
        self.restore_session_source(
            backend,
            SessionRestoreSource::Stored(session_id),
            options,
            sink,
        )
    }

    /// Restores an opaque snapshot read by the process-wide session store.
    pub fn restore_session_payload(
        &mut self,
        backend: &mut impl SequenceBackend,
        payload: &[u8],
        options: CoreSessionRestoreOptions,
        sink: &mut impl EventSink,
    ) -> Result<CoreSessionRestoreReceipt, SchedError> {
        self.restore_session_source(
            backend,
            SessionRestoreSource::Payload(payload),
            options,
            sink,
        )
    }

    fn restore_session_source(
        &mut self,
        backend: &mut impl SequenceBackend,
        source: SessionRestoreSource<'_>,
        options: CoreSessionRestoreOptions,
        sink: &mut impl EventSink,
    ) -> Result<CoreSessionRestoreReceipt, SchedError> {
        let CoreSessionRestoreOptions {
            request,
            expected_model_fingerprint,
            expected_owner_fingerprint,
            appended_prompt_tokens,
            now_step,
        } = options;
        let restore_priority = request.priority;
        let (persisted_priority, continuation) = match source {
            SessionRestoreSource::Payload(payload) => KvManager::session_payload_continuation(
                payload,
                expected_model_fingerprint,
                expected_owner_fingerprint,
            )?,
            SessionRestoreSource::Stored(session_id) => (
                self.kv.session_priority(session_id)?,
                self.kv.session_continuation(session_id)?,
            ),
        };
        if persisted_priority != restore_priority {
            return Err(SchedError::InvalidRequest(
                "session priority does not match restore request",
            ));
        }
        if continuation.model_fingerprint() != expected_model_fingerprint {
            return Err(SchedError::InvalidRequest(
                "session model fingerprint does not match the loaded model",
            ));
        }
        let accounted_prompt_tokens = continuation
            .next_position()
            .checked_add(appended_prompt_tokens)
            .ok_or(SchedError::InvalidRequest(
                "session prompt token count overflows",
            ))?;
        if request.prompt_tokens != accounted_prompt_tokens {
            return Err(SchedError::InvalidRequest(
                "session admission does not account for saved and appended prompt tokens",
            ));
        }
        let required_cells = request
            .prompt_tokens
            .checked_add(request.max_tokens)
            .ok_or(SchedError::InvalidRequest(
                "session KV cell request overflows",
            ))?;
        let Some(slot_id) = self
            .slots
            .iter()
            .position(|slot| slot.state() == SlotState::Idle)
        else {
            return Err(SchedError::QuotaFull {
                priority: restore_priority,
            });
        };
        let mut direct = AdmissionQueue::new(1)?;
        direct.enqueue(request, now_step)?;
        let active_principals = self.active_principal_counts();
        let admission = direct
            .admit(
                &mut self.admission_ledger,
                self.policy,
                self.batch.active_counts(),
                &active_principals,
                now_step,
                sink,
            )?
            .ok_or(SchedError::QuotaFull {
                priority: restore_priority,
            })?;
        if let Err(error) = self.slots[slot_id].transition(SlotState::Prefill, sink) {
            admission.release(&mut self.admission_ledger)?;
            return Err(error);
        }
        let kv_options = KvSessionRestoreOptions {
            expected_model_fingerprint,
            expected_owner_fingerprint,
            required_cells,
        };
        let restore_result = match source {
            SessionRestoreSource::Payload(payload) => self.kv.restore_session_payload(
                backend,
                payload,
                kv_options,
                &mut self.kv_policy,
                sink,
            ),
            SessionRestoreSource::Stored(session_id) => {
                self.kv
                    .restore_session(backend, session_id, kv_options, &mut self.kv_policy, sink)
            }
        };
        let restored = match restore_result {
            Ok(restored) => restored,
            Err(error) => {
                let slot_result = self.reset_prefill_slot(slot_id, sink);
                let admission_result = admission.release(&mut self.admission_ledger);
                slot_result?;
                admission_result?;
                return Err(error);
            }
        };
        let seq_id = restored.seq_id;
        let prefill_tokens_remaining =
            appended_prompt_tokens
                .checked_add(1)
                .ok_or(SchedError::InvalidRequest(
                    "session continuation prefill overflows",
                ))?;
        if let Err(error) = self.batch.add_sequence(ActiveSequence {
            seq_id,
            slot_id: Some(slot_id),
            priority: admission.request.priority,
            prefill_tokens_remaining,
            decode_tokens_remaining: admission.request.decode_steps,
            deadline_step: None,
            preempted: false,
        }) {
            let draining_result = self.slots[slot_id].transition(SlotState::Draining, sink);
            let kv_result = self.kv.remove(backend, seq_id, sink);
            let idle_result = if draining_result.is_ok() && kv_result.is_ok() {
                self.slots[slot_id].transition(SlotState::Idle, sink)
            } else {
                Ok(())
            };
            let admission_result = admission.release(&mut self.admission_ledger);
            draining_result?;
            kv_result?;
            idle_result?;
            admission_result?;
            return Err(error);
        }
        let receipt = CoreAdmissionReceipt {
            request_id: admission.request.request_id,
            seq_id,
            slot_id: Some(slot_id),
            priority: admission.request.priority,
            prefix_hit_tokens: 0,
            max_output_tokens: admission.request.max_tokens,
            decode_steps: admission.request.decode_steps,
        };
        admission.emit_admission(sink);
        self.slot_owners[slot_id] = Some(seq_id);
        self.active.insert(
            seq_id,
            ActiveLease {
                request_id: receipt.request_id,
                priority: receipt.priority,
                slot_id: Some(slot_id),
                prefix_hit_tokens: 0,
                max_output_tokens: receipt.max_output_tokens,
                decode_steps: receipt.decode_steps,
                pressure_evictable: false,
                admission,
            },
        );
        Ok(CoreSessionRestoreReceipt {
            admission: receipt,
            continuation_token: restored.continuation.last_token(),
            continuation_position: restored.continuation.next_position() - 1,
            appended_prompt_tokens,
        })
    }

    fn reset_prefill_slot(
        &mut self,
        slot_id: usize,
        sink: &mut impl EventSink,
    ) -> Result<(), SchedError> {
        self.slots[slot_id].transition(SlotState::Draining, sink)?;
        self.slots[slot_id].transition(SlotState::Idle, sink)
    }

    fn materialize_admission(
        &mut self,
        backend: &mut impl SequenceBackend,
        admission: AdmissionReceipt,
        prefix: Option<PrefixReusePlan>,
        allow_pressure_eviction: bool,
        sink: &mut impl EventSink,
    ) -> Result<MaterializedAdmission, Box<MaterializeAdmissionFailure>> {
        let mut readmissions = Vec::new();
        let prefix_hit_tokens = prefix
            .as_ref()
            .map(PrefixReusePlan::prefix_hit_tokens)
            .unwrap_or(0);
        if prefix_hit_tokens > admission.request.prompt_tokens {
            return Err(MaterializeAdmissionFailure::boxed(
                SchedError::InvalidRequest("prefix hit exceeds request prompt"),
                admission,
                readmissions,
            ));
        }
        if admission.request.max_tokens > 0 && prefix_hit_tokens == admission.request.prompt_tokens
        {
            return Err(MaterializeAdmissionFailure::boxed(
                SchedError::InvalidRequest(
                    "prefix reuse must leave one prompt token for output logits",
                ),
                admission,
                readmissions,
            ));
        }
        let Some(slot_id) = self
            .slots
            .iter()
            .position(|slot| slot.state() == SlotState::Idle)
        else {
            return Err(MaterializeAdmissionFailure::boxed(
                SchedError::QuotaFull {
                    priority: admission.request.priority,
                },
                admission,
                readmissions,
            ));
        };
        let cells = match admission
            .request
            .prompt_tokens
            .checked_add(admission.request.max_tokens)
        {
            Some(cells) => cells,
            None => {
                return Err(MaterializeAdmissionFailure::boxed(
                    SchedError::ContextOverflow {
                        requested: u32::MAX,
                        limit: admission.request.context_limit,
                    },
                    admission,
                    readmissions,
                ));
            }
        };
        let seq_id = match self.allocate_kv_for_admission(
            backend,
            cells,
            admission.request.priority,
            allow_pressure_eviction,
            &mut readmissions,
            sink,
        ) {
            Ok(seq_id) => seq_id,
            Err(error) => {
                return Err(MaterializeAdmissionFailure::boxed(
                    error,
                    admission,
                    readmissions,
                ));
            }
        };
        let has_backend_state = if let Some(plan) = &prefix {
            if let Err(error) = self
                .kv
                .copy_into(backend, plan.source_seq_id, seq_id, plan.cells)
            {
                return match self.kv.remove(backend, seq_id, sink) {
                    Ok(_) => Err(MaterializeAdmissionFailure::boxed(
                        error,
                        admission,
                        readmissions,
                    )),
                    Err(rollback_error) => Err(MaterializeAdmissionFailure::boxed(
                        rollback_error,
                        admission,
                        readmissions,
                    )),
                };
            }
            true
        } else {
            false
        };
        if let Err(error) = self.slots[slot_id].transition(SlotState::Prefill, sink) {
            return match self.rollback_unstarted_kv(backend, seq_id, has_backend_state, sink) {
                Ok(_) => Err(MaterializeAdmissionFailure::boxed(
                    error,
                    admission,
                    readmissions,
                )),
                Err(rollback_error) => Err(MaterializeAdmissionFailure::boxed(
                    rollback_error,
                    admission,
                    readmissions,
                )),
            };
        }
        let sequence = ActiveSequence {
            seq_id,
            slot_id: Some(slot_id),
            priority: admission.request.priority,
            prefill_tokens_remaining: admission.request.prompt_tokens - prefix_hit_tokens,
            decode_tokens_remaining: admission.request.decode_steps,
            deadline_step: None,
            preempted: false,
        };
        if let Err(error) = self.batch.add_sequence(sequence) {
            let drain_result = self.slots[slot_id].transition(SlotState::Draining, sink);
            let discard_result =
                self.rollback_unstarted_kv(backend, seq_id, has_backend_state, sink);
            if let Err(rollback_error) = drain_result {
                return Err(MaterializeAdmissionFailure::boxed(
                    rollback_error,
                    admission,
                    readmissions,
                ));
            }
            if let Err(rollback_error) = discard_result {
                return Err(MaterializeAdmissionFailure::boxed(
                    rollback_error,
                    admission,
                    readmissions,
                ));
            }
            if let Err(rollback_error) = self.slots[slot_id].transition(SlotState::Idle, sink) {
                return Err(MaterializeAdmissionFailure::boxed(
                    rollback_error,
                    admission,
                    readmissions,
                ));
            }
            return Err(MaterializeAdmissionFailure::boxed(
                error,
                admission,
                readmissions,
            ));
        }
        let receipt = CoreAdmissionReceipt {
            request_id: admission.request.request_id,
            seq_id,
            slot_id: Some(slot_id),
            priority: admission.request.priority,
            prefix_hit_tokens,
            max_output_tokens: admission.request.max_tokens,
            decode_steps: admission.request.decode_steps,
        };
        admission.emit_admission(sink);
        self.slot_owners[slot_id] = Some(seq_id);
        self.active.insert(
            seq_id,
            ActiveLease {
                request_id: receipt.request_id,
                priority: receipt.priority,
                slot_id: Some(slot_id),
                prefix_hit_tokens,
                max_output_tokens: receipt.max_output_tokens,
                decode_steps: receipt.decode_steps,
                pressure_evictable: true,
                admission,
            },
        );
        Ok(MaterializedAdmission {
            receipt,
            readmissions,
        })
    }

    fn allocate_kv_for_admission(
        &mut self,
        backend: &mut impl SequenceBackend,
        cells: u32,
        priority: PriorityClass,
        allow_pressure_eviction: bool,
        readmissions: &mut Vec<CoreReadmissionReceipt>,
        sink: &mut impl EventSink,
    ) -> Result<SeqId, SchedError> {
        let mut allocation_error =
            match self.kv.allocate(cells, priority, &mut self.kv_policy, sink) {
                Ok(seq_id) => return Ok(seq_id),
                Err(error) => error,
            };
        if !allow_pressure_eviction
            || priority != PriorityClass::InteractiveBlocking
            || !matches!(allocation_error, SchedError::Oom { .. })
        {
            return Err(allocation_error);
        }

        let candidates = self.kv.suspended_background_candidates();
        for seq_id in candidates {
            if !self
                .active
                .get(&seq_id)
                .is_some_and(|lease| lease.pressure_evictable)
            {
                continue;
            }
            readmissions.push(self.evict_suspended_for_readmission(backend, seq_id, sink)?);
            match self.kv.allocate(cells, priority, &mut self.kv_policy, sink) {
                Ok(seq_id) => return Ok(seq_id),
                Err(error) => allocation_error = error,
            }
            if !matches!(allocation_error, SchedError::Oom { .. }) {
                return Err(allocation_error);
            }
        }
        Err(allocation_error)
    }

    fn evict_suspended_for_readmission(
        &mut self,
        backend: &mut impl SequenceBackend,
        seq_id: SeqId,
        sink: &mut impl EventSink,
    ) -> Result<CoreReadmissionReceipt, SchedError> {
        let (request_id, remaining_prefill_tokens, remaining_decode_steps) = {
            let lease = self
                .active
                .get(&seq_id)
                .ok_or(SchedError::UnknownSequence(seq_id))?;
            let sequence = self
                .batch
                .active()
                .iter()
                .find(|sequence| sequence.seq_id == seq_id)
                .ok_or(SchedError::UnknownSequence(seq_id))?;
            if lease.priority != PriorityClass::Background
                || !lease.pressure_evictable
                || lease.slot_id.is_some()
                || !lease.admission.is_committed_in(&self.admission_ledger)
                || !self.suspended_sequences.contains(&seq_id)
                || sequence.priority != PriorityClass::Background
                || !sequence.preempted
                || sequence.slot_id.is_some()
            {
                return Err(SchedError::InvalidRequest(
                    "pressure readmission requires one fully suspended Background owner",
                ));
            }
            (
                lease.request_id,
                sequence.prefill_tokens_remaining,
                sequence.decode_tokens_remaining,
            )
        };

        self.kv.mark_pressure_evictable(seq_id)?;
        let (batch_index, batch_sequence) = match self.batch.take_for_readmission(seq_id) {
            Ok(staged) => staged,
            Err(error) => {
                self.kv.mark_preempted(seq_id, true)?;
                return Err(error);
            }
        };
        let Some(lease) = self.active.remove(&seq_id) else {
            self.batch
                .restore_failed_readmission(batch_index, batch_sequence);
            self.kv.mark_preempted(seq_id, true)?;
            return Err(SchedError::UnknownSequence(seq_id));
        };
        if !self.suspended_sequences.remove(&seq_id) {
            self.active.insert(seq_id, lease);
            self.batch
                .restore_failed_readmission(batch_index, batch_sequence);
            self.kv.mark_preempted(seq_id, true)?;
            return Err(SchedError::InvalidRequest(
                "pressure readmission lost suspended ownership during staging",
            ));
        }
        let kv = match self.kv.evict_pressure_candidate(backend, seq_id, sink) {
            Ok(receipt) => receipt,
            Err(error) => {
                self.active.insert(seq_id, lease);
                self.batch
                    .restore_failed_readmission(batch_index, batch_sequence);
                self.suspended_sequences.insert(seq_id);
                self.kv.mark_preempted(seq_id, true)?;
                return Err(error);
            }
        };
        let admission = lease.admission.release(&mut self.admission_ledger)?;
        Ok(CoreReadmissionReceipt {
            request_id,
            sequence: kv,
            admission,
            remaining_prefill_tokens,
            remaining_decode_steps,
        })
    }

    fn rollback_unstarted_kv(
        &mut self,
        backend: &mut impl SequenceBackend,
        seq_id: SeqId,
        has_backend_state: bool,
        sink: &mut impl EventSink,
    ) -> Result<(), SchedError> {
        if has_backend_state {
            self.kv.remove(backend, seq_id, sink).map(|_| ())
        } else {
            self.kv.discard_allocation(seq_id, sink).map(|_| ())
        }
    }

    fn refresh_blocking_arrival_signal(&mut self) {
        let physical_slot_full = self
            .slots
            .iter()
            .all(|slot| slot.state() != SlotState::Idle);
        if physical_slot_full
            && self
                .queue
                .contains_priority(PriorityClass::InteractiveBlocking)
        {
            self.batch.notify_interactive_blocking_arrival();
        } else {
            self.batch.clear_interactive_blocking_arrival();
        }
    }

    fn active_principal_counts(&self) -> ActivePrincipalCounts {
        let mut counts = ActivePrincipalCounts::default();
        for lease in self.active.values() {
            counts.add(&lease.admission.request.principal_id);
        }
        counts
    }

    fn resume_one(
        &mut self,
        seq_id: SeqId,
        sink: &mut impl EventSink,
    ) -> Result<CoreResumeReceipt, SchedError> {
        let slot_id = self
            .slots
            .iter()
            .position(|slot| slot.state() == SlotState::Idle)
            .ok_or(SchedError::QuotaFull {
                priority: PriorityClass::Background,
            })?;
        let phase = self
            .batch
            .active()
            .iter()
            .find(|sequence| sequence.seq_id == seq_id)
            .map(|sequence| {
                if sequence.prefill_tokens_remaining > 0 {
                    SlotState::Prefill
                } else {
                    SlotState::Decode
                }
            })
            .ok_or(SchedError::UnknownSequence(seq_id))?;
        if self.kv.sequence_priority(seq_id) != Some(PriorityClass::Background) {
            return Err(SchedError::InvalidRequest(
                "resumed sequence does not own Background KV state",
            ));
        }
        if !self.kv.is_suspended(seq_id) || self.slot_owners[slot_id].is_some() {
            return Err(SchedError::InvalidRequest(
                "resumed sequence KV or physical slot is not suspended and free",
            ));
        }
        self.slots[slot_id].reactivate(phase, sink)?;
        if let Err(error) = self.kv.mark_preempted(seq_id, false) {
            self.slots[slot_id].suspend(sink)?;
            return Err(error);
        }
        if let Err(error) = self.batch.resume_on_slot(seq_id, slot_id) {
            self.kv.mark_preempted(seq_id, true)?;
            self.slots[slot_id].suspend(sink)?;
            return Err(error);
        }
        let lease = self
            .active
            .get_mut(&seq_id)
            .ok_or(SchedError::UnknownSequence(seq_id))?;
        lease.slot_id = Some(slot_id);
        self.slot_owners[slot_id] = Some(seq_id);
        self.suspended_sequences.remove(&seq_id);
        Ok(CoreResumeReceipt { seq_id, slot_id })
    }

    fn cleanup_sequence(
        &mut self,
        backend: &mut impl SequenceBackend,
        receipt: SequenceReleaseReceipt,
        sink: &mut impl EventSink,
    ) -> Result<CoreReleaseReceipt, SchedError> {
        let lease = self
            .active
            .get(&receipt.seq_id)
            .ok_or(SchedError::UnknownSequence(receipt.seq_id))?;
        if lease.slot_id != receipt.slot_id || lease.priority != receipt.priority {
            return Err(SchedError::InvalidRequest(
                "release receipt does not match active lease",
            ));
        }
        let suspended = self.suspended_sequences.contains(&receipt.seq_id);
        let slot_id = lease.slot_id;
        if suspended {
            if slot_id.is_some() || receipt.slot_id.is_some() {
                return Err(SchedError::InvalidRequest(
                    "suspended release unexpectedly owns a physical slot",
                ));
            }
        } else {
            let slot_id = slot_id.ok_or(SchedError::InvalidRequest(
                "active release does not own a physical slot",
            ))?;
            if self.slot_owners[slot_id] != Some(receipt.seq_id) {
                return Err(SchedError::InvalidRequest(
                    "release receipt does not own its physical slot",
                ));
            }
            let slot = &mut self.slots[slot_id];
            if matches!(slot.state(), SlotState::Prefill | SlotState::Decode) {
                slot.transition(SlotState::Draining, sink)?;
            }
        }
        let kv = self.kv.remove(backend, receipt.seq_id, sink)?;
        let admission = lease.admission.release(&mut self.admission_ledger)?;
        if !suspended {
            let slot_id = slot_id.ok_or(SchedError::InvalidRequest(
                "active release lost its physical slot",
            ))?;
            self.slots[slot_id].transition(SlotState::Idle, sink)?;
            self.slot_owners[slot_id] = None;
        }
        self.suspended_sequences.remove(&receipt.seq_id);
        self.active.remove(&receipt.seq_id);
        Ok(CoreReleaseReceipt {
            sequence: receipt,
            admission,
            released_kv_cells: kv.released_cells,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const TEST_OWNER: [u8; 32] = [11; 32];

    #[derive(Default)]
    struct FakeBackend {
        fail_remove: bool,
        fail_copy: bool,
        copies: Vec<(SeqId, SeqId, u32)>,
        states: BTreeMap<SeqId, Vec<u8>>,
        position_max: BTreeMap<SeqId, i32>,
    }

    impl SequenceBackend for FakeBackend {
        fn copy_sequence(
            &mut self,
            source: SeqId,
            destination: SeqId,
            cells: u32,
        ) -> Result<(), SchedError> {
            if self.fail_copy {
                return Err(SchedError::Backend("injected copy failure"));
            }
            self.copies.push((source, destination, cells));
            if let Some(state) = self.states.get(&source).cloned() {
                self.states.insert(destination, state);
            }
            if let Some(position) = self.position_max.get(&source).copied() {
                self.position_max.insert(destination, position);
            }
            Ok(())
        }

        fn remove_sequence(&mut self, seq_id: SeqId) -> Result<(), SchedError> {
            if self.fail_remove {
                Err(SchedError::Backend("injected remove failure"))
            } else {
                self.states.remove(&seq_id);
                self.position_max.remove(&seq_id);
                Ok(())
            }
        }

        fn export_sequence(&mut self, seq_id: SeqId) -> Result<Vec<u8>, SchedError> {
            Ok(self
                .states
                .get(&seq_id)
                .cloned()
                .unwrap_or_else(|| vec![seq_id as u8]))
        }

        fn import_sequence(&mut self, seq_id: SeqId, state: &[u8]) -> Result<(), SchedError> {
            self.states.insert(seq_id, state.to_vec());
            self.position_max.insert(
                seq_id,
                i32::try_from(state.len())
                    .unwrap_or(i32::MAX)
                    .saturating_sub(1),
            );
            Ok(())
        }

        fn sequence_position_max(&mut self, seq_id: SeqId) -> Result<i32, SchedError> {
            self.position_max
                .get(&seq_id)
                .copied()
                .ok_or(SchedError::UnknownSequence(seq_id))
        }
    }

    fn core_at(session_dir: PathBuf) -> SchedulerCore {
        SchedulerCore::new(SchedulerCoreConfig {
            slot_count: 4,
            native_sequence_capacity: 16,
            queue_capacity: 8,
            batch_token_budget: 8,
            preemption_enabled: true,
            kv_capacity_cells: 128,
            kv_bytes_per_cell: 1,
            admission_memory: MemoryAmount::ram(1_024),
            kv_memory: MemoryAmount::ram(1_024),
            session_dir,
            prefix_capacity: 8,
        })
        .unwrap()
    }

    fn private_model_dir(temp: &tempfile::TempDir) -> PathBuf {
        let scheduler = temp.path().join("scheduler");
        crate::store::session::ensure_private_directory(&scheduler).unwrap();
        scheduler.join("model")
    }

    fn core() -> SchedulerCore {
        let temp = tempfile::tempdir().unwrap();
        core_at(temp.keep())
    }

    fn request(id: u64) -> AdmissionRequest {
        AdmissionRequest {
            request_id: id,
            principal_id: "principal-a".to_owned(),
            priority: PriorityClass::Worker,
            prompt_tokens: 2,
            max_tokens: 2,
            decode_steps: 1,
            context_limit: 8,
            kv_bytes: 4,
        }
    }

    #[test]
    fn sched_core_submit_admit_plan_commit_cleanup_is_end_to_end() {
        let mut core = core();
        let mut backend = FakeBackend::default();
        core.submit(request(7), 0).unwrap();
        let admitted = core
            .admit(&mut backend, 1, &mut Vec::new())
            .unwrap()
            .unwrap();
        assert_eq!(admitted.max_output_tokens, 2);
        assert_eq!(admitted.decode_steps, 1);
        assert_eq!(core.snapshot().active.len(), 1);
        let plan = core.plan_step();
        assert_eq!(plan.work[0].phase, crate::sched::SequencePhase::Prefill);
        assert_eq!(plan.work[0].token_count, 2);
        let mut committed = core
            .commit_step(&mut backend, plan, &mut Vec::new())
            .unwrap();
        assert!(committed.released.is_empty());
        let decode = core.plan_step();
        assert_eq!(decode.work[0].phase, crate::sched::SequencePhase::Decode);
        assert_eq!(decode.work[0].token_count, 3);
        committed = core
            .commit_step(&mut backend, decode, &mut Vec::new())
            .unwrap();
        while committed.released.is_empty() {
            let plan = core.plan_step();
            committed = core
                .commit_step(&mut backend, plan, &mut Vec::new())
                .unwrap();
        }
        assert_eq!(committed.released[0].sequence.seq_id, admitted.seq_id);
        assert!(core.snapshot().active.is_empty());
        assert_eq!(core.snapshot().kv_used_cells, 0);
    }

    #[test]
    fn sched_core_session_restart_reprefills_pending_token_and_appended_prompt() {
        let temp = tempfile::tempdir().unwrap();
        let session_dir = private_model_dir(&temp);
        let fingerprint = [9; 32];
        let continuation = SessionContinuation::new(fingerprint, 12, 3).unwrap();
        let mut first = core_at(session_dir.clone());
        let mut first_backend = FakeBackend::default();
        first.submit(request(70), 0).unwrap();
        let admitted = first
            .admit(&mut first_backend, 1, &mut Vec::new())
            .unwrap()
            .unwrap();
        first_backend.states.insert(admitted.seq_id, vec![10, 11]);
        first_backend.position_max.insert(admitted.seq_id, 1);
        first
            .create_session("restartable", fingerprint, TEST_OWNER)
            .unwrap();
        first
            .save_session(
                &mut first_backend,
                "restartable",
                admitted.seq_id,
                continuation,
                TEST_OWNER,
            )
            .unwrap();
        drop(first);

        let mut resumed = core_at(session_dir);
        assert_eq!(
            resumed.session_continuation("restartable").unwrap(),
            continuation
        );
        let mut resumed_backend = FakeBackend::default();
        let resume_request = AdmissionRequest {
            request_id: 71,
            principal_id: "principal-a".to_owned(),
            priority: PriorityClass::Worker,
            prompt_tokens: 5,
            max_tokens: 2,
            decode_steps: 1,
            context_limit: 8,
            kv_bytes: 7,
        };
        assert!(matches!(
            resumed.restore_session(
                &mut resumed_backend,
                "restartable",
                CoreSessionRestoreOptions {
                    request: resume_request.clone(),
                    expected_model_fingerprint: [8; 32],
                    expected_owner_fingerprint: TEST_OWNER,
                    appended_prompt_tokens: 2,
                    now_step: 2,
                },
                &mut Vec::new(),
            ),
            Err(SchedError::InvalidRequest(_))
        ));
        assert!(resumed.snapshot().active.is_empty());

        let restored = resumed
            .restore_session(
                &mut resumed_backend,
                "restartable",
                CoreSessionRestoreOptions {
                    request: resume_request,
                    expected_model_fingerprint: fingerprint,
                    expected_owner_fingerprint: TEST_OWNER,
                    appended_prompt_tokens: 2,
                    now_step: 2,
                },
                &mut Vec::new(),
            )
            .unwrap();
        assert_eq!(restored.continuation_token, 12);
        assert_eq!(restored.continuation_position, 2);
        assert_eq!(restored.appended_prompt_tokens, 2);
        assert_eq!(resumed_backend.position_max[&restored.admission.seq_id], 1);
        let plan = resumed.plan_step();
        assert_eq!(plan.work.len(), 1);
        assert_eq!(plan.work[0].seq_id, restored.admission.seq_id);
        assert_eq!(plan.work[0].phase, crate::sched::SequencePhase::Prefill);
        assert_eq!(plan.work[0].token_count, 3);
    }

    #[test]
    fn sched_core_failed_backend_cleanup_is_retryable_and_slot_drains() {
        let mut core = core();
        let mut backend = FakeBackend::default();
        core.submit(request(8), 0).unwrap();
        let admitted = core
            .admit(&mut backend, 1, &mut Vec::new())
            .unwrap()
            .unwrap();
        backend.fail_remove = true;
        assert!(matches!(
            core.cancel(&mut backend, admitted.seq_id, &mut Vec::new()),
            Err(SchedError::Backend(_))
        ));
        assert_eq!(
            core.snapshot().slots[admitted.slot_id.unwrap()].1,
            SlotState::Draining
        );
        backend.fail_remove = false;
        core.remove(
            &mut backend,
            admitted.seq_id,
            TerminationReason::Cancelled,
            &mut Vec::new(),
        )
        .unwrap();
        assert!(core.snapshot().active.is_empty());
    }

    #[test]
    fn sched_core_eval_deadline_fires_on_exact_injected_step_and_cleans_resources() {
        let mut core = core();
        let mut backend = FakeBackend::default();
        let mut eval = request(8);
        eval.priority = PriorityClass::Eval;
        core.submit(eval, 0).unwrap();
        let admitted = core
            .admit(&mut backend, 0, &mut Vec::new())
            .unwrap()
            .unwrap();
        core.set_eval_deadline(admitted.seq_id, 1).unwrap();

        let before_deadline = core.plan_step();
        assert_eq!(before_deadline.step_index, 0);
        assert!(before_deadline.timeout_receipts.is_empty());
        core.commit_step(&mut backend, before_deadline, &mut Vec::new())
            .unwrap();

        let at_deadline = core.plan_step();
        assert_eq!(at_deadline.step_index, 1);
        assert_eq!(at_deadline.timeout_receipts.len(), 1);
        let receipt = core
            .commit_step(&mut backend, at_deadline, &mut Vec::new())
            .unwrap();
        assert_eq!(receipt.released.len(), 1);
        assert_eq!(
            receipt.released[0].sequence.reason,
            TerminationReason::EvalTimeout
        );
        assert!(core.snapshot().active.is_empty());
        assert_eq!(core.snapshot().kv_used_cells, 0);
    }

    #[test]
    fn sched_core_two_principal_contention_preserves_underrepresented_share() {
        let mut core = core();
        let mut backend = FakeBackend::default();
        for id in 1..=2 {
            core.submit(request(id), 0).unwrap();
            core.admit(&mut backend, id, &mut Vec::new())
                .unwrap()
                .unwrap();
        }
        core.submit(request(3), 2).unwrap();
        let mut second_principal = request(4);
        second_principal.principal_id = "principal-b".to_owned();
        core.submit(second_principal, 2).unwrap();

        assert_eq!(core.next_admissible_request_id(3), Some(4));
        let admitted = core
            .admit(&mut backend, 3, &mut Vec::new())
            .unwrap()
            .unwrap();
        assert_eq!(admitted.request_id, 4);
    }

    #[test]
    fn sched_core_kv_refusal_rolls_admission_back_into_queue() {
        let mut core = core();
        let mut backend = FakeBackend::default();
        let mut oversized = request(10);
        oversized.prompt_tokens = 128;
        oversized.max_tokens = 1;
        oversized.context_limit = 256;
        core.submit(oversized, 0).unwrap();
        let mut events = Vec::new();
        let failure = core
            .admit_identified(&mut backend, 1, &mut events)
            .unwrap_err();
        assert_eq!(failure.request_id, 10);
        assert!(matches!(failure.error, SchedError::Oom { .. }));
        let snapshot = core.snapshot();
        assert_eq!(snapshot.queue_depth, 1);
        assert!(snapshot.active.is_empty());
        assert_eq!(snapshot.kv_used_cells, 0);
        assert!(!events
            .iter()
            .any(|event| matches!(event, crate::sched::SchedEvent::Admission { .. })));
        assert_eq!(core.drop_queued(failure.request_id).unwrap().request_id, 10);
        assert_eq!(core.snapshot().queue_depth, 0);
    }

    #[test]
    fn sched_core_drain_retries_sequence_removed_from_batch_after_cleanup_failure() {
        let mut core = core();
        let mut backend = FakeBackend::default();
        core.submit(request(9), 0).unwrap();
        let admitted = core
            .admit(&mut backend, 1, &mut Vec::new())
            .unwrap()
            .unwrap();
        backend.fail_remove = true;
        assert!(core
            .cancel(&mut backend, admitted.seq_id, &mut Vec::new())
            .is_err());
        backend.fail_remove = false;
        let receipts = core.drain(&mut backend, &mut Vec::new()).unwrap();
        assert_eq!(receipts.len(), 1);
        assert_eq!(receipts[0].sequence.seq_id, admitted.seq_id);
        assert_eq!(receipts[0].sequence.reason, TerminationReason::Drained);
        assert!(core.snapshot().active.is_empty());
    }

    #[test]
    fn sched_core_prefix_matches_longer_prompt_and_releases_pin() {
        let mut core = core();
        let mut backend = FakeBackend::default();
        core.register_prefix("system", vec![1, 2], 2, &mut Vec::new())
            .unwrap();
        core.pin_prefix("system", &mut Vec::new()).unwrap();
        assert!(core
            .match_prefix("system", &[1, 2, 3], &mut Vec::new())
            .unwrap());
        assert!(core
            .unpin_prefix(&mut backend, "system", &mut Vec::new())
            .unwrap());
    }

    #[test]
    fn sched_core_prefix_reuse_copies_native_state_and_skips_prefill_hit() {
        let mut core = core();
        let mut backend = FakeBackend::default();
        core.register_prefix("system", vec![1, 2], 2, &mut Vec::new())
            .unwrap();
        let source = core.pin_prefix("system", &mut Vec::new()).unwrap();
        let reuse = core
            .match_prefix_for_reuse(11, "system", &[1, 2, 3, 4], &mut Vec::new())
            .unwrap()
            .unwrap();
        let mut prefixed = request(11);
        prefixed.prompt_tokens = 4;
        prefixed.context_limit = 8;
        core.submit(prefixed, 0).unwrap();
        let admitted = core
            .admit_with_prefix(&mut backend, 1, Some(reuse), &mut Vec::new())
            .unwrap()
            .unwrap();
        assert_eq!(admitted.prefix_hit_tokens, 2);
        assert_eq!(admitted.max_output_tokens, 2);
        assert_eq!(admitted.decode_steps, 1);
        assert_eq!(backend.copies, vec![(source, admitted.seq_id, 2)]);
        let plan = core.plan_step();
        assert_eq!(plan.work[0].phase, super::super::SequencePhase::Prefill);
        assert_eq!(plan.work[0].token_count, 2);
    }

    #[test]
    fn sched_core_prefix_copy_failure_rolls_back_request_and_destination_kv() {
        let mut core = core();
        let mut backend = FakeBackend::default();
        core.register_prefix("system", vec![1, 2], 2, &mut Vec::new())
            .unwrap();
        core.pin_prefix("system", &mut Vec::new()).unwrap();
        let reuse = core
            .match_prefix_for_reuse(12, "system", &[1, 2, 3], &mut Vec::new())
            .unwrap()
            .unwrap();
        let mut prefixed = request(12);
        prefixed.prompt_tokens = 3;
        prefixed.context_limit = 8;
        core.submit(prefixed, 0).unwrap();
        backend.fail_copy = true;
        let failure = core
            .admit_with_prefix_identified(&mut backend, 1, Some(reuse), &mut Vec::new())
            .unwrap_err();
        assert_eq!(failure.request_id, 12);
        assert!(matches!(failure.error, SchedError::Backend(_)));
        assert_eq!(core.snapshot().queue_depth, 1);
        assert_eq!(core.snapshot().active.len(), 0);
        assert_eq!(core.snapshot().kv_used_cells, 2);
    }

    #[test]
    fn sched_core_early_termination_cleans_once_and_retry_survives_backend_failure() {
        let mut core = core();
        let mut backend = FakeBackend::default();
        core.submit(request(13), 0).unwrap();
        let admitted = core
            .admit(&mut backend, 1, &mut Vec::new())
            .unwrap()
            .unwrap();
        let before = core.plan_step();
        assert_eq!(before, core.plan_step());
        backend.fail_remove = true;
        assert!(matches!(
            core.terminate(
                &mut backend,
                admitted.seq_id,
                TerminationReason::BackendFailure,
                &mut Vec::new(),
            ),
            Err(SchedError::Backend(_))
        ));
        assert_eq!(core.snapshot().active.len(), 1);
        backend.fail_remove = false;
        let released = core
            .terminate(
                &mut backend,
                admitted.seq_id,
                TerminationReason::BackendFailure,
                &mut Vec::new(),
            )
            .unwrap();
        assert_eq!(released.sequence.reason, TerminationReason::BackendFailure);
        assert!(core.snapshot().active.is_empty());
        assert_eq!(core.snapshot().kv_used_cells, 0);
        assert!(matches!(
            core.terminate(
                &mut backend,
                admitted.seq_id,
                TerminationReason::BackendFailure,
                &mut Vec::new(),
            ),
            Err(SchedError::UnknownSequence(_))
        ));
    }

    #[test]
    fn sched_core_dropping_blocking_request_cancels_preemption_signal() {
        let mut core = core();
        let mut backend = FakeBackend::default();
        let mut background = request(14);
        background.priority = PriorityClass::Background;
        core.submit(background, 0).unwrap();
        core.admit(&mut backend, 1, &mut Vec::new())
            .unwrap()
            .unwrap();
        let mut blocking = request(15);
        blocking.priority = PriorityClass::InteractiveBlocking;
        core.submit(blocking, 2).unwrap();
        core.drop_queued(15).unwrap();
        assert!(core.plan_step().preempted_sequence_ids.is_empty());
    }

    #[test]
    fn sched_core_free_slot_admits_blocker_without_preempting_background() {
        let mut core = core();
        let mut backend = FakeBackend::default();
        let mut events = Vec::new();
        let mut background = request(16);
        background.priority = PriorityClass::Background;
        core.submit(background, 0).unwrap();
        let background = core.admit(&mut backend, 1, &mut events).unwrap().unwrap();

        let mut blocking = request(17);
        blocking.priority = PriorityClass::InteractiveBlocking;
        core.submit(blocking, 2).unwrap();
        assert!(core.plan_step().preempted_sequence_ids.is_empty());
        let blocking = core.admit(&mut backend, 3, &mut events).unwrap().unwrap();
        let plan = core.plan_step();
        assert!(plan.preempted_sequence_ids.is_empty());
        assert!(plan
            .work
            .iter()
            .any(|work| work.seq_id == background.seq_id));
        assert!(plan.work.iter().any(|work| work.seq_id == blocking.seq_id));
    }

    #[test]
    fn sched_core_cancel_suspended_sequence_preserves_reassigned_slot_owner() {
        let session_root = tempfile::tempdir().unwrap();
        let session_dir = private_model_dir(&session_root);
        let mut core = SchedulerCore::new(SchedulerCoreConfig {
            slot_count: 1,
            native_sequence_capacity: 8,
            queue_capacity: 4,
            batch_token_budget: 4,
            preemption_enabled: true,
            kv_capacity_cells: 32,
            kv_bytes_per_cell: 1,
            admission_memory: MemoryAmount::ram(128),
            kv_memory: MemoryAmount::ram(128),
            session_dir: session_dir.clone(),
            prefix_capacity: 2,
        })
        .unwrap();
        let mut backend = FakeBackend::default();
        let mut events = Vec::new();
        let mut background = request(18);
        background.priority = PriorityClass::Background;
        core.submit(background, 0).unwrap();
        let background = core.admit(&mut backend, 1, &mut events).unwrap().unwrap();

        let mut blocking = request(19);
        blocking.priority = PriorityClass::InteractiveBlocking;
        core.submit(blocking, 2).unwrap();
        let suspend = core.plan_step();
        let suspended = core
            .commit_step(&mut backend, suspend, &mut events)
            .unwrap();
        assert_eq!(
            suspended.suspended,
            vec![CoreSuspensionReceipt {
                seq_id: background.seq_id,
                released_slot_id: 0
            }]
        );
        let blocking = core.admit(&mut backend, 3, &mut events).unwrap().unwrap();
        assert_eq!(core.slot_owners[0], Some(blocking.seq_id));

        backend.fail_remove = true;
        assert_eq!(
            core.cancel(&mut backend, background.seq_id, &mut events),
            Err(SchedError::Backend("injected remove failure"))
        );
        assert_eq!(core.slot_owners[0], Some(blocking.seq_id));
        assert_eq!(core.slots[0].state(), SlotState::Prefill);
        assert!(core.suspended_sequences.contains(&background.seq_id));
        backend.fail_remove = false;
        core.remove(
            &mut backend,
            background.seq_id,
            TerminationReason::Cancelled,
            &mut events,
        )
        .unwrap();
        assert_eq!(core.slot_owners[0], Some(blocking.seq_id));
        assert!(core
            .snapshot()
            .active
            .iter()
            .any(|receipt| receipt.seq_id == blocking.seq_id));
    }

    #[test]
    fn sched_core_blocking_preemption_reassigns_slot_and_resumes_exact_progress() {
        let session_dir = tempfile::tempdir().unwrap();
        let mut core = SchedulerCore::new(SchedulerCoreConfig {
            slot_count: 1,
            native_sequence_capacity: 8,
            queue_capacity: 4,
            batch_token_budget: 4,
            preemption_enabled: true,
            kv_capacity_cells: 32,
            kv_bytes_per_cell: 1,
            admission_memory: MemoryAmount::ram(128),
            kv_memory: MemoryAmount::ram(128),
            session_dir: session_dir.path().to_owned(),
            prefix_capacity: 2,
        })
        .unwrap();
        let mut backend = FakeBackend::default();
        let mut events = Vec::new();

        let mut background = request(20);
        background.priority = PriorityClass::Background;
        background.max_tokens = 4;
        background.decode_steps = 3;
        background.kv_bytes = 6;
        core.submit(background, 0).unwrap();
        let background = core.admit(&mut backend, 1, &mut events).unwrap().unwrap();
        let prefill = core.plan_step();
        core.commit_step(&mut backend, prefill, &mut events)
            .unwrap();
        assert_eq!(
            core.batch
                .active()
                .iter()
                .find(|sequence| sequence.seq_id == background.seq_id)
                .unwrap()
                .decode_tokens_remaining,
            3
        );

        let mut blocking = request(21);
        blocking.priority = PriorityClass::InteractiveBlocking;
        blocking.prompt_tokens = 1;
        blocking.max_tokens = 1;
        blocking.decode_steps = 0;
        blocking.kv_bytes = 2;
        core.submit(blocking, 2).unwrap();
        let suspend = core.plan_step();
        assert_eq!(suspend.preempted_sequence_ids, vec![background.seq_id]);
        assert!(suspend.work.is_empty());
        core.commit_step(&mut backend, suspend, &mut events)
            .unwrap();
        assert_eq!(core.slots[0].state(), SlotState::Idle);

        let blocking = core.admit(&mut backend, 3, &mut events).unwrap().unwrap();
        assert_eq!(blocking.slot_id, Some(0));
        assert!(core.plan_step().preempted_sequence_ids.is_empty());
        let blocking_step = core.plan_step();
        let committed = core
            .commit_step(&mut backend, blocking_step, &mut events)
            .unwrap();
        assert_eq!(committed.released[0].sequence.seq_id, blocking.seq_id);
        assert_eq!(core.slots[0].state(), SlotState::Idle);
        assert_eq!(
            core.resume_next(&mut events).unwrap(),
            Some(CoreResumeReceipt {
                seq_id: background.seq_id,
                slot_id: 0
            })
        );
        assert_eq!(core.slots[0].state(), SlotState::Decode);

        let mut resumed_output_tokens = 0;
        loop {
            let plan = core.plan_step();
            resumed_output_tokens += plan
                .work
                .iter()
                .filter(|work| work.seq_id == background.seq_id)
                .map(|_| 1)
                .sum::<u32>();
            let committed = core.commit_step(&mut backend, plan, &mut events).unwrap();
            if committed
                .released
                .iter()
                .any(|release| release.sequence.seq_id == background.seq_id)
            {
                break;
            }
        }
        assert_eq!(resumed_output_tokens, 3);
        assert!(core.snapshot().active.is_empty());
        assert_eq!(core.snapshot().kv_used_cells, 0);
    }

    #[test]
    fn sched_core_kv_pressure_explicitly_readmits_only_suspended_background() {
        let session_dir = tempfile::tempdir().unwrap();
        let mut core = SchedulerCore::new(SchedulerCoreConfig {
            slot_count: 1,
            native_sequence_capacity: 4,
            queue_capacity: 4,
            batch_token_budget: 4,
            preemption_enabled: true,
            kv_capacity_cells: 8,
            kv_bytes_per_cell: 1,
            admission_memory: MemoryAmount::ram(64),
            kv_memory: MemoryAmount::ram(64),
            session_dir: session_dir.path().to_owned(),
            prefix_capacity: 2,
        })
        .unwrap();
        let mut backend = FakeBackend::default();
        let mut events = Vec::new();

        let mut background = request(22);
        background.priority = PriorityClass::Background;
        background.max_tokens = 4;
        background.decode_steps = 3;
        background.kv_bytes = 6;
        core.submit(background, 0).unwrap();
        let background = core.admit(&mut backend, 1, &mut events).unwrap().unwrap();

        let mut blocking = request(23);
        blocking.priority = PriorityClass::InteractiveBlocking;
        blocking.max_tokens = 4;
        blocking.decode_steps = 3;
        blocking.kv_bytes = 6;
        core.submit(blocking, 2).unwrap();
        let suspension = core.plan_step();
        core.commit_step(&mut backend, suspension, &mut events)
            .unwrap();

        let ordinary = core
            .admit_identified(&mut backend, 3, &mut events)
            .unwrap_err();
        assert!(matches!(ordinary.error, SchedError::Oom { .. }));
        assert!(ordinary.readmissions.is_empty());
        assert_eq!(core.snapshot().kv_used_cells, 6);
        assert_eq!(core.kv.background_evicted(), 0);

        let outcome = core
            .admit_with_prefix_under_pressure_identified(&mut backend, 4, None, &mut events)
            .unwrap();
        let admitted = outcome.admitted.unwrap();
        assert_eq!(admitted.request_id, 23);
        assert_eq!(outcome.readmissions.len(), 1);
        let readmission = &outcome.readmissions[0];
        assert_eq!(readmission.request_id, 22);
        assert_eq!(readmission.sequence.seq_id, background.seq_id);
        assert_eq!(readmission.sequence.evicted_cells, 6);
        assert_eq!(
            readmission.sequence.reason,
            crate::sched::ReadmissionReason::KvPressure
        );
        assert_eq!(readmission.admission.request_id, 22);
        assert_eq!(readmission.admission.released_kv_bytes, 6);
        assert_eq!(readmission.remaining_prefill_tokens, 2);
        assert_eq!(readmission.remaining_decode_steps, 3);
        assert_eq!(core.kv.background_evicted(), 1);
        assert_eq!(core.snapshot().kv_used_cells, 6);
        assert_eq!(core.snapshot().active.len(), 1);
        assert_eq!(core.snapshot().active[0].request_id, 23);
        assert!(events.iter().any(|event| matches!(
            event,
            crate::sched::SchedEvent::BackgroundEvicted { seq_id }
                if *seq_id == background.seq_id
        )));
    }

    #[test]
    fn sched_core_failed_pressure_eviction_is_retryable_without_partial_readmission() {
        let session_dir = tempfile::tempdir().unwrap();
        let mut core = SchedulerCore::new(SchedulerCoreConfig {
            slot_count: 1,
            native_sequence_capacity: 4,
            queue_capacity: 4,
            batch_token_budget: 4,
            preemption_enabled: true,
            kv_capacity_cells: 8,
            kv_bytes_per_cell: 1,
            admission_memory: MemoryAmount::ram(64),
            kv_memory: MemoryAmount::ram(64),
            session_dir: session_dir.path().to_owned(),
            prefix_capacity: 2,
        })
        .unwrap();
        let mut backend = FakeBackend::default();
        let mut events = Vec::new();

        let mut background = request(24);
        background.priority = PriorityClass::Background;
        background.max_tokens = 4;
        background.decode_steps = 3;
        background.kv_bytes = 6;
        core.submit(background, 0).unwrap();
        let background = core.admit(&mut backend, 1, &mut events).unwrap().unwrap();
        let mut blocking = request(25);
        blocking.priority = PriorityClass::InteractiveBlocking;
        blocking.max_tokens = 4;
        blocking.decode_steps = 3;
        blocking.kv_bytes = 6;
        core.submit(blocking, 2).unwrap();
        let suspension = core.plan_step();
        core.commit_step(&mut backend, suspension, &mut events)
            .unwrap();
        let batch_before = core.batch.active().to_vec();
        let suspended_before = core.suspended_sequences.clone();
        let admission_available_before = core.admission_ledger.available();
        let kv_available_before = core.kv.ledger_mut().available();

        backend.fail_remove = true;
        let failure = core
            .admit_with_prefix_under_pressure_identified(&mut backend, 3, None, &mut events)
            .unwrap_err();
        assert_eq!(
            failure.error,
            SchedError::Backend("injected remove failure")
        );
        assert!(failure.readmissions.is_empty());
        assert_eq!(core.snapshot().queue_depth, 1);
        assert_eq!(core.snapshot().kv_used_cells, 6);
        assert_eq!(core.snapshot().active[0].seq_id, background.seq_id);
        assert_eq!(core.snapshot().active[0].slot_id, None);
        assert_eq!(core.batch.active(), batch_before);
        assert_eq!(core.suspended_sequences, suspended_before);
        assert_eq!(
            core.admission_ledger.available(),
            admission_available_before
        );
        assert_eq!(core.kv.ledger_mut().available(), kv_available_before);
        assert_eq!(core.kv.background_evicted(), 0);
        assert!(!events
            .iter()
            .any(|event| matches!(event, crate::sched::SchedEvent::BackgroundEvicted { .. })));

        backend.fail_remove = false;
        let outcome = core
            .admit_with_prefix_under_pressure_identified(&mut backend, 4, None, &mut events)
            .unwrap();
        assert_eq!(outcome.readmissions.len(), 1);
        assert_eq!(outcome.readmissions[0].request_id, 24);
        assert_eq!(outcome.admitted.unwrap().request_id, 25);
        assert_eq!(core.kv.background_evicted(), 1);
    }

    #[test]
    fn sched_core_pressure_failure_after_eviction_returns_readmission_receipt() {
        let session_dir = tempfile::tempdir().unwrap();
        let mut core = SchedulerCore::new(SchedulerCoreConfig {
            slot_count: 1,
            native_sequence_capacity: 4,
            queue_capacity: 4,
            batch_token_budget: 4,
            preemption_enabled: true,
            kv_capacity_cells: 8,
            kv_bytes_per_cell: 1,
            admission_memory: MemoryAmount::ram(64),
            kv_memory: MemoryAmount::ram(64),
            session_dir: session_dir.path().to_owned(),
            prefix_capacity: 2,
        })
        .unwrap();
        let mut backend = FakeBackend::default();
        let mut events = Vec::new();

        core.register_prefix("system", vec![1, 2], 2, &mut events)
            .unwrap();
        core.pin_prefix("system", &mut events).unwrap();

        let mut background = request(26);
        background.priority = PriorityClass::Background;
        background.max_tokens = 4;
        background.decode_steps = 3;
        background.kv_bytes = 6;
        core.submit(background, 0).unwrap();
        let background = core.admit(&mut backend, 1, &mut events).unwrap().unwrap();

        let reuse = core
            .match_prefix_for_reuse(27, "system", &[1, 2, 3], &mut events)
            .unwrap()
            .unwrap();
        let mut blocking = request(27);
        blocking.priority = PriorityClass::InteractiveBlocking;
        blocking.prompt_tokens = 3;
        blocking.max_tokens = 2;
        blocking.decode_steps = 1;
        blocking.kv_bytes = 5;
        core.submit(blocking, 2).unwrap();
        let suspension = core.plan_step();
        core.commit_step(&mut backend, suspension, &mut events)
            .unwrap();

        backend.fail_copy = true;
        let failure = core
            .admit_with_prefix_under_pressure_identified(&mut backend, 3, Some(reuse), &mut events)
            .unwrap_err();

        assert_eq!(failure.request_id, 27);
        assert_eq!(failure.error, SchedError::Backend("injected copy failure"));
        assert_eq!(failure.readmissions.len(), 1);
        assert_eq!(failure.readmissions[0].request_id, 26);
        assert_eq!(failure.readmissions[0].sequence.seq_id, background.seq_id);
        assert_eq!(
            failure.readmissions[0].sequence.reason,
            crate::sched::ReadmissionReason::KvPressure
        );
        assert_eq!(core.snapshot().queue_depth, 1);
        assert!(core.snapshot().active.is_empty());
        assert_eq!(core.snapshot().kv_used_cells, 2);
        assert_eq!(core.kv.background_evicted(), 1);
        assert!(events.iter().any(|event| matches!(
            event,
            crate::sched::SchedEvent::BackgroundEvicted { seq_id }
                if *seq_id == background.seq_id
        )));
    }

    #[test]
    fn sched_core_restored_background_is_never_pressure_readmitted() {
        let session_root = tempfile::tempdir().unwrap();
        let session_dir = private_model_dir(&session_root);
        let fingerprint = [7; 32];
        let config = || SchedulerCoreConfig {
            slot_count: 1,
            native_sequence_capacity: 4,
            queue_capacity: 4,
            batch_token_budget: 4,
            preemption_enabled: true,
            kv_capacity_cells: 8,
            kv_bytes_per_cell: 1,
            admission_memory: MemoryAmount::ram(64),
            kv_memory: MemoryAmount::ram(64),
            session_dir: session_dir.clone(),
            prefix_capacity: 2,
        };

        let mut initial = SchedulerCore::new(config()).unwrap();
        let mut initial_backend = FakeBackend::default();
        let mut background = request(28);
        background.priority = PriorityClass::Background;
        background.max_tokens = 4;
        background.decode_steps = 3;
        background.kv_bytes = 6;
        initial.submit(background.clone(), 0).unwrap();
        let admitted = initial
            .admit(&mut initial_backend, 1, &mut Vec::new())
            .unwrap()
            .unwrap();
        initial_backend.states.insert(admitted.seq_id, vec![10]);
        initial_backend.position_max.insert(admitted.seq_id, 0);
        initial
            .create_session("protected-background", fingerprint, TEST_OWNER)
            .unwrap();
        initial
            .save_session(
                &mut initial_backend,
                "protected-background",
                admitted.seq_id,
                SessionContinuation::new(fingerprint, 10, 2).unwrap(),
                TEST_OWNER,
            )
            .unwrap();
        drop(initial);

        let mut core = SchedulerCore::new(config()).unwrap();
        let mut backend = FakeBackend::default();
        let mut events = Vec::new();
        let restored = core
            .restore_session(
                &mut backend,
                "protected-background",
                CoreSessionRestoreOptions {
                    request: background,
                    expected_model_fingerprint: fingerprint,
                    expected_owner_fingerprint: TEST_OWNER,
                    appended_prompt_tokens: 0,
                    now_step: 2,
                },
                &mut events,
            )
            .unwrap();

        let mut blocking = request(29);
        blocking.priority = PriorityClass::InteractiveBlocking;
        blocking.max_tokens = 4;
        blocking.decode_steps = 3;
        blocking.kv_bytes = 6;
        core.submit(blocking, 3).unwrap();
        let suspension = core.plan_step();
        core.commit_step(&mut backend, suspension, &mut events)
            .unwrap();

        let failure = core
            .admit_with_prefix_under_pressure_identified(&mut backend, 4, None, &mut events)
            .unwrap_err();

        assert_eq!(failure.request_id, 29);
        assert!(matches!(failure.error, SchedError::Oom { .. }));
        assert!(failure.readmissions.is_empty());
        assert_eq!(core.snapshot().queue_depth, 1);
        assert_eq!(core.snapshot().active.len(), 1);
        assert_eq!(core.snapshot().active[0].seq_id, restored.admission.seq_id);
        assert_eq!(core.snapshot().active[0].slot_id, None);
        assert_eq!(core.snapshot().kv_used_cells, 6);
        assert_eq!(core.kv.background_evicted(), 0);
        assert!(backend.states.contains_key(&restored.admission.seq_id));
        assert!(!events
            .iter()
            .any(|event| matches!(event, crate::sched::SchedEvent::BackgroundEvicted { .. })));
    }

    #[test]
    fn sched_core_scratch_sequence_is_collision_free_and_released_once() {
        let mut core = core();
        let mut backend = FakeBackend::default();
        core.submit(request(16), 0).unwrap();
        let active = core
            .admit(&mut backend, 1, &mut Vec::new())
            .unwrap()
            .unwrap();
        let scratch = core
            .reserve_scratch_sequence(3, PriorityClass::Worker, &mut Vec::new())
            .unwrap();
        assert_ne!(scratch.seq_id, active.seq_id);
        assert_eq!(scratch.cells, 3);
        let released = core
            .release_scratch_sequence(&mut backend, scratch.seq_id, &mut Vec::new())
            .unwrap();
        assert_eq!(released, scratch);
        assert_eq!(
            core.release_scratch_sequence(&mut backend, scratch.seq_id, &mut Vec::new()),
            Err(SchedError::UnknownSequence(scratch.seq_id))
        );

        let empty = core
            .reserve_scratch_sequence(2, PriorityClass::Worker, &mut Vec::new())
            .unwrap();
        backend.position_max.insert(empty.seq_id, -1);
        assert_eq!(
            core.release_empty_scratch_sequence(&mut backend, empty.seq_id, &mut Vec::new())
                .unwrap(),
            empty
        );
    }
}
