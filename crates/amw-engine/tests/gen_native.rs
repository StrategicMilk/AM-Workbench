#![cfg(any(feature = "cpu", feature = "cuda"))]

use std::{collections::BTreeMap, path::PathBuf};

use amw_engine::{
    ffi::{Batch, ContextOptions, EmbeddingPooling, Model},
    gen::{
        bounded_generation_stream, execute_embedding_batch, CompiledGrammar, EmbeddingInput,
        EmbeddingOptions, GenerationControl, GenerationEvent, GenerationExecutor, GenerationStep,
        NativeEmbeddingBackend, NativeGenerationConfig, SamplerCapabilities, SamplerChain,
        SamplerParams, StepOutcome, StopEvaluator, StopReason,
    },
};

fn fixture() -> PathBuf {
    let path = std::env::var_os("AMW_ENGINE_NATIVE_TEST_MODEL")
        .map(PathBuf::from)
        .expect("AMW_ENGINE_NATIVE_TEST_MODEL must name the governed pinned GGUF fixture");
    assert!(path.is_file(), "native GGUF fixture must exist");
    path
}

fn context_options(embeddings: bool) -> ContextOptions {
    ContextOptions {
        context_tokens: 128,
        batch_tokens: 128,
        micro_batch_tokens: 128,
        max_sequences: 4,
        unified_kv: false,
        embeddings,
        pooling: embeddings.then_some(EmbeddingPooling::None),
    }
}

#[test]
fn native_generation_applies_grammar_samples_and_emits_exact_bytes() {
    let model = Model::load(&fixture()).unwrap();
    let mut context = model.context_with(context_options(false)).unwrap();
    let prompt = vec![model.beginning_token()];
    assert!(!prompt.is_empty());
    let mut batch = Batch::tokens(i32::try_from(prompt.len()).unwrap(), 1).unwrap();
    for (position, token) in prompt.iter().enumerate() {
        batch
            .add_token(
                *token,
                i32::try_from(position).unwrap(),
                &[0],
                position + 1 == prompt.len(),
            )
            .unwrap();
    }
    context.decode(&mut batch).unwrap();

    let grammar = CompiledGrammar::compile("root ::= \"\\n\"").unwrap();
    let params = SamplerParams {
        temperature: 0.0,
        top_k: 0,
        top_p: 1.0,
        min_p: 0.0,
        ..Default::default()
    };
    let control = GenerationControl::default();
    let (sender, mut receiver) = bounded_generation_stream(control.clone());
    let stop = StopEvaluator::new(vec![], vec![], 1).unwrap();
    let output_index = i32::try_from(prompt.len() - 1).unwrap();
    let mut executor = GenerationExecutor::new_native(
        &model,
        NativeGenerationConfig {
            params: &params,
            capabilities: SamplerCapabilities::pinned_revision(),
            grammar: Some(&grammar),
            top_logprobs: 2,
            prompt_tokens: prompt.len(),
        },
        stop,
        sender,
        control,
    )
    .unwrap();
    assert_eq!(
        executor
            .after_native_decode_try(&model, &mut context, GenerationStep { output_index })
            .unwrap(),
        StepOutcome::Finished(StopReason::MaxTokens)
    );

    let delta = receiver.try_recv().unwrap().unwrap();
    assert!(matches!(
        delta,
        GenerationEvent::Delta { bytes, .. } if bytes == b"\n"
    ));
    assert!(matches!(
        receiver.try_recv().unwrap(),
        Some(GenerationEvent::Finished { .. })
    ));
}

#[test]
fn native_generation_emits_exactly_the_requested_multi_token_count() {
    let model = Model::load(&fixture()).unwrap();
    let mut context = model.context_with(context_options(false)).unwrap();
    let prompt = vec![model.beginning_token()];
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
        top_k: 0,
        top_p: 1.0,
        min_p: 0.0,
        ..Default::default()
    };
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
        StopEvaluator::new(vec![], vec![], 2).unwrap(),
        sender,
        control,
    )
    .unwrap();
    let first = executor
        .after_native_decode_try(
            &model,
            &mut context,
            GenerationStep {
                output_index: i32::try_from(prompt.len() - 1).unwrap(),
            },
        )
        .unwrap();
    let StepOutcome::Continue { token_id } = first else {
        unreachable!("first generated token must continue a two-token request");
    };

    let mut decode = Batch::tokens(1, 1).unwrap();
    decode
        .add_token(token_id, i32::try_from(prompt.len()).unwrap(), &[0], true)
        .unwrap();
    context.decode(&mut decode).unwrap();
    assert_eq!(
        executor
            .after_native_decode_try(&model, &mut context, GenerationStep { output_index: 0 },)
            .unwrap(),
        StepOutcome::Finished(StopReason::MaxTokens)
    );

    let mut deltas = 0;
    let mut terminal_usage = None;
    while let Some(event) = receiver.try_recv().unwrap() {
        match event {
            GenerationEvent::Delta { .. } => deltas += 1,
            GenerationEvent::Finished { usage, .. } => terminal_usage = Some(usage),
            GenerationEvent::Failed(error) => {
                assert!(false, "unexpected generation failure: {error}")
            }
        }
    }
    assert_eq!(deltas, 2);
    assert_eq!(terminal_usage.unwrap().completion_tokens, 2);
    assert_eq!(executor.usage().completion_tokens, 2);
}

#[test]
fn native_embedding_rows_are_pooled_and_normalized_in_input_order() {
    let model = Model::load(&fixture()).unwrap();
    let mut context = model.context_with(context_options(true)).unwrap();
    let inputs = [EmbeddingInput {
        tokens: vec![model.beginning_token()],
    }];
    let mut backend = NativeEmbeddingBackend::new(&mut context);
    let vectors =
        execute_embedding_batch(&mut backend, &inputs, EmbeddingOptions::default()).unwrap();
    assert_eq!(vectors.len(), 1);
    assert_eq!(vectors[0].len(), model.embedding_size());
    let norm = vectors[0]
        .iter()
        .map(|value| f64::from(*value).powi(2))
        .sum::<f64>()
        .sqrt();
    assert!((norm - 1.0).abs() < 1.0e-5);
}

#[test]
fn native_post_transform_distribution_proves_bias_temperature_and_grammar() {
    let model = Model::load(&fixture()).unwrap();
    let mut context = model.context_with(context_options(false)).unwrap();
    let mut prefill = Batch::tokens(1, 1).unwrap();
    prefill
        .add_token(model.beginning_token(), 0, &[0], true)
        .unwrap();
    context.decode(&mut prefill).unwrap();
    let unconstrained = SamplerParams {
        temperature: 1.0,
        top_k: 0,
        top_p: 1.0,
        min_p: 0.0,
        typical_p: 1.0,
        seed: 17,
        ..Default::default()
    };
    let mut baseline = SamplerChain::build_native(
        &unconstrained,
        SamplerCapabilities::pinned_revision(),
        &model,
    )
    .unwrap();
    let baseline = baseline.transform_sample_accept(&mut context, 0).unwrap();
    let target = baseline
        .candidates
        .iter()
        .filter(|candidate| candidate.token != baseline.token)
        .max_by(|left, right| left.probability.total_cmp(&right.probability))
        .unwrap();

    let biased_params = SamplerParams {
        logit_bias: BTreeMap::from([(target.token, 10.0)]),
        ..unconstrained.clone()
    };
    let mut biased = SamplerChain::build_native(
        &biased_params,
        SamplerCapabilities::pinned_revision(),
        &model,
    )
    .unwrap();
    let biased = biased.transform_sample_accept(&mut context, 0).unwrap();
    let biased_target = biased
        .candidates
        .iter()
        .find(|candidate| candidate.token == target.token)
        .unwrap();
    assert!(biased_target.probability > target.probability);

    let mut cold = SamplerChain::build_native(
        &SamplerParams {
            temperature: 0.5,
            ..unconstrained.clone()
        },
        SamplerCapabilities::pinned_revision(),
        &model,
    )
    .unwrap();
    let mut hot = SamplerChain::build_native(
        &SamplerParams {
            temperature: 2.0,
            ..unconstrained
        },
        SamplerCapabilities::pinned_revision(),
        &model,
    )
    .unwrap();
    let cold = cold.transform_sample_accept(&mut context, 0).unwrap();
    let hot = hot.transform_sample_accept(&mut context, 0).unwrap();
    let cold_peak = cold
        .candidates
        .iter()
        .map(|candidate| candidate.probability)
        .fold(0.0_f32, f32::max);
    let hot_peak = hot
        .candidates
        .iter()
        .map(|candidate| candidate.probability)
        .fold(0.0_f32, f32::max);
    assert!(cold_peak > hot_peak);

    let grammar = CompiledGrammar::compile("root ::= \"\\n\"").unwrap();
    let mut constrained = SamplerChain::build_native_with_grammar(
        &SamplerParams {
            temperature: 0.0,
            top_k: 0,
            top_p: 1.0,
            min_p: 0.0,
            ..Default::default()
        },
        SamplerCapabilities::pinned_revision(),
        &model,
        Some(&grammar),
    )
    .unwrap();
    let constrained = constrained
        .transform_sample_accept(&mut context, 0)
        .unwrap();
    assert_eq!(model.token_piece(constrained.token, false).unwrap(), b"\n");
    assert!((constrained.probability - 1.0).abs() < 1.0e-6);
    assert_eq!(
        constrained
            .candidates
            .iter()
            .filter(|candidate| candidate.probability > 0.0)
            .count(),
        1
    );
}
