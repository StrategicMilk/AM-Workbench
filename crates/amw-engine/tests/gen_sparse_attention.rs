#[test]
fn generation_api_does_not_advertise_a_fake_attention_override() {
    let source = include_str!("../src/gen/execution.rs");
    assert!(!source.contains("AttentionRoute"));
    assert!(!source.contains("prepare_attention"));
    assert!(source.contains("model graph selects"));
}
