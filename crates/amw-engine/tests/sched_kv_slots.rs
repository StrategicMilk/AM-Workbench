use std::collections::BTreeSet;

use amw_engine::{
    hw::budget::MemoryAmount,
    sched::{
        AdmissionRequest, PriorityClass, ReadmissionReason, SchedError, SchedulerCore,
        SchedulerCoreConfig, SeqId, SequenceBackend,
    },
};

#[derive(Default)]
struct FixtureBackend {
    removed: Vec<SeqId>,
}

impl SequenceBackend for FixtureBackend {
    fn copy_sequence(
        &mut self,
        _source: SeqId,
        _destination: SeqId,
        _cells: u32,
    ) -> Result<(), SchedError> {
        Ok(())
    }

    fn remove_sequence(&mut self, seq_id: SeqId) -> Result<(), SchedError> {
        self.removed.push(seq_id);
        Ok(())
    }

    fn export_sequence(&mut self, seq_id: SeqId) -> Result<Vec<u8>, SchedError> {
        Ok(vec![seq_id as u8])
    }

    fn import_sequence(&mut self, _seq_id: SeqId, _state: &[u8]) -> Result<(), SchedError> {
        Ok(())
    }
}

fn request(
    id: u64,
    priority: PriorityClass,
    prompt_tokens: u32,
    max_tokens: u32,
) -> AdmissionRequest {
    AdmissionRequest {
        request_id: id,
        principal_id: "scheduler-integration".to_owned(),
        priority,
        prompt_tokens,
        max_tokens,
        decode_steps: max_tokens.saturating_sub(1),
        context_limit: 32,
        kv_bytes: u64::from(prompt_tokens.saturating_add(max_tokens)),
    }
}

#[test]
fn sched_kv_slots_pressure_readmits_background_and_preserves_interactive_and_worker() {
    let temp = tempfile::tempdir().unwrap();
    let mut scheduler = SchedulerCore::new(SchedulerCoreConfig {
        slot_count: 3,
        native_sequence_capacity: 8,
        queue_capacity: 8,
        batch_token_budget: 8,
        preemption_enabled: true,
        kv_capacity_cells: 16,
        kv_bytes_per_cell: 1,
        admission_memory: MemoryAmount::ram(256),
        kv_memory: MemoryAmount::ram(256),
        session_dir: temp.path().to_owned(),
        prefix_capacity: 2,
    })
    .unwrap();
    let mut backend = FixtureBackend::default();
    let mut events = Vec::new();

    let protected = [(1, PriorityClass::Worker), (2, PriorityClass::Interactive)];
    let mut protected_sequences = BTreeSet::new();
    for (step, (request_id, priority)) in protected.into_iter().enumerate() {
        scheduler
            .submit(request(request_id, priority, 2, 2), step as u64)
            .unwrap();
        let admitted = scheduler
            .admit(&mut backend, step as u64 + 1, &mut events)
            .unwrap()
            .unwrap();
        protected_sequences.insert(admitted.seq_id);
    }

    scheduler
        .submit(request(4, PriorityClass::Background, 4, 4), 4)
        .unwrap();
    let background = scheduler
        .admit(&mut backend, 5, &mut events)
        .unwrap()
        .unwrap();
    scheduler
        .submit(request(5, PriorityClass::InteractiveBlocking, 3, 3), 6)
        .unwrap();
    let suspension = scheduler.plan_step();
    assert_eq!(suspension.preempted_sequence_ids, vec![background.seq_id]);
    let committed = scheduler
        .commit_step(&mut backend, suspension, &mut events)
        .unwrap();
    assert_eq!(committed.suspended[0].seq_id, background.seq_id);

    let outcome = scheduler
        .admit_with_prefix_under_pressure_identified(&mut backend, 7, None, &mut events)
        .unwrap();
    let blocker = outcome.admitted.unwrap();
    assert_eq!(blocker.request_id, 5);
    assert_eq!(outcome.readmissions.len(), 1);
    let readmission = &outcome.readmissions[0];
    assert_eq!(readmission.request_id, 4);
    assert_eq!(readmission.sequence.seq_id, background.seq_id);
    assert_eq!(readmission.sequence.evicted_cells, 8);
    assert_eq!(readmission.sequence.reason, ReadmissionReason::KvPressure);
    assert_eq!(readmission.admission.request_id, 4);
    assert_eq!(readmission.admission.released_kv_bytes, 8);
    assert_eq!(scheduler.background_evicted(), 1);

    let snapshot = scheduler.snapshot();
    let active_sequences: BTreeSet<_> = snapshot.active.iter().map(|item| item.seq_id).collect();
    let active_requests: BTreeSet<_> = snapshot.active.iter().map(|item| item.request_id).collect();
    assert!(protected_sequences.is_subset(&active_sequences));
    assert_eq!(active_requests, BTreeSet::from([1, 2, 5]));
    assert_eq!(snapshot.kv_used_cells, 14);
    assert_eq!(backend.removed, vec![background.seq_id]);
}

#[test]
fn sched_kv_slots_eval_is_never_preempted_or_pressure_evicted() {
    let temp = tempfile::tempdir().unwrap();
    let mut scheduler = SchedulerCore::new(SchedulerCoreConfig {
        slot_count: 1,
        native_sequence_capacity: 4,
        queue_capacity: 4,
        batch_token_budget: 4,
        preemption_enabled: true,
        kv_capacity_cells: 8,
        kv_bytes_per_cell: 1,
        admission_memory: MemoryAmount::ram(64),
        kv_memory: MemoryAmount::ram(64),
        session_dir: temp.path().to_owned(),
        prefix_capacity: 2,
    })
    .unwrap();
    let mut backend = FixtureBackend::default();
    let mut events = Vec::new();

    scheduler
        .submit(request(10, PriorityClass::Eval, 4, 4), 0)
        .unwrap();
    let eval = scheduler
        .admit(&mut backend, 1, &mut events)
        .unwrap()
        .unwrap();
    scheduler
        .submit(request(11, PriorityClass::InteractiveBlocking, 3, 3), 2)
        .unwrap();
    assert!(scheduler.plan_step().preempted_sequence_ids.is_empty());
    let outcome = scheduler
        .admit_with_prefix_under_pressure_identified(&mut backend, 3, None, &mut events)
        .unwrap();
    assert!(outcome.admitted.is_none());
    assert!(outcome.readmissions.is_empty());
    let snapshot = scheduler.snapshot();
    assert_eq!(snapshot.active.len(), 1);
    assert_eq!(snapshot.active[0].seq_id, eval.seq_id);
    assert_eq!(snapshot.active[0].priority, PriorityClass::Eval);
    assert_eq!(snapshot.kv_used_cells, 8);
    assert_eq!(scheduler.background_evicted(), 0);
    assert!(backend.removed.is_empty());
}
