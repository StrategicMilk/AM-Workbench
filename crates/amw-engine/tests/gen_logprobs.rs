use amw_engine::gen::{capture_top_logprobs, GenError, LogitCandidate};

#[test]
fn capture_derives_normalized_logprobs_before_top_k_truncation() {
    let candidates = vec![
        LogitCandidate {
            token_id: 1,
            bytes: b"a".to_vec(),
            logit: 0.0,
        },
        LogitCandidate {
            token_id: 2,
            bytes: b"b".to_vec(),
            logit: 2.0,
        },
        LogitCandidate {
            token_id: 3,
            bytes: b"c".to_vec(),
            logit: 1.0,
        },
    ];
    assert_eq!(
        capture_top_logprobs(false, 2, candidates.clone()).unwrap(),
        None
    );
    let captured = capture_top_logprobs(true, 2, candidates).unwrap().unwrap();
    assert_eq!(captured.len(), 2);
    assert_eq!(captured.capacity(), 2);
    assert_eq!(captured[0].token_id, 2);
    assert!(captured.iter().all(|entry| entry.logprob <= 0.0));
    assert!((captured[0].logprob - (-0.40760598)).abs() < 1.0e-5);
}

#[test]
fn malformed_logits_fail_closed() {
    assert_eq!(
        capture_top_logprobs(true, 1, Vec::<LogitCandidate>::new()),
        Err(GenError::InvalidLogits("candidate set is empty"))
    );
    assert!(matches!(
        capture_top_logprobs(
            true,
            1,
            [LogitCandidate {
                token_id: 1,
                bytes: vec![],
                logit: f32::NAN,
            }]
        ),
        Err(GenError::InvalidLogits(_))
    ));
}
