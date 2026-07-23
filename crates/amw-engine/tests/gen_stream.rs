use amw_engine::gen::{
    bounded_generation_stream, GenError, GenerationControl, GenerationControlState,
    GenerationEvent, GenerationFailureCode, StopReason, MAX_GENERATION_EVENT_BYTES,
    OUTPUT_CHANNEL_CAPACITY,
};

fn delta(bytes: usize) -> GenerationEvent {
    GenerationEvent::Delta {
        token_id: 1,
        bytes: vec![b'x'; bytes],
        logprob: None,
        top_logprobs: vec![],
    }
}

#[tokio::test]
async fn stream_enforces_item_and_byte_bounds_and_detects_disconnect() {
    let control = GenerationControl::default();
    let (sender, mut receiver) = bounded_generation_stream(control.clone());
    for _ in 0..OUTPUT_CHANNEL_CAPACITY {
        sender
            .try_send(GenerationEvent::Finished {
                reason: StopReason::MaxTokens,
                usage: Default::default(),
                confidence: None,
            })
            .unwrap();
    }
    assert_eq!(sender.try_send(delta(1)), Err(GenError::Backpressure));
    assert!(receiver.recv().await.is_some());
    sender.try_send(delta(1)).unwrap();
    drop(receiver);
    assert_eq!(control.state(), GenerationControlState::Disconnected);
    assert_eq!(sender.try_send(delta(1)), Err(GenError::StreamDisconnected));
}

#[test]
fn oversized_single_event_fails_before_queue_allocation() {
    let (sender, _receiver) = bounded_generation_stream(GenerationControl::default());
    assert_eq!(
        sender.try_send(delta(MAX_GENERATION_EVENT_BYTES)),
        Err(GenError::EventTooLarge)
    );
    assert_eq!(sender.queued_bytes(), 0);
}

#[test]
fn retained_byte_budget_applies_before_the_item_count_limit() {
    let (sender, _receiver) = bounded_generation_stream(GenerationControl::default());
    let mut accepted = 0;
    loop {
        match sender.try_send(delta(12 * 1024)) {
            Ok(()) => accepted += 1,
            Err(GenError::Backpressure) => break,
            Err(error) => assert_eq!(error, GenError::Backpressure, "unexpected stream result"),
        }
    }
    assert!(accepted > 0);
    assert!(accepted < OUTPUT_CHANNEL_CAPACITY);
    assert!(sender.queued_bytes() <= 64 * 1024);
}

#[test]
fn event_bundle_reserves_all_item_slots_before_enqueueing_any_event() {
    let (sender, _receiver) = bounded_generation_stream(GenerationControl::default());
    for _ in 0..OUTPUT_CHANNEL_CAPACITY - 1 {
        sender.try_send(delta(1)).unwrap();
    }
    let queued_before = sender.queued_bytes();
    assert_eq!(
        sender.try_send_batch(vec![delta(1), delta(1)]),
        Err(GenError::Backpressure)
    );
    assert_eq!(sender.queued_bytes(), queued_before);
}

#[test]
fn normal_events_cannot_consume_the_authoritative_terminal_slot() {
    let (sender, mut receiver) = bounded_generation_stream(GenerationControl::default());
    for _ in 0..OUTPUT_CHANNEL_CAPACITY {
        sender.try_send(delta(1)).unwrap();
    }
    assert_eq!(sender.try_send(delta(1)), Err(GenError::Backpressure));

    sender
        .try_send(GenerationEvent::Finished {
            reason: StopReason::Cancelled,
            usage: Default::default(),
            confidence: None,
        })
        .expect("the terminal reserve must survive saturated delta backpressure");
    for _ in 0..OUTPUT_CHANNEL_CAPACITY {
        assert!(matches!(
            receiver.try_recv().unwrap(),
            Some(GenerationEvent::Delta { .. })
        ));
    }
    assert!(matches!(
        receiver.try_recv().unwrap(),
        Some(GenerationEvent::Finished {
            reason: StopReason::Cancelled,
            ..
        })
    ));
}

#[test]
fn normal_bytes_cannot_consume_the_authoritative_failure_reserve() {
    let (sender, mut receiver) = bounded_generation_stream(GenerationControl::default());
    let mut deltas = 0;
    loop {
        match sender.try_send(delta(12 * 1024)) {
            Ok(()) => deltas += 1,
            Err(GenError::Backpressure) => break,
            Err(error) => assert_eq!(
                error,
                GenError::Backpressure,
                "unexpected normal-event result"
            ),
        }
    }
    assert!(deltas > 0);
    let mut message = String::with_capacity(MAX_GENERATION_EVENT_BYTES / 2);
    message.push_str("authoritative saturated-stream failure");
    sender
        .try_send(GenerationEvent::Failed(GenError::RuntimeFailure {
            code: GenerationFailureCode::Oom,
            message,
        }))
        .expect("the terminal byte reserve must fit a large typed failure");

    let mut saw_failure = false;
    while let Some(event) = receiver.try_recv().unwrap() {
        saw_failure |= matches!(
            event,
            GenerationEvent::Failed(GenError::RuntimeFailure { .. })
        );
    }
    assert!(saw_failure);
}

#[test]
fn event_bundle_allows_only_one_final_terminal() {
    let (sender, _receiver) = bounded_generation_stream(GenerationControl::default());
    let terminal = || GenerationEvent::Finished {
        reason: StopReason::Cancelled,
        usage: Default::default(),
        confidence: None,
    };

    assert!(matches!(
        sender.try_send_batch(vec![terminal(), delta(1)]),
        Err(GenError::SpeculationInvalid(
            "generation event bundle terminal is not final"
        ))
    ));
    assert!(matches!(
        sender.try_send_batch(vec![terminal(), terminal()]),
        Err(GenError::SpeculationInvalid(
            "generation event bundle contains multiple terminals"
        ))
    ));
    sender
        .try_send_batch(vec![delta(1), terminal()])
        .expect("one final terminal is a valid atomic event bundle");
}

#[tokio::test]
async fn context_overflow_event_retains_only_fixed_width_error_state() {
    let (sender, mut receiver) = bounded_generation_stream(GenerationControl::default());
    let overflow = GenError::ContextOverflow {
        requested: 4_097,
        limit: 4_096,
    };

    sender
        .try_send(GenerationEvent::Failed(overflow.clone()))
        .unwrap();
    assert_eq!(
        sender.queued_bytes(),
        std::mem::size_of::<GenerationEvent>()
    );
    assert_eq!(
        receiver.recv().await,
        Some(GenerationEvent::Failed(overflow))
    );
    assert_eq!(sender.queued_bytes(), 0);
}

#[tokio::test]
async fn typed_runtime_failure_accounts_for_and_releases_its_message_allocation() {
    let (sender, mut receiver) = bounded_generation_stream(GenerationControl::default());
    let mut message = String::with_capacity(128);
    message.push_str("engine is draining");
    let message_capacity = message.capacity();

    sender
        .try_send(GenerationEvent::Failed(GenError::RuntimeFailure {
            code: GenerationFailureCode::Draining,
            message,
        }))
        .unwrap();
    assert_eq!(
        sender.queued_bytes(),
        std::mem::size_of::<GenerationEvent>() + message_capacity
    );
    assert!(matches!(
        receiver.recv().await,
        Some(GenerationEvent::Failed(GenError::RuntimeFailure {
            code: GenerationFailureCode::Draining,
            message,
        })) if message == "engine is draining"
    ));
    assert_eq!(sender.queued_bytes(), 0);
}

#[test]
fn oversized_runtime_failure_is_rejected_before_queueing() {
    let (sender, _receiver) = bounded_generation_stream(GenerationControl::default());
    let mut message = String::with_capacity(MAX_GENERATION_EVENT_BYTES);
    message.push_str("native allocation failed");

    assert_eq!(
        sender.try_send(GenerationEvent::Failed(GenError::RuntimeFailure {
            code: GenerationFailureCode::Oom,
            message,
        })),
        Err(GenError::EventTooLarge)
    );
    assert_eq!(sender.queued_bytes(), 0);
}
