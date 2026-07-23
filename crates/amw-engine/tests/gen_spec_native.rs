#![cfg(any(feature = "cpu", feature = "cuda"))]

use std::collections::BTreeMap;

use amw_engine::{
    ffi::{Batch, ContextOptions, FfiError, Model},
    gen::{
        bounded_generation_stream, resolve_draft_mode, DraftJob, DraftMode, DraftModelBackend,
        DraftModelCompatibility, DraftProposal, ExternalBundleToken, GenerationControl,
        GenerationEvent, GenerationExecutor, NativeDraftBackend, NativeGenerationConfig,
        SamplerCapabilities, SamplerParams, SpeculationPlan, StepOutcome, StopEvaluator,
        StopReason, TargetKvFork, TargetVerification,
    },
    store::registry::{DraftPair, ModelRecord},
};

fn governed_path() -> std::path::PathBuf {
    std::env::var_os("AMW_ENGINE_NATIVE_TEST_MODEL")
        .map(std::path::PathBuf::from)
        .expect("AMW_ENGINE_NATIVE_TEST_MODEL must name the governed pinned GGUF fixture")
}

fn governed_model() -> Model {
    Model::load(&governed_path()).unwrap()
}

fn context_options(max_sequences: u32) -> ContextOptions {
    ContextOptions {
        context_tokens: 256,
        batch_tokens: 128,
        micro_batch_tokens: 128,
        max_sequences,
        unified_kv: false,
        embeddings: false,
        pooling: None,
    }
}

#[test]
fn two_sequence_shared_verification_batch_preserves_cancelled_sibling_isolation() {
    let model = governed_model();
    let prompt = model.tokenize("shared verification", true, false).unwrap();
    assert!(!prompt.is_empty());
    let mut context = model.context_with(context_options(4)).unwrap();
    let mut prefill = Batch::tokens(i32::try_from(prompt.len() * 2).unwrap(), 1).unwrap();
    for sequence in [0, 1] {
        for (position, token) in prompt.iter().enumerate() {
            prefill
                .add_token(*token, i32::try_from(position).unwrap(), &[sequence], false)
                .unwrap();
        }
    }
    context.decode(&mut prefill).unwrap();
    let original_zero = context.sequence_state(0).unwrap();
    let original_one = context.sequence_state(1).unwrap();
    let fork_zero = TargetKvFork::begin(&mut context, 0, 2, 1).unwrap();
    let fork_one = TargetKvFork::begin(&mut context, 1, 3, 1).unwrap();

    let mut verification = Batch::tokens(2, 1).unwrap();
    let rows_zero = fork_zero
        .append_proposals(&mut verification, &[prompt[0]])
        .unwrap();
    let rows_one = fork_one
        .append_proposals(&mut verification, &[prompt[0]])
        .unwrap();
    assert_eq!(rows_zero, vec![0]);
    assert_eq!(rows_one, vec![1]);
    context.decode(&mut verification).unwrap();
    assert_eq!(
        context.logits(rows_zero[0]).unwrap().len(),
        model.vocab_size()
    );
    assert_eq!(
        context.logits(rows_one[0]).unwrap().len(),
        model.vocab_size()
    );

    fork_zero.rollback(&mut context).unwrap();
    fork_one.commit(&mut context, 1).unwrap();
    assert_eq!(context.sequence_state(0).unwrap(), original_zero);
    assert_ne!(context.sequence_state(1).unwrap(), original_one);
    assert_eq!(context.memory_seq_pos_max(2).unwrap(), -1);
    assert_eq!(context.memory_seq_pos_max(3).unwrap(), -1);
    assert_eq!(
        context.logits(rows_zero[0]),
        Err(FfiError::OutputUnavailable),
        "KV rollback/commit must invalidate tentative shared verification rows"
    );
}

#[test]
fn empty_speculative_commit_restores_bytes_and_refuses_stale_logits() {
    let model = governed_model();
    let prompt = model.tokenize("empty rollback", true, false).unwrap();
    let mut context = model.context_with(context_options(2)).unwrap();
    let mut prefill = Batch::tokens(i32::try_from(prompt.len()).unwrap(), 1).unwrap();
    for (position, token) in prompt.iter().enumerate() {
        prefill
            .add_token(*token, i32::try_from(position).unwrap(), &[0], false)
            .unwrap();
    }
    context.decode(&mut prefill).unwrap();
    let original = context.sequence_state(0).unwrap();
    let fork = TargetKvFork::begin(&mut context, 0, 1, 1).unwrap();
    let mut verification = Batch::tokens(1, 1).unwrap();
    let rows = fork
        .append_proposals(&mut verification, &[prompt[0]])
        .unwrap();
    context.decode(&mut verification).unwrap();
    assert!(context.logits(rows[0]).is_ok());
    fork.commit(&mut context, 0).unwrap();
    assert_eq!(context.sequence_state(0).unwrap(), original);
    assert_eq!(context.memory_seq_pos_max(1).unwrap(), -1);
    assert_eq!(context.logits(rows[0]), Err(FfiError::OutputUnavailable));
}

#[test]
fn cloned_sampler_probe_and_external_bundle_commit_advance_exactly_once() {
    let model = governed_model();
    let prompt = model.tokenize("speculative bundle", true, false).unwrap();
    let mut context = model.context_with(context_options(1)).unwrap();
    let mut prefill = Batch::tokens(i32::try_from(prompt.len()).unwrap(), 1).unwrap();
    for (position, token) in prompt.iter().enumerate() {
        prefill
            .add_token(
                *token,
                i32::try_from(position).unwrap(),
                &[0],
                position + 1 == prompt.len(),
            )
            .unwrap();
    }
    context.decode(&mut prefill).unwrap();
    let params = SamplerParams {
        temperature: 0.0,
        ..SamplerParams::default()
    };
    let control = GenerationControl::default();
    let (sender, mut receiver) = bounded_generation_stream(control.clone());
    let mut executor = GenerationExecutor::new_native(
        &model,
        NativeGenerationConfig {
            params: &params,
            capabilities: SamplerCapabilities::pinned_revision(),
            grammar: None,
            top_logprobs: 2,
            prompt_tokens: prompt.len(),
        },
        StopEvaluator::new(Vec::new(), Vec::new(), 1).unwrap(),
        sender,
        control,
    )
    .unwrap();
    let mut transaction = executor.begin_speculative_sampler().unwrap();
    let probe = executor
        .probe_distribution(
            &mut transaction,
            &mut context,
            i32::try_from(prompt.len() - 1).unwrap(),
        )
        .unwrap();
    let token = probe.selected_token;
    let preview = executor
        .preview_external_bundle(
            &model,
            &[ExternalBundleToken {
                token_id: token,
                distribution: probe.distribution,
                sampler_probe_index: 0,
            }],
        )
        .unwrap();
    assert_eq!(preview.output_tokens(), 1);
    assert_eq!(preview.kv_tokens(), 0);
    assert_eq!(preview.pending_token(), token);
    assert_eq!(preview.terminal_reason(), Some(&StopReason::MaxTokens));
    let committed = executor
        .commit_external_bundle_try(transaction, preview)
        .unwrap();
    assert_eq!(committed.output_tokens, 1);
    assert_eq!(committed.kv_tokens, 0);
    assert!(matches!(
        committed.outcome,
        StepOutcome::Finished(StopReason::MaxTokens)
    ));
    let mut events = Vec::new();
    while let Some(event) = receiver.try_recv().unwrap() {
        events.push(event);
    }
    assert!(events.iter().any(|event| matches!(
        event,
        GenerationEvent::Finished { usage, .. } if usage.completion_tokens == 1
    )));
}

#[test]
fn boundary_spanning_stop_truncates_external_bundle_kv_sampler_and_events_together() {
    let model = governed_model();
    let prompt = model.tokenize("boundary stop", true, false).unwrap();
    let params = SamplerParams {
        temperature: 0.0,
        ..SamplerParams::default()
    };

    let mut discovery_context = model.context_with(context_options(1)).unwrap();
    let mut discovery_prefill = Batch::tokens(i32::try_from(prompt.len()).unwrap(), 1).unwrap();
    for (position, token) in prompt.iter().enumerate() {
        discovery_prefill
            .add_token(
                *token,
                i32::try_from(position).unwrap(),
                &[0],
                position + 1 == prompt.len(),
            )
            .unwrap();
    }
    discovery_context.decode(&mut discovery_prefill).unwrap();
    let discovery_control = GenerationControl::default();
    let (discovery_sender, _discovery_receiver) =
        bounded_generation_stream(discovery_control.clone());
    let discovery_executor = GenerationExecutor::new_native(
        &model,
        NativeGenerationConfig {
            params: &params,
            capabilities: SamplerCapabilities::pinned_revision(),
            grammar: None,
            top_logprobs: 0,
            prompt_tokens: prompt.len(),
        },
        StopEvaluator::new(Vec::new(), Vec::new(), 3).unwrap(),
        discovery_sender,
        discovery_control,
    )
    .unwrap();
    let mut discovery_sampler = discovery_executor.begin_speculative_sampler().unwrap();
    let first = discovery_executor
        .probe_distribution(
            &mut discovery_sampler,
            &mut discovery_context,
            i32::try_from(prompt.len() - 1).unwrap(),
        )
        .unwrap();
    discovery_sampler
        .accept_proposal(first.selected_token)
        .unwrap();
    let mut discovery_decode = Batch::tokens(1, 1).unwrap();
    discovery_decode
        .add_token(
            first.selected_token,
            i32::try_from(prompt.len()).unwrap(),
            &[0],
            true,
        )
        .unwrap();
    discovery_context.decode(&mut discovery_decode).unwrap();
    let second = discovery_executor
        .probe_distribution(&mut discovery_sampler, &mut discovery_context, 0)
        .unwrap();
    let first_piece = model.token_piece(first.selected_token, false).unwrap();
    let second_piece = model.token_piece(second.selected_token, false).unwrap();
    assert!(!first_piece.is_empty() && !second_piece.is_empty());
    let stop = String::from_utf8([first_piece, second_piece].concat()).unwrap();

    let mut context = model.context_with(context_options(2)).unwrap();
    let mut prefill = Batch::tokens(i32::try_from(prompt.len()).unwrap(), 1).unwrap();
    for (position, token) in prompt.iter().enumerate() {
        prefill
            .add_token(
                *token,
                i32::try_from(position).unwrap(),
                &[0],
                position + 1 == prompt.len(),
            )
            .unwrap();
    }
    context.decode(&mut prefill).unwrap();
    let control = GenerationControl::default();
    let (sender, mut receiver) = bounded_generation_stream(control.clone());
    let mut executor = GenerationExecutor::new_native(
        &model,
        NativeGenerationConfig {
            params: &params,
            capabilities: SamplerCapabilities::pinned_revision(),
            grammar: None,
            top_logprobs: 0,
            prompt_tokens: prompt.len(),
        },
        StopEvaluator::new(vec![stop.clone()], Vec::new(), 3).unwrap(),
        sender,
        control,
    )
    .unwrap();
    let mut sampler = executor.begin_speculative_sampler().unwrap();
    let first_live = executor
        .probe_distribution(
            &mut sampler,
            &mut context,
            i32::try_from(prompt.len() - 1).unwrap(),
        )
        .unwrap();
    assert_eq!(first_live.selected_token, first.selected_token);
    let fork = TargetKvFork::begin(&mut context, 0, 1, 1).unwrap();
    let mut verification = Batch::tokens(1, 1).unwrap();
    let rows = fork
        .append_proposals(&mut verification, &[first_live.selected_token])
        .unwrap();
    context.decode(&mut verification).unwrap();
    sampler.accept_proposal(first_live.selected_token).unwrap();
    let second_live = executor
        .probe_distribution(&mut sampler, &mut context, rows[0])
        .unwrap();
    assert_eq!(second_live.selected_token, second.selected_token);
    let preview = executor
        .preview_external_bundle(
            &model,
            &[
                ExternalBundleToken {
                    token_id: first_live.selected_token,
                    distribution: first_live.distribution,
                    sampler_probe_index: 0,
                },
                ExternalBundleToken {
                    token_id: second_live.selected_token,
                    distribution: second_live.distribution,
                    sampler_probe_index: 1,
                },
            ],
        )
        .unwrap();
    assert_eq!(preview.output_tokens(), 2);
    assert_eq!(preview.kv_tokens(), 1);
    assert_eq!(
        preview.terminal_reason(),
        Some(&StopReason::StopString(stop.clone()))
    );
    fork.commit(&mut context, preview.kv_tokens()).unwrap();
    let outcome = executor
        .commit_external_bundle_try(sampler, preview)
        .unwrap();
    assert_eq!(outcome.output_tokens, 2);
    assert_eq!(outcome.kv_tokens, 1);
    assert_eq!(context.memory_seq_pos_max(0).unwrap(), prompt.len() as i32);
    assert_eq!(context.memory_seq_pos_max(1).unwrap(), -1);
    assert_eq!(executor.usage().completion_tokens, 2);
    let events = std::iter::from_fn(|| receiver.try_recv().unwrap()).collect::<Vec<_>>();
    assert_eq!(
        events.len(),
        1,
        "matched stop bytes must never leak as deltas"
    );
    assert!(matches!(
        &events[0],
        GenerationEvent::Finished {
            reason: StopReason::StopString(value),
            usage,
            ..
        } if value == &stop && usage.completion_tokens == 2
    ));
}

#[test]
fn native_vocabulary_fingerprint_is_stable_and_semantic() {
    let model = governed_model();
    let fingerprint = model.vocabulary_fingerprint().unwrap();
    assert_eq!(fingerprint.len(), 64);
    assert!(fingerprint.bytes().all(|byte| byte.is_ascii_hexdigit()));
    assert_eq!(fingerprint, model.vocabulary_fingerprint().unwrap());
}

#[test]
fn configured_native_draft_pair_fails_closed_on_missing_or_mismatched_identity() {
    let target = governed_model();
    let fingerprint = target.vocabulary_fingerprint().unwrap();
    let record = ModelRecord {
        id: "target".into(),
        path: governed_path(),
        aliases: Vec::new(),
        draft_pair: Some(DraftPair {
            draft_model_id: "draft".into(),
            minimum_context: Some(128),
            vocabulary_fingerprint: Some(fingerprint.clone()),
        }),
    };
    let compatibility = DraftModelCompatibility {
        model_id: "draft".into(),
        vocabulary_fingerprint: fingerprint.clone(),
        context_capacity: 256,
    };
    assert_eq!(
        resolve_draft_mode(&record, &fingerprint, 256, Some(&compatibility)).unwrap(),
        DraftMode::DraftModel("draft".into())
    );
    assert!(resolve_draft_mode(&record, &fingerprint, 256, None).is_err());
    let mismatched = DraftModelCompatibility {
        vocabulary_fingerprint: "0".repeat(64),
        ..compatibility
    };
    assert!(resolve_draft_mode(&record, &fingerprint, 256, Some(&mismatched)).is_err());
}

#[test]
fn native_draft_only_bias_forces_exact_target_minus_draft_residual_sample() {
    let target = governed_model();
    let history = target
        .tokenize("forced native residual", true, false)
        .unwrap();
    let mut target_context = target.context_with(context_options(1)).unwrap();
    let mut prefill = Batch::tokens(i32::try_from(history.len()).unwrap(), 1).unwrap();
    for (position, token) in history.iter().enumerate() {
        prefill
            .add_token(
                *token,
                i32::try_from(position).unwrap(),
                &[0],
                position + 1 == history.len(),
            )
            .unwrap();
    }
    target_context.decode(&mut prefill).unwrap();
    let target_params = SamplerParams {
        temperature: 1.0,
        seed: 7,
        ..SamplerParams::default()
    };
    let control = GenerationControl::default();
    let (sender, _receiver) = bounded_generation_stream(control.clone());
    let target_executor = GenerationExecutor::new_native(
        &target,
        NativeGenerationConfig {
            params: &target_params,
            capabilities: SamplerCapabilities::pinned_revision(),
            grammar: None,
            top_logprobs: 0,
            prompt_tokens: history.len(),
        },
        StopEvaluator::new(Vec::new(), Vec::new(), 2).unwrap(),
        sender,
        control,
    )
    .unwrap();
    let mut target_sampler = target_executor.begin_speculative_sampler().unwrap();
    let target_probe = target_executor
        .probe_distribution(
            &mut target_sampler,
            &mut target_context,
            i32::try_from(history.len() - 1).unwrap(),
        )
        .unwrap();
    let biased_token = (0..i32::try_from(target.vocab_size()).unwrap())
        .find(|token| target_probe.distribution.probability(*token) == 0.0)
        .expect("bounded target transform must omit at least one vocabulary token");

    let mut draft_backend = NativeDraftBackend::load(&governed_path(), context_options(1)).unwrap();
    let mut biased_params = target_params;
    biased_params.logit_bias = BTreeMap::from([(biased_token, 100.0)]);
    draft_backend
        .configure_sequence(0, &biased_params, None)
        .unwrap();
    let job = DraftJob::new(0, 1, &history, 1, target.vocab_size()).unwrap();
    let proposed = draft_backend.propose_tokens(&job).unwrap();
    assert_eq!(proposed.len(), 1);
    assert_eq!(proposed[0].token_id, biased_token);
    assert_eq!(target_probe.distribution.probability(biased_token), 0.0);
    let proposal = DraftProposal::new(DraftMode::DraftModel("draft".into()), proposed).unwrap();
    let verification =
        TargetVerification::new(&proposal, vec![target_probe.clone(), target_probe.clone()])
            .unwrap();
    let plan = SpeculationPlan::new(DraftMode::DraftModel("draft".into()), 7).unwrap();
    let decision = plan.decide(&proposal, &verification).unwrap();
    assert!(decision.rejected);
    assert_eq!(decision.accepted, 0);

    let mut residual = BTreeMap::<i32, f64>::new();
    for candidate in target_probe.distribution.candidates() {
        residual.insert(candidate.token_id, f64::from(candidate.probability));
    }
    for candidate in proposal.tokens[0].draft.candidates() {
        *residual.entry(candidate.token_id).or_default() -= f64::from(candidate.probability);
    }
    let total = residual.values().map(|value| value.max(0.0)).sum::<f64>();
    let sample = decision_unit(7, 1);
    let mut cumulative = 0.0_f64;
    let mut expected = None;
    let mut final_positive = None;
    for (token, probability) in residual {
        if probability <= 0.0 {
            continue;
        }
        final_positive = Some(token);
        cumulative += f64::from((probability / total) as f32);
        if sample < cumulative {
            expected = Some(token);
            break;
        }
    }
    assert_eq!(decision.pending_token, expected.or(final_positive).unwrap());
    draft_backend.remove_sequence(0).unwrap();
}

fn decision_unit(seed: u64, index: usize) -> f64 {
    let mut state = seed;
    let mut unit = 0.0;
    for _ in 0..=index {
        state = state.wrapping_add(0x9E37_79B9_7F4A_7C15);
        let mut value = state;
        value = (value ^ (value >> 30)).wrapping_mul(0xBF58_476D_1CE4_E5B9);
        value = (value ^ (value >> 27)).wrapping_mul(0x94D0_49BB_1331_11EB);
        value ^= value >> 31;
        unit = (value >> 11) as f64 * (1.0 / ((1_u64 << 53) as f64));
    }
    unit
}

#[test]
fn isolated_native_draft_backend_produces_complete_versioned_rows() {
    let target = governed_model();
    let history = target.tokenize("draft actor history", true, false).unwrap();
    let mut backend = NativeDraftBackend::load(&governed_path(), context_options(1)).unwrap();
    assert!(backend.context_capacity() >= 256);
    assert_eq!(backend.vocabulary_size(), target.vocab_size());
    backend
        .configure_sequence(
            0,
            &SamplerParams {
                temperature: 0.0,
                ..SamplerParams::default()
            },
            None,
        )
        .unwrap();
    let job = DraftJob::new(0, 1, &history, 2, target.vocab_size()).unwrap();
    let proposal = backend.propose_tokens(&job).unwrap();
    assert_eq!(proposal.len(), 2);
    assert!(proposal
        .iter()
        .all(|token| !token.draft.candidates().is_empty()
            && token.draft.candidates().len() <= target.vocab_size()
            && token.draft.probability(token.token_id) > 0.0));
    backend.remove_sequence(0).unwrap();
}

#[test]
fn draft_history_sync_failure_restores_prior_kv_and_sampler_state() {
    let target = governed_model();
    let token = target.beginning_token();
    let mut backend = NativeDraftBackend::load(&governed_path(), context_options(1)).unwrap();
    backend
        .configure_sequence(
            0,
            &SamplerParams {
                temperature: 0.0,
                ..SamplerParams::default()
            },
            None,
        )
        .unwrap();

    let baseline = DraftJob::new(0, 1, &[token, token], 1, target.vocab_size()).unwrap();
    assert_eq!(backend.propose_tokens(&baseline).unwrap().len(), 1);
    assert_eq!(backend.sequence_position(0).unwrap(), 0);

    let invalid_params = SamplerParams {
        temperature: f32::NAN,
        ..SamplerParams::default()
    };
    assert!(backend
        .configure_sequence(0, &invalid_params, None)
        .is_err());
    assert_eq!(
        backend.sequence_position(0).unwrap(),
        0,
        "sampler construction failure must not clear the existing draft sequence"
    );
    let after_configuration_failure =
        DraftJob::new(0, 2, &[token, token], 1, target.vocab_size()).unwrap();
    assert_eq!(
        backend
            .propose_tokens(&after_configuration_failure)
            .unwrap()
            .len(),
        1
    );

    let overflowing_history = vec![token; backend.context_capacity() + 32];
    let overflowing = DraftJob::new(0, 3, &overflowing_history, 1, target.vocab_size()).unwrap();
    assert!(backend.propose_tokens(&overflowing).is_err());
    assert_eq!(
        backend.sequence_position(0).unwrap(),
        0,
        "failed history synchronization must restore the previous committed prefix"
    );

    let recovered = DraftJob::new(0, 4, &[token, token], 1, target.vocab_size()).unwrap();
    assert_eq!(backend.propose_tokens(&recovered).unwrap().len(), 1);
    assert_eq!(backend.sequence_position(0).unwrap(), 0);
    backend.remove_sequence(0).unwrap();
}
