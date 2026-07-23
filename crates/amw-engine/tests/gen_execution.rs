use amw_engine::gen::{
    bounded_generation_stream, DecodeBackend, DistributionCandidate, GenError, GenerationControl,
    GenerationEvent, GenerationExecutor, GenerationPlan, GenerationStep, GenerationUsage,
    SamplerCapabilities, SamplerParams, SamplingResult, StepOutcome, StopEvaluator, StopReason,
    OUTPUT_CHANNEL_CAPACITY,
};

struct Backend {
    token: i32,
}

impl DecodeBackend for Backend {
    fn transform_sample_accept(&mut self, _output_index: i32) -> Result<SamplingResult, GenError> {
        Ok(SamplingResult {
            token_id: self.token,
            probability: if self.token == 2 { 0.731_058_6 } else { 0.0 },
            candidates: vec![
                DistributionCandidate {
                    token_id: 1,
                    logit: 1.0,
                    probability: 0.268_941_43,
                },
                DistributionCandidate {
                    token_id: 2,
                    logit: 2.0,
                    probability: 0.731_058_6,
                },
            ],
        })
    }

    fn accept(&mut self, token: i32) -> Result<(), GenError> {
        if token < 0 {
            return Err(GenError::InvalidLogits("invalid accepted token"));
        }
        self.token = token;
        Ok(())
    }

    fn token_piece(&mut self, token: i32) -> Result<Vec<u8>, GenError> {
        Ok(if token == 2 {
            b"b".to_vec()
        } else {
            b"a".to_vec()
        })
    }
}

#[tokio::test]
async fn executor_consumes_post_transform_distribution_and_emits_terminal() {
    let control = GenerationControl::default();
    let (sender, mut receiver) = bounded_generation_stream(control.clone());
    let plan = GenerationPlan::build(
        &SamplerParams::default(),
        SamplerCapabilities::pinned_revision(),
        2,
    )
    .unwrap();
    let stop = StopEvaluator::new(vec![], vec![], 1).unwrap();
    let mut executor =
        GenerationExecutor::new(Backend { token: 2 }, plan, stop, sender, control, 3);

    assert_eq!(
        executor
            .after_decode(GenerationStep { output_index: 0 })
            .await
            .unwrap(),
        StepOutcome::Finished(StopReason::MaxTokens)
    );
    assert!(matches!(
        receiver.recv().await,
        Some(GenerationEvent::Delta {
            token_id: 2,
            bytes,
            logprob: Some(_),
            top_logprobs,
        }) if bytes == b"b" && top_logprobs.len() == 2
    ));
    assert!(matches!(
        receiver.recv().await,
        Some(GenerationEvent::Finished {
            reason: StopReason::MaxTokens,
            usage,
            confidence: Some(_),
        }) if usage.prompt_tokens == 3 && usage.completion_tokens == 1
    ));
}

#[tokio::test]
async fn selected_probability_drives_confidence_when_top_logprobs_are_disabled() {
    let control = GenerationControl::default();
    let (sender, mut receiver) = bounded_generation_stream(control.clone());
    let plan = GenerationPlan::build(
        &SamplerParams::default(),
        SamplerCapabilities::pinned_revision(),
        0,
    )
    .unwrap();
    let stop = StopEvaluator::new(vec![], vec![], 1).unwrap();
    let mut executor =
        GenerationExecutor::new(Backend { token: 2 }, plan, stop, sender, control, 3);

    assert_eq!(
        executor
            .after_decode(GenerationStep { output_index: 0 })
            .await
            .unwrap(),
        StepOutcome::Finished(StopReason::MaxTokens)
    );
    assert!(matches!(
        receiver.recv().await,
        Some(GenerationEvent::Delta {
            token_id: 2,
            logprob: Some(logprob),
            top_logprobs,
            ..
        }) if top_logprobs.is_empty() && logprob < 0.0
    ));
    assert!(matches!(
        receiver.recv().await,
        Some(GenerationEvent::Finished {
            confidence: Some(confidence),
            ..
        }) if (confidence - 0.731_058_6).abs() < 1e-6
    ));
}

#[tokio::test]
async fn backend_contract_failure_is_emitted_as_request_local_failure() {
    let control = GenerationControl::default();
    let (sender, mut receiver) = bounded_generation_stream(control.clone());
    let plan = GenerationPlan::build(
        &SamplerParams::default(),
        SamplerCapabilities::pinned_revision(),
        1,
    )
    .unwrap();
    let stop = StopEvaluator::new(vec![], vec![], 2).unwrap();
    let mut executor =
        GenerationExecutor::new(Backend { token: 99 }, plan, stop, sender, control, 1);
    assert!(matches!(
        executor
            .after_decode(GenerationStep { output_index: 0 })
            .await,
        Err(GenError::InvalidLogits(_))
    ));
    assert!(matches!(
        receiver.recv().await,
        Some(GenerationEvent::Failed(GenError::InvalidLogits(_)))
    ));
}

#[tokio::test]
async fn one_control_authority_terminates_before_the_next_sample() {
    let control = GenerationControl::default();
    let (sender, mut receiver) = bounded_generation_stream(control.clone());
    let plan = GenerationPlan::build(
        &SamplerParams::default(),
        SamplerCapabilities::pinned_revision(),
        0,
    )
    .unwrap();
    let stop = StopEvaluator::new(vec![], vec![], 2).unwrap();
    let mut executor =
        GenerationExecutor::new(Backend { token: 2 }, plan, stop, sender, control.clone(), 1);
    control.cancel();
    assert_eq!(
        executor
            .after_decode(GenerationStep { output_index: 0 })
            .await
            .unwrap(),
        StepOutcome::Finished(StopReason::Cancelled)
    );
    assert!(matches!(
        receiver.recv().await,
        Some(GenerationEvent::Finished {
            reason: StopReason::Cancelled,
            ..
        })
    ));
}

#[test]
fn nonblocking_backpressure_terminates_only_the_slow_sequence() {
    let slow_control = GenerationControl::default();
    let (slow_sender, _slow_receiver) = bounded_generation_stream(slow_control.clone());
    let fill_sender = slow_sender.clone();
    for _ in 0..OUTPUT_CHANNEL_CAPACITY {
        fill_sender
            .try_send(GenerationEvent::Finished {
                reason: StopReason::MaxTokens,
                usage: GenerationUsage::default(),
                confidence: None,
            })
            .unwrap();
    }
    let plan = GenerationPlan::build(
        &SamplerParams::default(),
        SamplerCapabilities::pinned_revision(),
        0,
    )
    .unwrap();
    let mut slow = GenerationExecutor::new(
        Backend { token: 2 },
        plan.clone(),
        StopEvaluator::new(vec![], vec![], 2).unwrap(),
        slow_sender,
        slow_control,
        1,
    );

    assert_eq!(
        slow.after_decode_try(GenerationStep { output_index: 0 }),
        Err(GenError::Backpressure)
    );
    assert_eq!(slow.usage().completion_tokens, 1);

    let fast_control = GenerationControl::default();
    let (fast_sender, mut fast_receiver) = bounded_generation_stream(fast_control.clone());
    let mut fast = GenerationExecutor::new(
        Backend { token: 2 },
        plan,
        StopEvaluator::new(vec![], vec![], 1).unwrap(),
        fast_sender,
        fast_control,
        1,
    );
    assert_eq!(
        fast.after_decode_try(GenerationStep { output_index: 0 })
            .unwrap(),
        StepOutcome::Finished(StopReason::MaxTokens)
    );
    assert!(matches!(
        fast_receiver.try_recv().unwrap(),
        Some(GenerationEvent::Delta { .. })
    ));
    assert!(matches!(
        fast_receiver.try_recv().unwrap(),
        Some(GenerationEvent::Finished { .. })
    ));
}

#[test]
fn nonblocking_executor_honors_exact_one_and_multi_token_limits() {
    for max_tokens in [1_usize, 3] {
        let control = GenerationControl::default();
        let (sender, mut receiver) = bounded_generation_stream(control.clone());
        let plan = GenerationPlan::build(
            &SamplerParams::default(),
            SamplerCapabilities::pinned_revision(),
            0,
        )
        .unwrap();
        let mut executor = GenerationExecutor::new(
            Backend { token: 2 },
            plan,
            StopEvaluator::new(vec![], vec![], max_tokens).unwrap(),
            sender,
            control,
            4,
        );

        for step in 0..max_tokens {
            let outcome = executor
                .after_decode_try(GenerationStep { output_index: 0 })
                .unwrap();
            if step + 1 == max_tokens {
                assert_eq!(outcome, StepOutcome::Finished(StopReason::MaxTokens));
            } else {
                assert_eq!(outcome, StepOutcome::Continue { token_id: 2 });
            }
        }
        assert_eq!(
            executor.usage(),
            GenerationUsage {
                prompt_tokens: 4,
                completion_tokens: max_tokens,
            }
        );
        assert_eq!(executor.last_sampled_token(), Some(2));
        let mut deltas = 0;
        let mut finished = 0;
        let mut failure = None;
        while let Some(event) = receiver.try_recv().unwrap() {
            match event {
                GenerationEvent::Delta { .. } => deltas += 1,
                GenerationEvent::Finished { usage, .. } => {
                    finished += 1;
                    assert_eq!(usage.completion_tokens, max_tokens);
                }
                GenerationEvent::Failed(error) => failure = Some(error),
            }
        }
        assert_eq!(deltas, max_tokens);
        assert_eq!(finished, 1);
        assert!(
            failure.is_none(),
            "unexpected generation failure: {failure:?}"
        );
    }
}

#[test]
fn predecode_control_sweep_finishes_without_sampling_an_extra_token() {
    let control = GenerationControl::default();
    let (sender, mut receiver) = bounded_generation_stream(control.clone());
    let plan = GenerationPlan::build(
        &SamplerParams::default(),
        SamplerCapabilities::pinned_revision(),
        0,
    )
    .unwrap();
    let executor = GenerationExecutor::new(
        Backend { token: 2 },
        plan,
        StopEvaluator::new(vec![], vec![], 4).unwrap(),
        sender,
        control.clone(),
        5,
    );

    control.cancel();
    assert_eq!(
        executor.finish_from_control_try().unwrap(),
        Some(StepOutcome::Finished(StopReason::Cancelled))
    );
    assert_eq!(executor.last_sampled_token(), None);
    assert_eq!(
        executor.usage(),
        GenerationUsage {
            prompt_tokens: 5,
            completion_tokens: 0,
        }
    );
    assert!(matches!(
        receiver.try_recv().unwrap(),
        Some(GenerationEvent::Finished {
            reason: StopReason::Cancelled,
            usage: GenerationUsage {
                completion_tokens: 0,
                ..
            },
            ..
        })
    ));
}
