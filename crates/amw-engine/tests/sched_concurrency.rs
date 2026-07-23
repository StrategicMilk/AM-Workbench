use amw_engine::sched::{ActiveSequence, BatchLoop, PriorityClass, PriorityPolicy};

#[cfg(any(feature = "cpu", feature = "cuda"))]
use amw_engine::sched::CoreSessionRestoreOptions;

#[cfg(any(feature = "cpu", feature = "cuda"))]
static NATIVE_TEST_LOCK: std::sync::Mutex<()> = std::sync::Mutex::new(());

#[test]
fn sched_policy_preserves_sequence_isolation_and_preemption() {
    let mut scheduler = BatchLoop::new(PriorityPolicy::new(2, 2).unwrap(), true);
    scheduler
        .add_sequence(ActiveSequence {
            seq_id: 1,
            slot_id: Some(0),
            priority: PriorityClass::Background,
            prefill_tokens_remaining: 0,
            decode_tokens_remaining: 4,
            deadline_step: None,
            preempted: false,
        })
        .unwrap();
    scheduler
        .add_sequence(ActiveSequence {
            seq_id: 2,
            slot_id: Some(1),
            priority: PriorityClass::Worker,
            prefill_tokens_remaining: 0,
            decode_tokens_remaining: 4,
            deadline_step: None,
            preempted: false,
        })
        .unwrap();

    scheduler.notify_interactive_blocking_arrival();
    let first = scheduler.step(1, &mut Vec::new()).unwrap();
    assert_eq!(first.preempted_sequence_ids, vec![1]);
    assert_eq!(first.sequence_ids, vec![2]);

    while scheduler
        .active()
        .iter()
        .any(|sequence| sequence.seq_id == 2)
    {
        scheduler.step(0, &mut Vec::new()).unwrap();
    }
    scheduler.resume(1, 0).unwrap();
    let mut background_tokens = 0;
    while scheduler
        .active()
        .iter()
        .any(|sequence| sequence.seq_id == 1)
    {
        let step = scheduler.step(0, &mut Vec::new()).unwrap();
        if step.sequence_ids.contains(&1) {
            background_tokens += step.token_count;
        }
    }
    assert_eq!(background_tokens, 4);
}

#[cfg(any(feature = "cpu", feature = "cuda"))]
#[test]
fn sched_concurrency_decodes_two_sequences_on_one_native_context() {
    use std::path::PathBuf;

    use amw_engine::ffi::{Batch, ContextOptions, Model, Sampler};
    use amw_engine::sched::SequenceBackend;

    let _native_guard = NATIVE_TEST_LOCK.lock().unwrap();
    let path = std::env::var_os("AMW_ENGINE_NATIVE_TEST_MODEL")
        .or_else(|| std::env::var_os("AMW_ENGINE_TEST_MODEL"))
        .map(PathBuf::from)
        .expect("AMW_ENGINE_NATIVE_TEST_MODEL must name the governed pinned GGUF fixture");
    let model = Model::load(&path).unwrap();
    let prompts = [
        model.tokenize("The first sequence", true, false).unwrap(),
        model
            .tokenize("A different second sequence", true, false)
            .unwrap(),
    ];
    let capacity = prompts.iter().map(Vec::len).sum::<usize>();
    let mut context = model
        .context_with(ContextOptions {
            context_tokens: 256,
            batch_tokens: 64,
            micro_batch_tokens: 64,
            max_sequences: 64,
            unified_kv: true,
            embeddings: false,
            pooling: None,
        })
        .unwrap();
    let metadata = context.metadata();
    assert_eq!(metadata.context_tokens, 256);
    assert_eq!(metadata.batch_tokens, 64);
    assert_eq!(metadata.max_sequences, 64);
    let mut batch = Batch::tokens(i32::try_from(capacity).unwrap(), 1).unwrap();
    let mut final_rows = [0_i32; 2];
    let mut batch_row = 0_i32;
    for (seq_id, tokens) in prompts.iter().enumerate() {
        for (position, token) in tokens.iter().copied().enumerate() {
            let final_token = position + 1 == tokens.len();
            batch
                .add_token(
                    token,
                    i32::try_from(position).unwrap(),
                    &[i32::try_from(seq_id).unwrap()],
                    final_token,
                )
                .unwrap();
            if final_token {
                final_rows[seq_id] = batch_row;
            }
            batch_row += 1;
        }
    }
    context.decode(&mut batch).unwrap();
    let mut first_sampler = Sampler::greedy().unwrap();
    let mut second_sampler = Sampler::greedy().unwrap();
    let first = first_sampler
        .sample_and_accept(&mut context, final_rows[0])
        .unwrap();
    let second = second_sampler
        .sample_and_accept(&mut context, final_rows[1])
        .unwrap();
    assert!(first >= 0 && second >= 0);
    let first_state = context.sequence_state(0).unwrap();
    let second_state = context.sequence_state(1).unwrap();
    assert!(!first_state.is_empty() && !second_state.is_empty());
    assert_ne!(first_state, second_state);
    assert!(context.memory_seq_rm(0, -1, -1).unwrap());
    let shared_cells = u32::try_from(prompts[1].len()).unwrap();
    context.copy_sequence(1, 0, shared_cells).unwrap();
    assert!(context.memory_seq_pos_max(0).unwrap() >= 0);
    context.remove_sequence(1).unwrap();
    assert!(context.memory_seq_pos_max(0).unwrap() >= 0);
    assert!(!context.sequence_state(0).unwrap().is_empty());
}

#[cfg(any(feature = "cpu", feature = "cuda"))]
#[test]
fn sched_session_survives_context_restart_and_regenerates_continuation_logits() {
    use std::path::PathBuf;

    use amw_engine::{
        ffi::{Batch, ContextOptions, Model, Sampler},
        hw::budget::MemoryAmount,
        sched::{AdmissionRequest, SchedulerCore, SchedulerCoreConfig, SessionContinuation},
    };
    use sha2::{Digest, Sha256};

    let _native_guard = NATIVE_TEST_LOCK.lock().unwrap();
    let path = std::env::var_os("AMW_ENGINE_NATIVE_TEST_MODEL")
        .or_else(|| std::env::var_os("AMW_ENGINE_TEST_MODEL"))
        .map(PathBuf::from)
        .expect("AMW_ENGINE_NATIVE_TEST_MODEL must name the governed pinned GGUF fixture");
    let fingerprint: [u8; 32] = Sha256::digest(std::fs::read(&path).unwrap()).into();
    let owner_fingerprint = [7_u8; 32];
    let session_dir = tempfile::tempdir().unwrap();
    let scheduler = || {
        SchedulerCore::new(SchedulerCoreConfig {
            slot_count: 2,
            native_sequence_capacity: 8,
            queue_capacity: 4,
            batch_token_budget: 32,
            preemption_enabled: true,
            kv_capacity_cells: 256,
            kv_bytes_per_cell: 1,
            admission_memory: MemoryAmount::ram(1_024),
            kv_memory: MemoryAmount::ram(1_024),
            session_dir: session_dir.path().to_owned(),
            prefix_capacity: 4,
        })
        .unwrap()
    };
    let context_options = ContextOptions {
        context_tokens: 128,
        batch_tokens: 32,
        micro_batch_tokens: 32,
        max_sequences: 8,
        unified_kv: true,
        embeddings: false,
        pooling: None,
    };

    let model = Model::load(&path).unwrap();
    let prompt = model.tokenize("A saved session", true, false).unwrap();
    let mut context = model.context_with(context_options).unwrap();
    let metadata = context.metadata();
    assert_eq!(metadata.context_tokens, 256);
    assert_eq!(metadata.max_sequences, 8);
    let mut first = scheduler();
    first
        .submit(
            AdmissionRequest {
                request_id: 90,
                principal_id: "scheduler-concurrency".to_owned(),
                priority: PriorityClass::Worker,
                prompt_tokens: u32::try_from(prompt.len()).unwrap(),
                max_tokens: 1,
                decode_steps: 0,
                context_limit: 128,
                kv_bytes: u64::try_from(prompt.len() + 1).unwrap(),
            },
            0,
        )
        .unwrap();
    let admitted = first
        .admit(&mut context, 1, &mut Vec::new())
        .unwrap()
        .unwrap();
    let mut prefill = Batch::tokens(i32::try_from(prompt.len()).unwrap(), 1).unwrap();
    for (position, token) in prompt.iter().copied().enumerate() {
        prefill
            .add_token(
                token,
                i32::try_from(position).unwrap(),
                &[i32::try_from(admitted.seq_id).unwrap()],
                position + 1 == prompt.len(),
            )
            .unwrap();
    }
    context.decode(&mut prefill).unwrap();
    let mut sampler = Sampler::greedy().unwrap();
    let pending_token = sampler
        .sample_and_accept(
            &mut context,
            i32::try_from(prompt.len().saturating_sub(1)).unwrap(),
        )
        .unwrap();
    let continuation = SessionContinuation::new(
        fingerprint,
        pending_token,
        u32::try_from(prompt.len() + 1).unwrap(),
    )
    .unwrap();
    first
        .save_session(
            &mut context,
            "native-restart",
            admitted.seq_id,
            continuation,
            owner_fingerprint,
        )
        .unwrap();
    drop(first);
    drop(context);
    drop(model);

    let restored_model = Model::load(&path).unwrap();
    let appended = restored_model.tokenize(" continued", false, false).unwrap();
    let mut restored_context = restored_model.context_with(context_options).unwrap();
    let mut restored_scheduler = scheduler();
    assert_eq!(
        restored_scheduler
            .session_continuation("native-restart")
            .unwrap(),
        continuation
    );
    let appended_count = u32::try_from(appended.len()).unwrap();
    let restore_request = AdmissionRequest {
        request_id: 91,
        principal_id: "scheduler-concurrency".to_owned(),
        priority: PriorityClass::Worker,
        prompt_tokens: continuation.next_position() + appended_count,
        max_tokens: 1,
        decode_steps: 0,
        context_limit: 128,
        kv_bytes: u64::from(continuation.next_position() + appended_count + 1),
    };
    let mut wrong_owner_scheduler = scheduler();
    assert!(matches!(
        wrong_owner_scheduler.restore_session(
            &mut restored_context,
            "native-restart",
            CoreSessionRestoreOptions {
                request: restore_request.clone(),
                expected_model_fingerprint: fingerprint,
                expected_owner_fingerprint: [8_u8; 32],
                appended_prompt_tokens: appended_count,
                now_step: 2,
            },
            &mut Vec::new(),
        ),
        Err(amw_engine::sched::SchedError::SessionUnknown(session))
            if session == "native-restart"
    ));
    let restored = restored_scheduler
        .restore_session(
            &mut restored_context,
            "native-restart",
            CoreSessionRestoreOptions {
                request: restore_request,
                expected_model_fingerprint: fingerprint,
                expected_owner_fingerprint: owner_fingerprint,
                appended_prompt_tokens: appended_count,
                now_step: 2,
            },
            &mut Vec::new(),
        )
        .unwrap();
    assert_eq!(restored.continuation_token, pending_token);
    assert_eq!(
        restored.continuation_position,
        u32::try_from(prompt.len()).unwrap()
    );
    let plan = restored_scheduler.plan_step();
    assert_eq!(plan.work[0].token_count, appended_count + 1);

    let mut continuation_batch =
        Batch::tokens(i32::try_from(appended.len() + 1).unwrap(), 1).unwrap();
    let mut feed = Vec::with_capacity(appended.len() + 1);
    feed.push(pending_token);
    feed.extend_from_slice(&appended);
    for (offset, token) in feed.iter().copied().enumerate() {
        continuation_batch
            .add_token(
                token,
                i32::try_from(restored.continuation_position as usize + offset).unwrap(),
                &[i32::try_from(restored.admission.seq_id).unwrap()],
                offset + 1 == feed.len(),
            )
            .unwrap();
    }
    restored_context.decode(&mut continuation_batch).unwrap();
    let mut resumed_sampler = Sampler::greedy().unwrap();
    let resumed_token = resumed_sampler
        .sample_and_accept(
            &mut restored_context,
            i32::try_from(feed.len().saturating_sub(1)).unwrap(),
        )
        .unwrap();
    assert!(resumed_token >= 0);
}
