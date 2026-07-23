//! Deterministic prefill/decode planning with transactional commit.
//!
//! Seed plumbing is request-local and does not participate in scheduling order.
//! Given identical admissions, token counts, deadlines, and seeds, plans are
//! stable. Backend kernels, device-specific reductions, and native sampler
//! implementations are the explicit bit-exact break points.

use std::collections::{BTreeMap, BTreeSet};

use super::{
    queue::PolicyGroup, ActiveClassCounts, EventSink, PriorityClass, PriorityPolicy, SchedError,
    SchedEvent,
};

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ActiveSequence {
    pub seq_id: u32,
    pub slot_id: Option<usize>,
    pub priority: PriorityClass,
    pub prefill_tokens_remaining: u32,
    pub decode_tokens_remaining: u32,
    /// Injected step index at which Eval fails closed.
    pub deadline_step: Option<u64>,
    pub preempted: bool,
}

#[derive(Clone, Copy, Debug, Eq, Ord, PartialEq, PartialOrd)]
pub enum SequencePhase {
    Prefill,
    Decode,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum TerminationReason {
    Completed,
    EndOfGeneration,
    StopSequence,
    Deadline,
    EvalTimeout,
    Cancelled,
    Drained,
    BackendFailure,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct SequenceReleaseReceipt {
    pub seq_id: u32,
    pub slot_id: Option<usize>,
    pub priority: PriorityClass,
    pub reason: TerminationReason,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct SequenceWork {
    pub seq_id: u32,
    pub phase: SequencePhase,
    /// Immutable compute-token ceiling granted for this scheduler step.
    pub token_count: u32,
}

/// Actual decode work committed for one sequence against an immutable grant.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct DecodeProgress {
    pub seq_id: u32,
    pub compute_tokens: u32,
    pub output_tokens: u32,
}

/// Immutable backend work proposal. State changes only through `commit_step`.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct BatchPlan {
    pub step_index: u64,
    pub work: Vec<SequenceWork>,
    pub preempted_sequence_ids: Vec<u32>,
    pub timeout_receipts: Vec<SequenceReleaseReceipt>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct BatchStep {
    pub step_index: u64,
    pub token_count: u32,
    pub sequence_ids: Vec<u32>,
    pub preempted_sequence_ids: Vec<u32>,
    pub release_receipts: Vec<SequenceReleaseReceipt>,
}

#[derive(Debug)]
pub struct BatchLoop {
    policy: PriorityPolicy,
    preemption_enabled: bool,
    step_index: u64,
    interactive_blocking_waiting: bool,
    sequences: Vec<ActiveSequence>,
}

impl BatchLoop {
    pub fn new(policy: PriorityPolicy, preemption_enabled: bool) -> Self {
        Self {
            policy,
            preemption_enabled,
            step_index: 0,
            interactive_blocking_waiting: false,
            sequences: Vec::new(),
        }
    }

    pub fn add_sequence(&mut self, sequence: ActiveSequence) -> Result<(), SchedError> {
        if sequence.prefill_tokens_remaining == 0 && sequence.decode_tokens_remaining == 0 {
            return Err(SchedError::InvalidRequest(
                "sequence must contain at least one token",
            ));
        }
        if !sequence.preempted && sequence.slot_id.is_none() {
            return Err(SchedError::InvalidRequest(
                "active sequence must own a physical slot",
            ));
        }
        if self.sequences.iter().any(|active| {
            active.seq_id == sequence.seq_id
                || (!active.preempted && active.slot_id == sequence.slot_id)
        }) {
            return Err(SchedError::InvalidRequest("duplicate sequence or slot id"));
        }
        let active = self.active_counts();
        if !active.can_admit(self.policy, sequence.priority) {
            return Err(SchedError::QuotaFull {
                priority: sequence.priority,
            });
        }
        self.sequences.push(sequence);
        Ok(())
    }

    pub fn notify_interactive_blocking_arrival(&mut self) {
        self.interactive_blocking_waiting = true;
    }

    pub(crate) fn clear_interactive_blocking_arrival(&mut self) {
        self.interactive_blocking_waiting = false;
    }

    pub fn active(&self) -> &[ActiveSequence] {
        &self.sequences
    }

    pub fn active_counts(&self) -> ActiveClassCounts {
        let mut counts = ActiveClassCounts::default();
        for sequence in self.sequences.iter().filter(|sequence| !sequence.preempted) {
            counts.add(sequence.priority);
        }
        counts
    }

    /// Plans one step without consuming tokens or changing preemption state.
    pub fn plan_step(&self) -> BatchPlan {
        let timeout_receipts: Vec<_> = self
            .sequences
            .iter()
            .filter(|sequence| {
                sequence.priority == PriorityClass::Eval
                    && sequence
                        .deadline_step
                        .is_some_and(|deadline| self.step_index >= deadline)
            })
            .map(|sequence| release_receipt(sequence, TerminationReason::EvalTimeout))
            .collect();
        if !timeout_receipts.is_empty() {
            return BatchPlan {
                step_index: self.step_index,
                work: Vec::new(),
                preempted_sequence_ids: Vec::new(),
                timeout_receipts,
            };
        }

        let preempted_sequence_ids: Vec<_> =
            if self.preemption_enabled && self.interactive_blocking_waiting {
                self.sequences
                    .iter()
                    .filter(|sequence| {
                        sequence.priority == PriorityClass::Background && !sequence.preempted
                    })
                    .take(1)
                    .map(|sequence| sequence.seq_id)
                    .collect()
            } else {
                Vec::new()
            };
        let virtually_preempted: BTreeSet<_> = preempted_sequence_ids.iter().copied().collect();

        let eval_indices: Vec<_> = self
            .sequences
            .iter()
            .enumerate()
            .filter_map(|(index, sequence)| {
                (sequence.priority == PriorityClass::Eval
                    && !sequence.preempted
                    && !virtually_preempted.contains(&sequence.seq_id))
                .then_some(index)
            })
            .collect();
        let mut work = BTreeMap::new();
        if !eval_indices.is_empty() {
            allocate_fair(
                &self.sequences,
                &eval_indices,
                self.policy.batch_token_budget(),
                &mut work,
            );
        } else {
            let interactive =
                self.indices_for_group(PolicyGroup::Interactive, &virtually_preempted);
            let worker = self.indices_for_group(PolicyGroup::Worker, &virtually_preempted);
            let background = self.indices_for_group(PolicyGroup::Background, &virtually_preempted);
            let total = self.policy.batch_token_budget();
            let interactive_budget = share_ceil(
                total,
                self.policy.initial_batch_share(PolicyGroup::Interactive),
            );
            let worker_base =
                share_ceil(total, self.policy.initial_batch_share(PolicyGroup::Worker))
                    .min(total.saturating_sub(interactive_budget));
            let background_base = total
                .saturating_sub(interactive_budget)
                .saturating_sub(worker_base);
            let interactive_used =
                allocate_fair(&self.sequences, &interactive, interactive_budget, &mut work);
            let worker_budget = worker_base.saturating_add(interactive_budget - interactive_used);
            let worker_used = allocate_fair(&self.sequences, &worker, worker_budget, &mut work);
            let background_budget = background_base.saturating_add(worker_budget - worker_used);
            allocate_fair(&self.sequences, &background, background_budget, &mut work);
        }
        BatchPlan {
            step_index: self.step_index,
            work: work
                .into_iter()
                .map(|(seq_id, (phase, token_count))| SequenceWork {
                    seq_id,
                    phase,
                    token_count,
                })
                .collect(),
            preempted_sequence_ids,
            timeout_receipts,
        }
    }

    /// Commits a successfully executed plan and returns all cleanup receipts.
    pub fn commit_step(
        &mut self,
        plan: BatchPlan,
        decode_progress: &[DecodeProgress],
        queue_depth: usize,
        sink: &mut impl EventSink,
    ) -> Result<BatchStep, SchedError> {
        if plan.step_index != self.step_index {
            return Err(SchedError::StalePlan {
                expected: self.step_index,
                actual: plan.step_index,
            });
        }
        if plan != self.plan_step() {
            return Err(SchedError::InvalidRequest(
                "batch plan no longer matches scheduler state",
            ));
        }
        let decode_by_sequence =
            decode_progress
                .iter()
                .try_fold(BTreeMap::new(), |mut by_sequence, progress| {
                    if by_sequence.insert(progress.seq_id, *progress).is_some() {
                        return Err(SchedError::InvalidRequest(
                            "decode progress contains a duplicate sequence",
                        ));
                    }
                    Ok(by_sequence)
                })?;
        let planned_decode_ids = plan
            .work
            .iter()
            .filter(|work| work.phase == SequencePhase::Decode)
            .map(|work| work.seq_id)
            .collect::<BTreeSet<_>>();
        if planned_decode_ids != decode_by_sequence.keys().copied().collect() {
            return Err(SchedError::InvalidRequest(
                "decode progress does not exactly cover the immutable plan",
            ));
        }
        for seq_id in &plan.preempted_sequence_ids {
            let sequence = self
                .sequences
                .iter()
                .find(|sequence| sequence.seq_id == *seq_id)
                .ok_or(SchedError::UnknownSequence(*seq_id))?;
            if sequence.priority != PriorityClass::Background {
                return Err(SchedError::InvalidRequest(
                    "only Background sequences may be preempted",
                ));
            }
        }
        for item in &plan.work {
            let sequence = self
                .sequences
                .iter()
                .find(|sequence| sequence.seq_id == item.seq_id)
                .ok_or(SchedError::UnknownSequence(item.seq_id))?;
            if sequence.preempted
                || (item.phase == SequencePhase::Decode && sequence.prefill_tokens_remaining > 0)
            {
                return Err(SchedError::InvalidRequest(
                    "batch plan no longer matches sequence",
                ));
            }
            match item.phase {
                SequencePhase::Prefill if item.token_count > sequence.prefill_tokens_remaining => {
                    return Err(SchedError::InvalidRequest(
                        "batch plan no longer matches sequence",
                    ));
                }
                SequencePhase::Decode => {
                    let progress = decode_by_sequence
                        .get(&item.seq_id)
                        .expect("decode progress coverage was validated");
                    if progress.compute_tokens == 0
                        || progress.compute_tokens > item.token_count
                        || progress.output_tokens > sequence.decode_tokens_remaining
                        || progress.output_tokens > progress.compute_tokens
                    {
                        return Err(SchedError::InvalidRequest(
                            "decode progress exceeds its compute or output grant",
                        ));
                    }
                }
                SequencePhase::Prefill => {}
            }
        }
        let timeout_ids: BTreeSet<_> = plan
            .timeout_receipts
            .iter()
            .map(|receipt| receipt.seq_id)
            .collect();
        for seq_id in &plan.preempted_sequence_ids {
            let sequence = self
                .sequences
                .iter_mut()
                .find(|sequence| sequence.seq_id == *seq_id)
                .expect("preemption ownership was validated");
            sequence.preempted = true;
            sequence.slot_id = None;
        }
        let mut token_count = 0_u32;
        let mut sequence_ids = Vec::new();
        for item in &plan.work {
            let sequence = self
                .sequences
                .iter_mut()
                .find(|sequence| sequence.seq_id == item.seq_id)
                .expect("planned sequence ownership was validated");
            match item.phase {
                SequencePhase::Prefill => {
                    sequence.prefill_tokens_remaining -= item.token_count;
                    token_count = token_count.saturating_add(item.token_count);
                    if item.token_count > 0 {
                        sequence_ids.push(item.seq_id);
                    }
                }
                SequencePhase::Decode => {
                    let progress = decode_by_sequence
                        .get(&item.seq_id)
                        .expect("decode progress coverage was validated");
                    sequence.decode_tokens_remaining -= progress.output_tokens;
                    token_count = token_count.saturating_add(progress.compute_tokens);
                    sequence_ids.push(item.seq_id);
                }
            }
        }
        let mut release_receipts = plan.timeout_receipts;
        release_receipts.extend(
            self.sequences
                .iter()
                .filter(|sequence| {
                    sequence.prefill_tokens_remaining == 0
                        && sequence.decode_tokens_remaining == 0
                        && !timeout_ids.contains(&sequence.seq_id)
                })
                .map(|sequence| release_receipt(sequence, TerminationReason::Completed)),
        );
        let release_ids: BTreeSet<_> = release_receipts
            .iter()
            .map(|receipt| receipt.seq_id)
            .collect();
        self.sequences
            .retain(|sequence| !release_ids.contains(&sequence.seq_id));
        let eval_slot = self
            .sequences
            .iter()
            .find(|sequence| sequence.priority == PriorityClass::Eval && !sequence.preempted)
            .and_then(|sequence| sequence.slot_id);
        let slots_busy = self
            .sequences
            .iter()
            .filter(|sequence| !sequence.preempted)
            .count();
        sink.emit(SchedEvent::BatchStep {
            eval_slot,
            slots_busy,
            queue_depth,
        });
        let result = BatchStep {
            step_index: self.step_index,
            token_count,
            sequence_ids,
            preempted_sequence_ids: plan.preempted_sequence_ids,
            release_receipts,
        };
        self.step_index = self.step_index.saturating_add(1);
        Ok(result)
    }

    /// Convenience path for deterministic tests with no fallible backend work.
    pub fn step(
        &mut self,
        queue_depth: usize,
        sink: &mut impl EventSink,
    ) -> Result<BatchStep, SchedError> {
        let plan = self.plan_step();
        let progress = normal_decode_progress(&plan);
        self.commit_step(plan, &progress, queue_depth, sink)
    }

    pub fn cancel(&mut self, seq_id: u32) -> Result<SequenceReleaseReceipt, SchedError> {
        self.terminate(seq_id, TerminationReason::Cancelled)
    }

    pub fn terminate(
        &mut self,
        seq_id: u32,
        reason: TerminationReason,
    ) -> Result<SequenceReleaseReceipt, SchedError> {
        let index = self
            .sequences
            .iter()
            .position(|sequence| sequence.seq_id == seq_id)
            .ok_or(SchedError::UnknownSequence(seq_id))?;
        let sequence = self.sequences.remove(index);
        Ok(release_receipt(&sequence, reason))
    }

    pub(crate) fn take_for_readmission(
        &mut self,
        seq_id: u32,
    ) -> Result<(usize, ActiveSequence), SchedError> {
        let index = self
            .sequences
            .iter()
            .position(|sequence| sequence.seq_id == seq_id)
            .ok_or(SchedError::UnknownSequence(seq_id))?;
        let sequence = &self.sequences[index];
        if sequence.priority != PriorityClass::Background
            || !sequence.preempted
            || sequence.slot_id.is_some()
        {
            return Err(SchedError::InvalidRequest(
                "only a suspended Background sequence may be readmitted",
            ));
        }
        Ok((index, self.sequences.remove(index)))
    }

    pub(crate) fn restore_failed_readmission(&mut self, index: usize, sequence: ActiveSequence) {
        debug_assert!(index <= self.sequences.len());
        self.sequences.insert(index, sequence);
    }

    pub fn drain(&mut self) -> Vec<SequenceReleaseReceipt> {
        self.sequences
            .drain(..)
            .map(|sequence| release_receipt(&sequence, TerminationReason::Drained))
            .collect()
    }

    pub fn resume(&mut self, seq_id: u32, slot_id: usize) -> Result<(), SchedError> {
        self.clear_interactive_blocking_arrival();
        self.resume_on_slot(seq_id, slot_id)
    }

    pub(crate) fn resume_on_slot(&mut self, seq_id: u32, slot_id: usize) -> Result<(), SchedError> {
        if self.sequences.iter().any(|sequence| {
            sequence.seq_id != seq_id && !sequence.preempted && sequence.slot_id == Some(slot_id)
        }) {
            return Err(SchedError::InvalidRequest(
                "resumed sequence slot is already active",
            ));
        }
        let sequence = self
            .sequences
            .iter_mut()
            .find(|sequence| sequence.seq_id == seq_id)
            .ok_or(SchedError::UnknownSequence(seq_id))?;
        if sequence.priority != PriorityClass::Background || !sequence.preempted {
            return Err(SchedError::InvalidRequest(
                "only a preempted Background sequence may resume",
            ));
        }
        sequence.slot_id = Some(slot_id);
        sequence.preempted = false;
        Ok(())
    }

    pub fn set_eval_deadline(&mut self, seq_id: u32, deadline_step: u64) -> Result<(), SchedError> {
        let sequence = self
            .sequences
            .iter_mut()
            .find(|sequence| sequence.seq_id == seq_id)
            .ok_or(SchedError::UnknownSequence(seq_id))?;
        if sequence.priority != PriorityClass::Eval {
            return Err(SchedError::InvalidRequest(
                "only Eval sequences accept scheduler deadlines",
            ));
        }
        sequence.deadline_step = Some(deadline_step);
        Ok(())
    }

    fn indices_for_group(
        &self,
        group: PolicyGroup,
        virtually_preempted: &BTreeSet<u32>,
    ) -> Vec<usize> {
        self.sequences
            .iter()
            .enumerate()
            .filter_map(|(index, sequence)| {
                (sequence.priority.policy_group() == group
                    && !sequence.preempted
                    && !virtually_preempted.contains(&sequence.seq_id))
                .then_some(index)
            })
            .collect()
    }

    #[cfg(any(feature = "cpu", feature = "cuda"))]
    pub fn decode_tokens(
        context: &mut crate::ffi::Context,
        batch: &mut crate::ffi::Batch,
        tokens: &[DecodeToken<'_>],
    ) -> Result<(), SchedError> {
        batch.clear();
        for item in tokens {
            batch
                .add_token(
                    item.token,
                    item.position,
                    item.sequence_ids,
                    item.request_logits,
                )
                .map_err(|_| SchedError::InvalidRequest("invalid llama.cpp batch fill"))?;
        }
        context.decode(batch).map_err(|error| match error {
            crate::ffi::FfiError::Decode(status) => SchedError::Decode(status),
            _ => SchedError::InvalidRequest("llama.cpp batch decode failed"),
        })
    }
}

#[cfg(any(feature = "cpu", feature = "cuda"))]
pub struct DecodeToken<'a> {
    pub token: i32,
    pub position: i32,
    pub sequence_ids: &'a [i32],
    pub request_logits: bool,
}

fn share_ceil(total: u32, percentage: u32) -> u32 {
    total.saturating_mul(percentage).div_ceil(100)
}

fn allocate_fair(
    sequences: &[ActiveSequence],
    indices: &[usize],
    budget: u32,
    work: &mut BTreeMap<u32, (SequencePhase, u32)>,
) -> u32 {
    let mut used = 0_u32;
    while used < budget {
        let mut progressed = false;
        for index in indices {
            if used >= budget {
                break;
            }
            let sequence = &sequences[*index];
            let (phase, available) = if sequence.prefill_tokens_remaining > 0 {
                (SequencePhase::Prefill, sequence.prefill_tokens_remaining)
            } else {
                (SequencePhase::Decode, decode_compute_demand(sequence))
            };
            let already = work
                .get(&sequence.seq_id)
                .map(|(_, count)| *count)
                .unwrap_or(0);
            if already < available {
                let entry = work.entry(sequence.seq_id).or_insert((phase, 0));
                entry.1 += 1;
                used += 1;
                progressed = true;
            }
        }
        if !progressed {
            break;
        }
    }
    used
}

fn decode_compute_demand(sequence: &ActiveSequence) -> u32 {
    sequence
        .decode_tokens_remaining
        .saturating_mul(2)
        .saturating_add(1)
}

fn normal_decode_progress(plan: &BatchPlan) -> Vec<DecodeProgress> {
    plan.work
        .iter()
        .filter(|work| work.phase == SequencePhase::Decode)
        .map(|work| DecodeProgress {
            seq_id: work.seq_id,
            compute_tokens: 1,
            output_tokens: 1,
        })
        .collect()
}

fn release_receipt(sequence: &ActiveSequence, reason: TerminationReason) -> SequenceReleaseReceipt {
    SequenceReleaseReceipt {
        seq_id: sequence.seq_id,
        slot_id: sequence.slot_id,
        priority: sequence.priority,
        reason,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn policy(budget: u32) -> PriorityPolicy {
        PriorityPolicy::new(8, budget).unwrap()
    }

    fn sequence(id: u32, priority: PriorityClass, tokens: u32) -> ActiveSequence {
        ActiveSequence {
            seq_id: id,
            slot_id: Some(id as usize),
            priority,
            prefill_tokens_remaining: 0,
            decode_tokens_remaining: tokens,
            deadline_step: None,
            preempted: false,
        }
    }

    fn prefill_sequence(id: u32, priority: PriorityClass, tokens: u32) -> ActiveSequence {
        ActiveSequence {
            seq_id: id,
            slot_id: Some(id as usize),
            priority,
            prefill_tokens_remaining: tokens,
            decode_tokens_remaining: 0,
            deadline_step: None,
            preempted: false,
        }
    }

    #[test]
    fn sched_plan_is_non_mutating_until_commit() {
        let mut batch = BatchLoop::new(policy(4), true);
        batch
            .add_sequence(sequence(1, PriorityClass::Worker, 4))
            .unwrap();
        let plan = batch.plan_step();
        assert_eq!(batch.active()[0].decode_tokens_remaining, 4);
        let progress = normal_decode_progress(&plan);
        let step = batch
            .commit_step(plan, &progress, 0, &mut Vec::new())
            .unwrap();
        assert!(step.token_count > 0);
        assert!(batch.active()[0].decode_tokens_remaining < 4);
    }

    #[test]
    fn normal_decode_consumes_one_output_despite_a_larger_compute_grant() {
        let mut batch = BatchLoop::new(policy(16), false);
        batch
            .add_sequence(sequence(1, PriorityClass::Worker, 5))
            .unwrap();
        let plan = batch.plan_step();
        assert!(plan.work[0].token_count > 1);
        let progress = [DecodeProgress {
            seq_id: 1,
            compute_tokens: 1,
            output_tokens: 1,
        }];
        let receipt = batch
            .commit_step(plan, &progress, 0, &mut Vec::new())
            .unwrap();
        assert_eq!(receipt.token_count, 1);
        assert_eq!(batch.active()[0].decode_tokens_remaining, 4);
    }

    #[test]
    fn speculative_decode_consumes_actual_outputs_and_rejects_grant_overrun() {
        let mut batch = BatchLoop::new(policy(16), false);
        batch
            .add_sequence(sequence(1, PriorityClass::Worker, 5))
            .unwrap();
        let plan = batch.plan_step();
        let before = batch.active().to_vec();
        let excessive = [DecodeProgress {
            seq_id: 1,
            compute_tokens: plan.work[0].token_count.saturating_add(1),
            output_tokens: 2,
        }];
        assert!(matches!(
            batch.commit_step(plan.clone(), &excessive, 0, &mut Vec::new()),
            Err(SchedError::InvalidRequest(
                "decode progress exceeds its compute or output grant"
            ))
        ));
        assert_eq!(batch.active(), before);

        let progress = [DecodeProgress {
            seq_id: 1,
            compute_tokens: 5,
            output_tokens: 2,
        }];
        let receipt = batch
            .commit_step(plan, &progress, 0, &mut Vec::new())
            .unwrap();
        assert_eq!(receipt.token_count, 5);
        assert_eq!(batch.active()[0].decode_tokens_remaining, 3);
    }

    #[test]
    fn sched_batch_preempts_only_background_at_commit_boundary() {
        let mut batch = BatchLoop::new(policy(4), true);
        batch
            .add_sequence(sequence(1, PriorityClass::Background, 4))
            .unwrap();
        batch
            .add_sequence(sequence(2, PriorityClass::Worker, 4))
            .unwrap();
        batch.notify_interactive_blocking_arrival();
        let plan = batch.plan_step();
        assert!(!batch.active()[0].preempted);
        let progress = normal_decode_progress(&plan);
        let result = batch
            .commit_step(plan, &progress, 1, &mut Vec::new())
            .unwrap();
        assert_eq!(result.preempted_sequence_ids, vec![1]);
        assert!(result.sequence_ids.contains(&2));
        assert!(
            batch
                .active()
                .iter()
                .find(|item| item.seq_id == 1)
                .unwrap()
                .preempted
        );
    }

    #[test]
    fn sched_batch_disabled_preemption_keeps_background_runnable() {
        let mut batch = BatchLoop::new(PriorityPolicy::new(1, 4).unwrap(), false);
        batch
            .add_sequence(sequence(1, PriorityClass::Background, 2))
            .unwrap();
        batch.notify_interactive_blocking_arrival();
        let plan = batch.plan_step();
        assert!(plan.preempted_sequence_ids.is_empty());
        assert_eq!(plan.work[0].seq_id, 1);
    }

    #[test]
    fn sched_batch_policy_redistributes_only_downward() {
        let mut batch = BatchLoop::new(policy(20), false);
        batch
            .add_sequence(prefill_sequence(1, PriorityClass::Interactive, 50))
            .unwrap();
        batch
            .add_sequence(prefill_sequence(2, PriorityClass::Worker, 50))
            .unwrap();
        batch
            .add_sequence(prefill_sequence(3, PriorityClass::Background, 50))
            .unwrap();
        let plan = batch.plan_step();
        assert_eq!(plan.work[0].token_count, 10);
        assert_eq!(plan.work[1].token_count, 7);
        assert_eq!(plan.work[2].token_count, 3);
        assert!(plan
            .work
            .iter()
            .all(|work| work.phase == SequencePhase::Prefill));
    }

    #[test]
    fn sched_prefill_is_chunked_and_decode_receives_a_bounded_compute_grant() {
        let mut batch = BatchLoop::new(policy(8), false);
        let mut item = sequence(1, PriorityClass::Interactive, 3);
        item.prefill_tokens_remaining = 6;
        batch.add_sequence(item).unwrap();
        let prefill = batch.plan_step();
        assert_eq!(prefill.work[0].phase, SequencePhase::Prefill);
        assert!(prefill.work[0].token_count > 1);
        batch.commit_step(prefill, &[], 0, &mut Vec::new()).unwrap();
        while batch.active()[0].prefill_tokens_remaining > 0 {
            let plan = batch.plan_step();
            batch.commit_step(plan, &[], 0, &mut Vec::new()).unwrap();
        }
        let decode = batch.plan_step();
        assert_eq!(decode.work[0].phase, SequencePhase::Decode);
        assert_eq!(decode.work[0].token_count, 4);
    }

    #[test]
    fn sched_eval_timeout_returns_cleanup_receipt() {
        let mut batch = BatchLoop::new(policy(2), false);
        let mut eval = sequence(2, PriorityClass::Eval, 8);
        eval.deadline_step = Some(0);
        batch.add_sequence(eval).unwrap();
        let plan = batch.plan_step();
        assert_eq!(plan.timeout_receipts[0].seq_id, 2);
        assert_eq!(batch.active().len(), 1);
        let result = batch.commit_step(plan, &[], 0, &mut Vec::new()).unwrap();
        assert_eq!(
            result.release_receipts[0].reason,
            TerminationReason::EvalTimeout
        );
        assert!(batch.active().is_empty());
    }

    #[test]
    fn sched_completion_and_cancel_return_release_receipts() {
        let mut batch = BatchLoop::new(policy(4), false);
        batch
            .add_sequence(sequence(1, PriorityClass::Worker, 1))
            .unwrap();
        let result = batch.step(0, &mut Vec::new()).unwrap();
        assert_eq!(
            result.release_receipts[0].reason,
            TerminationReason::Completed
        );
        batch
            .add_sequence(sequence(2, PriorityClass::Background, 3))
            .unwrap();
        assert_eq!(
            batch.cancel(2).unwrap().reason,
            TerminationReason::Cancelled
        );
    }

    #[test]
    fn sched_stale_plan_is_rejected_without_mutation() {
        let mut batch = BatchLoop::new(policy(4), false);
        batch
            .add_sequence(sequence(1, PriorityClass::Worker, 4))
            .unwrap();
        let plan = batch.plan_step();
        let progress = normal_decode_progress(&plan);
        batch
            .commit_step(plan.clone(), &progress, 0, &mut Vec::new())
            .unwrap();
        let before = batch.active().to_vec();
        assert!(matches!(
            batch.commit_step(plan, &progress, 0, &mut Vec::new()),
            Err(SchedError::StalePlan { .. })
        ));
        assert_eq!(batch.active(), before);
    }

    #[test]
    fn sched_tampered_plan_is_rejected_transactionally() {
        let mut batch = BatchLoop::new(policy(4), false);
        batch
            .add_sequence(sequence(1, PriorityClass::Worker, 4))
            .unwrap();
        let mut plan = batch.plan_step();
        plan.work[0].token_count = 2;
        let before = batch.active().to_vec();
        assert_eq!(
            batch.commit_step(plan, &[], 0, &mut Vec::new()),
            Err(SchedError::InvalidRequest(
                "batch plan no longer matches scheduler state"
            ))
        );
        assert_eq!(batch.active(), before);
    }
}
