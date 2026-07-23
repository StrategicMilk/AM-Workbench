use amw_engine::gen::{assemble_infill, FimTokenMap, FimTokenMetadata, GenError, ModelFamily};

fn metadata(prefix: Option<i32>, suffix: Option<i32>, middle: Option<i32>) -> FimTokenMetadata {
    FimTokenMetadata {
        family: ModelFamily::StarCoder,
        prefix,
        suffix,
        middle,
    }
}

#[test]
fn infill_requires_complete_distinct_metadata_and_uses_exact_sentinels() {
    let map = FimTokenMap::from_metadata(metadata(Some(100), Some(101), Some(102))).unwrap();
    assert_eq!(
        assemble_infill(Some(map), &[1, 2], &[3]).unwrap(),
        vec![100, 1, 2, 101, 3, 102]
    );
    assert_eq!(
        FimTokenMap::from_metadata(metadata(Some(100), None, Some(102))),
        Err(GenError::FimUnsupported)
    );
    assert!(matches!(
        FimTokenMap::from_metadata(metadata(Some(100), Some(100), Some(102))),
        Err(GenError::InvalidFimSentinels(_))
    ));
    assert!(matches!(
        assemble_infill(Some(map), &[100], &[3]),
        Err(GenError::InvalidFimSentinels(_))
    ));
}

#[test]
fn sentinel_metadata_retains_the_proven_model_family() {
    for family in [
        ModelFamily::CodeLlama,
        ModelFamily::DeepSeekCoder,
        ModelFamily::StarCoder,
        ModelFamily::QwenCoder,
    ] {
        let map = FimTokenMap::from_metadata(FimTokenMetadata {
            family,
            prefix: Some(10),
            suffix: Some(11),
            middle: Some(12),
        })
        .unwrap();
        assert_eq!(map.family(), family);
    }
}
