#![cfg(any(feature = "cpu", feature = "cuda"))]

use std::path::PathBuf;

use amw_engine::{
    ffi::{Batch, ContextOptions, EmbeddingPooling, Model},
    gen::{
        assemble_infill, bounded_generation_stream, FimTokenMap, GenError, GenerationControl,
        GenerationEvent, GenerationExecutor, GenerationStep, ModelFamily, NativeGenerationConfig,
        SamplerCapabilities, SamplerParams, StepOutcome, StopEvaluator, StopReason,
    },
};

fn governed_fixture(environment_variable: &str) -> PathBuf {
    let path = std::env::var_os(environment_variable)
        .map(PathBuf::from)
        .expect("governed native fixture environment variable must be set");
    assert!(path.is_file(), "{environment_variable} must name a file");
    path
}

fn context_options() -> ContextOptions {
    ContextOptions {
        context_tokens: 128,
        batch_tokens: 128,
        micro_batch_tokens: 128,
        max_sequences: 1,
        unified_kv: false,
        embeddings: false,
        pooling: None::<EmbeddingPooling>,
    }
}

#[test]
fn incapable_native_model_returns_exact_typed_fim_refusal() {
    let model = Model::load(&governed_fixture("AMW_ENGINE_NATIVE_TEST_MODEL")).unwrap();
    assert!(matches!(
        FimTokenMap::from_model(ModelFamily::QwenCoder, &model),
        Err(GenError::FimUnsupported)
    ));
}

#[test]
fn capable_native_model_executes_exact_fim_prompt_and_stops_at_bound() {
    let model = Model::load(&governed_fixture("AMW_ENGINE_NATIVE_FIM_TEST_MODEL")).unwrap();
    let sentinels = FimTokenMap::from_model(ModelFamily::QwenCoder, &model)
        .expect("the governed coder fixture must expose complete native FIM metadata");
    let prefix = model
        .tokenize("def answer():\n    ", false, false)
        .expect("native prefix tokenization must succeed");
    let suffix = model
        .tokenize("\n", false, false)
        .expect("native suffix tokenization must succeed");
    let prompt = assemble_infill(Some(sentinels), &prefix, &suffix).unwrap();

    assert_eq!(prompt[0], sentinels.prefix());
    assert_eq!(prompt[1..1 + prefix.len()], prefix);
    assert_eq!(prompt[1 + prefix.len()], sentinels.suffix());
    assert_eq!(
        prompt[2 + prefix.len()..2 + prefix.len() + suffix.len()],
        suffix
    );
    assert_eq!(prompt.last(), Some(&sentinels.middle()));

    let mut context = model.context_with(context_options()).unwrap();
    let mut prefill = Batch::tokens(i32::try_from(prompt.len()).unwrap(), 1).unwrap();
    for (position, token) in prompt.iter().copied().enumerate() {
        prefill
            .add_token(
                token,
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
        StopEvaluator::new(vec![], vec![], 1).unwrap(),
        sender,
        control,
    )
    .unwrap();

    assert_eq!(
        executor
            .after_native_decode_try(
                &model,
                &mut context,
                GenerationStep {
                    output_index: i32::try_from(prompt.len() - 1).unwrap(),
                },
            )
            .unwrap(),
        StepOutcome::Finished(StopReason::MaxTokens)
    );
    assert!(matches!(
        receiver.try_recv().unwrap(),
        Some(GenerationEvent::Delta { bytes, .. }) if !bytes.is_empty()
    ));
    assert!(matches!(
        receiver.try_recv().unwrap(),
        Some(GenerationEvent::Finished { reason: StopReason::MaxTokens, usage, .. })
            if usage.prompt_tokens == prompt.len() && usage.completion_tokens == 1
    ));
}
