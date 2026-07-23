use std::collections::BTreeMap;

use amw_engine::gen::{GenError, SamplerCapabilities, SamplerChain, SamplerParams, SamplerStage};

struct UnavailableFeatureCase {
    expected_parameter: &'static str,
    params: SamplerParams,
    disable: fn(&mut SamplerCapabilities),
}

#[test]
fn fixed_seed_is_deterministic_for_one_sequence() {
    let params = SamplerParams {
        seed: 73,
        dry_multiplier: 0.7,
        xtc_probability: 0.2,
        top_n_sigma: 1.0,
        ..Default::default()
    };
    let chain = SamplerChain::build(&params, SamplerCapabilities::pinned_revision()).unwrap();
    let first: Vec<_> = (0..20)
        .map(|step| chain.deterministic_index(100, step).unwrap())
        .collect();
    let second: Vec<_> = (0..20)
        .map(|step| chain.deterministic_index(100, step).unwrap())
        .collect();
    assert_eq!(first, second);
    assert!(chain.stages.contains(&SamplerStage::Dry));
    assert!(chain.stages.contains(&SamplerStage::Xtc));
    assert!(chain.stages.contains(&SamplerStage::TopNSigma));
}

#[test]
fn every_active_optional_sampler_feature_fails_closed_when_unavailable() {
    let cases = [
        UnavailableFeatureCase {
            expected_parameter: "typical_p",
            params: SamplerParams {
                typical_p: 0.9,
                ..Default::default()
            },
            disable: |caps| caps.typical = false,
        },
        UnavailableFeatureCase {
            expected_parameter: "logit_bias",
            params: SamplerParams {
                logit_bias: BTreeMap::from([(1, 0.5)]),
                ..Default::default()
            },
            disable: |caps| caps.logit_bias = false,
        },
        UnavailableFeatureCase {
            expected_parameter: "mirostat",
            params: SamplerParams {
                mirostat_mode: 1,
                ..Default::default()
            },
            disable: |caps| caps.mirostat = false,
        },
        UnavailableFeatureCase {
            expected_parameter: "dry_multiplier",
            params: SamplerParams {
                dry_multiplier: 0.5,
                ..Default::default()
            },
            disable: |caps| caps.dry = false,
        },
        UnavailableFeatureCase {
            expected_parameter: "xtc_probability",
            params: SamplerParams {
                xtc_probability: 0.5,
                ..Default::default()
            },
            disable: |caps| caps.xtc = false,
        },
        UnavailableFeatureCase {
            expected_parameter: "top_n_sigma",
            params: SamplerParams {
                top_n_sigma: 1.0,
                ..Default::default()
            },
            disable: |caps| caps.top_n_sigma = false,
        },
    ];

    for case in cases {
        let mut capabilities = SamplerCapabilities::pinned_revision();
        (case.disable)(&mut capabilities);
        let result = SamplerChain::build(&case.params, capabilities);
        assert_eq!(
            result,
            Err(GenError::UnsupportedParam(case.expected_parameter)),
            "{} must be rejected exactly instead of silently dropping its stage",
            case.expected_parameter,
        );
    }
}

#[test]
fn native_width_and_mirostat_mode_fail_before_ffi_construction() {
    let oversized_seed = SamplerParams {
        seed: u64::from(u32::MAX) + 1,
        ..Default::default()
    };
    assert!(matches!(
        SamplerChain::build(&oversized_seed, SamplerCapabilities::pinned_revision()),
        Err(GenError::InvalidSamplerParam("seed", _))
    ));

    let invalid_mode = SamplerParams {
        mirostat_mode: 3,
        ..Default::default()
    };
    assert!(matches!(
        SamplerChain::build(&invalid_mode, SamplerCapabilities::pinned_revision()),
        Err(GenError::InvalidSamplerParam("mirostat_mode", _))
    ));
}

#[test]
fn every_sampler_field_is_validated_before_stage_selection() {
    let mut invalid = Vec::new();

    invalid.push((
        "temperature",
        SamplerParams {
            temperature: f32::NAN,
            ..Default::default()
        },
    ));
    invalid.push((
        "top_k",
        SamplerParams {
            top_k: u32::MAX,
            ..Default::default()
        },
    ));
    invalid.push((
        "top_p",
        SamplerParams {
            top_p: 1.01,
            ..Default::default()
        },
    ));
    invalid.push((
        "min_p",
        SamplerParams {
            min_p: -0.01,
            ..Default::default()
        },
    ));
    invalid.push((
        "typical_p",
        SamplerParams {
            typical_p: f32::INFINITY,
            ..Default::default()
        },
    ));
    invalid.push((
        "repetition_penalty",
        SamplerParams {
            repetition_penalty: 0.0,
            ..Default::default()
        },
    ));
    invalid.push((
        "presence_penalty",
        SamplerParams {
            presence_penalty: 2.1,
            ..Default::default()
        },
    ));
    invalid.push((
        "frequency_penalty",
        SamplerParams {
            frequency_penalty: f32::NEG_INFINITY,
            ..Default::default()
        },
    ));
    invalid.push((
        "mirostat_tau",
        SamplerParams {
            mirostat_tau: 0.0,
            ..Default::default()
        },
    ));
    invalid.push((
        "mirostat_eta",
        SamplerParams {
            mirostat_eta: 1.1,
            ..Default::default()
        },
    ));
    invalid.push((
        "dry_multiplier",
        SamplerParams {
            dry_multiplier: -0.1,
            ..Default::default()
        },
    ));
    invalid.push((
        "dry_base",
        SamplerParams {
            dry_base: 0.9,
            ..Default::default()
        },
    ));
    invalid.push((
        "dry_allowed_length",
        SamplerParams {
            dry_allowed_length: 0,
            ..Default::default()
        },
    ));
    invalid.push((
        "xtc_probability",
        SamplerParams {
            xtc_probability: 1.1,
            ..Default::default()
        },
    ));
    invalid.push((
        "xtc_threshold",
        SamplerParams {
            xtc_threshold: f32::NAN,
            ..Default::default()
        },
    ));
    invalid.push((
        "top_n_sigma",
        SamplerParams {
            top_n_sigma: -0.1,
            ..Default::default()
        },
    ));
    invalid.push((
        "logit_bias",
        SamplerParams {
            logit_bias: BTreeMap::from([(1, f32::INFINITY)]),
            ..Default::default()
        },
    ));
    invalid.push((
        "logit_bias",
        SamplerParams {
            logit_bias: BTreeMap::from([(-1, 0.0)]),
            ..Default::default()
        },
    ));

    for (name, params) in invalid {
        assert!(
            matches!(
                SamplerChain::build(&params, SamplerCapabilities::pinned_revision()),
                Err(GenError::InvalidSamplerParam(actual, _)) if actual == name
            ),
            "{name} must fail before stage selection"
        );
    }
}

#[test]
fn logit_bias_ids_are_checked_against_the_loaded_vocabulary() {
    let params = SamplerParams {
        logit_bias: BTreeMap::from([(9, 1.0)]),
        ..Default::default()
    };
    params
        .validate(SamplerCapabilities::pinned_revision())
        .unwrap();
    assert!(matches!(
        params.validate_for_vocab(9),
        Err(GenError::InvalidSamplerParam("logit_bias", _))
    ));
    params.validate_for_vocab(10).unwrap();
}

#[test]
fn stage_order_matches_the_pinned_llama_default_chain() {
    let params = SamplerParams {
        repetition_penalty: 1.1,
        dry_multiplier: 0.7,
        top_n_sigma: 1.0,
        typical_p: 0.9,
        xtc_probability: 0.2,
        ..Default::default()
    };
    let chain = SamplerChain::build(&params, SamplerCapabilities::pinned_revision()).unwrap();
    assert_eq!(
        chain.stages,
        vec![
            SamplerStage::Penalties,
            SamplerStage::Dry,
            SamplerStage::TopNSigma,
            SamplerStage::TopK,
            SamplerStage::Typical,
            SamplerStage::TopP,
            SamplerStage::MinP,
            SamplerStage::Xtc,
            SamplerStage::Temperature,
            SamplerStage::Distribution,
        ]
    );
}
