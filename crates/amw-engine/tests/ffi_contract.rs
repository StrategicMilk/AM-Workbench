#![cfg(any(feature = "cpu", feature = "cuda"))]

use std::{path::Path, process::Command};

use amw_engine::{
    ffi::{Batch, ChatMessage, ChatRole, ContextOptions, FfiError, Model, Sampler},
    store::{
        gguf_meta::inspect_gguf,
        template::{TemplatePolicy, TemplateRenderError},
    },
};

#[test]
fn nonexistent_model_path_is_a_typed_error() {
    let path = Path::new("definitely-not-a-real-amw-model.gguf");
    let error = Model::load(path)
        .err()
        .expect("nonexistent model path must fail closed");
    assert_eq!(error, FfiError::ModelNotFound(path.to_owned()));
}

#[test]
fn malformed_existing_model_reaches_ffi_and_returns_typed_error() {
    let temp = tempfile::NamedTempFile::new().expect("temp model file");
    let error = Model::load(temp.path())
        .err()
        .expect("malformed model must fail closed");
    assert_eq!(error, FfiError::ModelLoad(temp.path().to_owned()));
}

#[test]
fn batch_rejects_invalid_dimensions_before_ffi() {
    let error = Batch::new(0, 0, 1)
        .err()
        .expect("zero-capacity batch must fail closed");
    assert_eq!(error, FfiError::BatchCreate);
}

#[test]
fn sampler_constructor_and_drop_are_live() {
    let sampler = Sampler::greedy().expect("greedy sampler must initialize");
    drop(sampler);
}

#[test]
fn batch_modes_reject_cross_mode_writes_before_pointer_access() {
    let mut token_batch = Batch::tokens(2, 1).unwrap();
    assert_eq!(token_batch.token_count(), 0);
    assert_eq!(
        token_batch.add_embedding(&[0.0, 1.0], 0, &[0], true),
        Err(FfiError::BatchMode("token"))
    );

    let mut embedding_batch = Batch::embeddings(2, 2, 1).unwrap();
    assert_eq!(
        embedding_batch.add_token(1, 0, &[0], true),
        Err(FfiError::BatchMode("embedding"))
    );
    embedding_batch
        .add_embedding(&[0.0, 1.0], 0, &[0], true)
        .unwrap();
    assert_eq!(embedding_batch.token_count(), 1);

    assert_eq!(
        token_batch.add_token(1, -1, &[0], true),
        Err(FfiError::InvalidArgument(
            "batch positions and sequence ids must be non-negative"
        ))
    );
    assert_eq!(
        token_batch.add_token(1, 0, &[-1], true),
        Err(FfiError::InvalidArgument(
            "batch positions and sequence ids must be non-negative"
        ))
    );
}

#[test]
fn invalid_native_sequence_and_layout_inputs_return_typed_errors_without_aborting() {
    const CHILD: &str = "AMW_ENGINE_FFI_INVALID_SEQUENCE_CHILD";
    if std::env::var_os(CHILD).is_some() {
        let path = std::env::var_os("AMW_ENGINE_NATIVE_TEST_MODEL")
            .map(std::path::PathBuf::from)
            .expect("governed model fixture");
        let model = Model::load(&path).unwrap();
        let mut context = model
            .context_with(ContextOptions {
                context_tokens: 128,
                batch_tokens: 128,
                micro_batch_tokens: 128,
                max_sequences: 2,
                unified_kv: false,
                embeddings: false,
                pooling: None,
            })
            .unwrap();
        assert!(matches!(
            context.memory_seq_pos_max(2),
            Err(FfiError::InvalidArgument(_))
        ));
        assert!(matches!(
            context.memory_seq_cp(0, 1, 0, 1),
            Err(FfiError::InvalidArgument(_))
        ));
        let mut batch = Batch::tokens(1, 1).unwrap();
        batch
            .add_token(model.beginning_token(), 0, &[2], true)
            .unwrap();
        assert!(matches!(
            context.decode(&mut batch),
            Err(FfiError::InvalidArgument(_))
        ));
        return;
    }

    let status = Command::new(std::env::current_exe().unwrap())
        .args([
            "--exact",
            "invalid_native_sequence_and_layout_inputs_return_typed_errors_without_aborting",
            "--nocapture",
        ])
        .env(CHILD, "1")
        .status()
        .unwrap();
    assert!(status.success(), "invalid FFI preflight child aborted");
}

#[test]
fn governed_model_tokenizer_succeeds_nonempty_and_round_trips_meaningful_bytes() {
    let path = std::env::var_os("AMW_ENGINE_NATIVE_TEST_MODEL")
        .map(std::path::PathBuf::from)
        .expect("AMW_ENGINE_NATIVE_TEST_MODEL must name the governed pinned GGUF fixture");
    let model = Model::load(&path).unwrap();
    let tokens = model
        .tokenize("Hello from AM Workbench", true, false)
        .expect("governed fixture tokenizer must succeed");
    assert!(!tokens.is_empty());
    let round_trip = model.detokenize(&tokens, false, true).unwrap();
    assert!(!round_trip.is_empty());
    assert!(String::from_utf8_lossy(&round_trip).contains("Hello"));
}

#[test]
fn native_context_keeps_model_alive_and_supports_decode_state_round_trip() {
    let path = std::env::var_os("AMW_ENGINE_NATIVE_TEST_MODEL")
        .or_else(|| std::env::var_os("AMW_ENGINE_TEST_MODEL"))
        .map(std::path::PathBuf::from)
        .expect("AMW_ENGINE_NATIVE_TEST_MODEL must name the governed pinned GGUF fixture");
    let model = Model::load(&path).unwrap();
    assert!(model.vocabulary_size() > 0);
    let vocabulary_fingerprint = model.vocabulary_fingerprint().unwrap();
    assert_eq!(vocabulary_fingerprint.len(), 64);
    assert_eq!(
        model.vocabulary_fingerprint().unwrap(),
        vocabulary_fingerprint
    );
    assert!(model.embedding_size() > 0);
    let vocabulary_size = model.vocabulary_size();
    let tokens = vec![model.beginning_token()];
    assert!(!tokens.is_empty());
    assert!(!model.token_piece(tokens[0], true).unwrap().is_empty());

    let capacity = i32::try_from(tokens.len()).unwrap();
    let retained_model = model.clone();
    let mut context = retained_model
        .context_with(ContextOptions {
            context_tokens: 128,
            batch_tokens: 128,
            micro_batch_tokens: 128,
            max_sequences: 2,
            unified_kv: false,
            embeddings: false,
            pooling: None,
        })
        .unwrap();
    drop(retained_model);
    drop(model);
    let metadata = context.metadata();
    // The pinned llama.cpp revision pads each sequence to 256 KV cells, so a
    // two-sequence context requested at 128 tokens materializes as 512 cells.
    assert_eq!(metadata.context_tokens, 512);
    assert_eq!(metadata.max_sequences, 2);
    let mut batch = Batch::tokens(capacity, 1).unwrap();
    for (index, token) in tokens.iter().copied().enumerate() {
        batch
            .add_token(
                token,
                i32::try_from(index).unwrap(),
                &[0],
                index + 1 == tokens.len(),
            )
            .unwrap();
    }
    context.decode(&mut batch).unwrap();
    if tokens.len() > 1 {
        assert!(matches!(
            context.logits(0),
            Err(FfiError::OutputUnavailable)
        ));
    }
    let output_index = i32::try_from(tokens.len() - 1).unwrap();
    assert_eq!(context.logits(output_index).unwrap().len(), vocabulary_size);
    let mut sampler = Sampler::greedy().unwrap();
    let sampled = sampler
        .sample_and_accept(&mut context, output_index)
        .unwrap();
    assert!(sampled >= 0);
    assert!(usize::try_from(sampled).unwrap() < vocabulary_size);
    let state = context.sequence_state(0).unwrap();
    assert!(!state.is_empty());
    assert_eq!(context.sequence_state_size(0).unwrap(), state.len());
    assert_eq!(
        context.sequence_state_size(-1),
        Err(FfiError::InvalidArgument(
            "sequence id is outside the native context capacity"
        ))
    );
    context.restore_sequence_state(0, &state).unwrap();
    assert!(context.memory_seq_pos_max(0).unwrap() >= 0);
    assert_eq!(
        Sampler::greedy()
            .unwrap()
            .transform_sample_accept(&mut context, output_index),
        Err(FfiError::OutputUnavailable),
        "state restore must invalidate tentative output rows"
    );
}

#[test]
fn embedded_chat_template_is_trust_checked_and_exception_firewalled() {
    let path = std::env::var_os("AMW_ENGINE_NATIVE_TEST_MODEL")
        .map(std::path::PathBuf::from)
        .expect("AMW_ENGINE_NATIVE_TEST_MODEL must name the governed pinned GGUF fixture");
    let metadata = inspect_gguf(&path).unwrap();
    let model = Model::load(&path).unwrap();
    let messages = [
        ChatMessage {
            role: ChatRole::System,
            content: "Answer precisely.",
        },
        ChatMessage {
            role: ChatRole::User,
            content: "hello",
        },
    ];
    let policy = TemplatePolicy;
    let verdict = policy.evaluate("tinyllama-15m", metadata.chat_template.as_deref());
    let rendered = verdict.render_chat(&model, &messages, true).unwrap();
    assert!(!rendered.is_empty());
    assert!(rendered.windows(5).any(|window| window == b"hello"));
    assert_eq!(
        policy
            .evaluate("tinyllama-15m", Some("substituted template"))
            .render_chat(&model, &messages, true),
        Err(TemplateRenderError::TemplateUntrusted)
    );
    assert!(matches!(
        verdict.render_chat(
            &model,
            &[ChatMessage {
                role: ChatRole::User,
                content: "bad\0content",
            }],
            true,
        ),
        Err(TemplateRenderError::Native(FfiError::InteriorNul))
    ));
}
