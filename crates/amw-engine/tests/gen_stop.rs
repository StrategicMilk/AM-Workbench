use amw_engine::gen::{GenError, StopDecision, StopEvaluator, StopReason};

#[test]
fn stop_strings_are_withheld_and_never_leak_across_token_boundaries() {
    let mut stop = StopEvaluator::new(vec!["END".into()], vec![], 20).unwrap();
    let first = stop.observe_bytes(1, b"hello E").unwrap();
    assert_eq!(first.emit, b"hello ");
    assert_eq!(first.decision, StopDecision::Continue);
    let second = stop.observe_bytes(2, b"NDignored").unwrap();
    assert!(second.emit.is_empty());
    assert_eq!(
        second.decision,
        StopDecision::Stop(StopReason::StopString("END".into()))
    );
}

#[test]
fn every_content_terminal_family_fires_on_its_boundary() {
    let mut eos = StopEvaluator::new(vec![], vec![7], 20).unwrap();
    assert_eq!(
        eos.observe_bytes(7, b"").unwrap().decision,
        StopDecision::Stop(StopReason::EndToken(7))
    );

    let mut max = StopEvaluator::new(vec![], vec![], 2).unwrap();
    assert_eq!(
        max.observe_bytes(1, b"a").unwrap().decision,
        StopDecision::Continue
    );
    let terminal = max.observe_bytes(2, b"b").unwrap();
    assert_eq!(terminal.emit, b"b");
    assert_eq!(terminal.decision, StopDecision::Stop(StopReason::MaxTokens));
}

#[test]
fn malformed_stop_configuration_fails_closed() {
    assert!(matches!(
        StopEvaluator::new(vec![String::new()], vec![], 10),
        Err(GenError::InvalidStop(_))
    ));
}
