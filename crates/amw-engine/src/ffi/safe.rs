use std::{
    ffi::{CStr, CString},
    path::{Path, PathBuf},
    ptr::{self, NonNull},
    sync::{Arc, Once},
};

use sha2::{Digest, Sha256};
use thiserror::Error;

use crate::store::loader::VerifiedModelFile;

use super::sys;

static BACKEND_INIT: Once = Once::new();

/// Maximum sequence identity accepted by the pinned llama.cpp revision.
pub const MAX_NATIVE_SEQUENCES: u32 = 256;
/// Maximum native bytes retained by one sequence snapshot (64 MiB).
pub const MAX_SEQUENCE_STATE_BYTES: usize = 64 * 1024 * 1024;

#[derive(Debug, Error, Eq, PartialEq)]
pub enum FfiError {
    #[error("model file does not exist: {0}")]
    ModelNotFound(PathBuf),
    #[error("path or grammar contains an interior NUL byte")]
    InteriorNul,
    #[error("llama.cpp refused to load model: {0}")]
    ModelLoad(PathBuf),
    #[error("verified model content changed while llama.cpp was loading it")]
    ModelIdentityChanged,
    #[error("llama.cpp refused to load the verified model")]
    VerifiedModelLoad,
    #[error("llama.cpp could not create a model context")]
    ContextCreate,
    #[error("llama.cpp could not allocate a token batch")]
    BatchCreate,
    #[error("llama.cpp could not create a sampler")]
    SamplerCreate,
    #[error("the active sampler chain cannot be transactionally cloned")]
    SamplerCloneUnavailable,
    #[error("llama.cpp could not parse the requested grammar")]
    GrammarCreate,
    #[error("LoRA adapter file does not exist: {0}")]
    AdapterNotFound(PathBuf),
    #[error("llama.cpp refused to load LoRA adapter: {0}")]
    AdapterLoad(PathBuf),
    #[error("llama.cpp refused to apply the requested LoRA adapter set")]
    AdapterApply,
    #[error("llama.cpp decode failed with status {0}")]
    Decode(i32),
    #[error("token batch capacity or sequence width exceeded")]
    BatchCapacity,
    #[error("batch operation does not match its {0} storage mode")]
    BatchMode(&'static str),
    #[error("llama.cpp tokenization failed")]
    Tokenize,
    #[error("llama.cpp detokenization failed")]
    Detokenize,
    #[error("llama.cpp vocabulary identity could not be fingerprinted")]
    VocabularyFingerprint,
    #[error("requested llama.cpp output is unavailable")]
    OutputUnavailable,
    #[error("loaded model does not embed a chat template")]
    ChatTemplateMissing,
    #[error("loaded model chat template does not match the trusted catalog template")]
    ChatTemplateMismatch,
    #[error("llama.cpp could not render the embedded chat template")]
    ChatTemplateRender,
    #[error("llama.cpp sequence state transfer failed")]
    StateTransfer,
    #[error("invalid llama.cpp argument: {0}")]
    InvalidArgument(&'static str),
    #[error("llama.cpp raised a native exception during {operation}: {message}")]
    NativeException {
        operation: &'static str,
        message: String,
    },
}

fn check_native_exception(operation: &'static str, status: i32) -> Result<(), FfiError> {
    if status == 0 {
        return Ok(());
    }
    // SAFETY: the firewall returns a thread-local, NUL-terminated string that
    // remains valid until the next guarded call on this thread.
    let message = unsafe {
        let raw = sys::amw_ffi_last_error();
        if raw.is_null() {
            "native exception without detail".to_owned()
        } else {
            CStr::from_ptr(raw).to_string_lossy().into_owned()
        }
    };
    Err(FfiError::NativeException { operation, message })
}

/// The mutually-exclusive storage mode selected by `llama_batch_init`.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum BatchMode {
    Tokens,
    Embeddings { width: usize },
}

/// Actual dimensions selected by llama.cpp for a live context.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct ContextMetadata {
    pub context_tokens: u32,
    pub batch_tokens: u32,
    pub micro_batch_tokens: u32,
    pub max_sequences: u32,
    pub vocabulary_size: usize,
    pub embedding_size: usize,
    pub pooling: Option<EmbeddingPooling>,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum EmbeddingPooling {
    None,
    Mean,
    Cls,
    Last,
    Rank,
}

/// Safe subset of llama.cpp context configuration owned by EngineConfig.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct ContextOptions {
    pub context_tokens: u32,
    pub batch_tokens: u32,
    pub micro_batch_tokens: u32,
    pub max_sequences: u32,
    pub unified_kv: bool,
    pub embeddings: bool,
    pub pooling: Option<EmbeddingPooling>,
}

impl ContextOptions {
    fn validate(self) -> Result<(), FfiError> {
        if self.micro_batch_tokens > 0
            && self.batch_tokens > 0
            && self.micro_batch_tokens > self.batch_tokens
        {
            return Err(FfiError::InvalidArgument(
                "micro batch cannot exceed logical batch",
            ));
        }
        if self.max_sequences > MAX_NATIVE_SEQUENCES {
            return Err(FfiError::InvalidArgument(
                "maximum sequences exceeds llama.cpp identity limit",
            ));
        }
        if self.batch_tokens > 0 && self.max_sequences > self.batch_tokens {
            return Err(FfiError::InvalidArgument(
                "maximum sequences cannot exceed logical batch capacity",
            ));
        }
        if self.context_tokens > 0 && self.max_sequences > self.context_tokens {
            return Err(FfiError::InvalidArgument(
                "maximum sequences cannot exceed context capacity",
            ));
        }
        if self.pooling.is_some() && !self.embeddings {
            return Err(FfiError::InvalidArgument(
                "embedding pooling requires embeddings output",
            ));
        }
        Ok(())
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct FimTokens {
    pub prefix: i32,
    pub suffix: i32,
    pub middle: i32,
    pub padding: Option<i32>,
    pub repository: Option<i32>,
    pub separator: Option<i32>,
}

/// Role accepted by llama.cpp's embedded chat-template renderer.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ChatRole {
    System,
    User,
    Assistant,
    Tool,
}

impl ChatRole {
    fn as_str(self) -> &'static str {
        match self {
            Self::System => "system",
            Self::User => "user",
            Self::Assistant => "assistant",
            Self::Tool => "tool",
        }
    }
}

/// Typed message rendered by a model's trusted embedded template.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct ChatMessage<'a> {
    pub role: ChatRole,
    pub content: &'a str,
}

/// One candidate after grammar, bias, penalties, truncation, and temperature.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct PostTransformCandidate {
    pub token: i32,
    pub logit: f32,
    pub probability: f32,
}

/// Exactly one selected and accepted token plus its authoritative distribution.
#[derive(Clone, Debug, PartialEq)]
pub struct SampledToken {
    pub token: i32,
    pub probability: f32,
    pub candidates: Vec<PostTransformCandidate>,
}

/// One sampler-clone probe that has selected but not accepted a token.
#[derive(Clone, Debug, PartialEq)]
pub struct ProbedDistribution {
    pub selected_token: i32,
    pub selected_probability: f32,
    pub candidates: Vec<PostTransformCandidate>,
}

impl Default for ContextOptions {
    fn default() -> Self {
        Self {
            context_tokens: 0,
            batch_tokens: 0,
            micro_batch_tokens: 0,
            max_sequences: 0,
            unified_kv: false,
            embeddings: false,
            pooling: None,
        }
    }
}

fn pooling_from_native(value: sys::llama_pooling_type) -> Option<EmbeddingPooling> {
    match value {
        sys::llama_pooling_type_LLAMA_POOLING_TYPE_NONE => Some(EmbeddingPooling::None),
        sys::llama_pooling_type_LLAMA_POOLING_TYPE_MEAN => Some(EmbeddingPooling::Mean),
        sys::llama_pooling_type_LLAMA_POOLING_TYPE_CLS => Some(EmbeddingPooling::Cls),
        sys::llama_pooling_type_LLAMA_POOLING_TYPE_LAST => Some(EmbeddingPooling::Last),
        sys::llama_pooling_type_LLAMA_POOLING_TYPE_RANK => Some(EmbeddingPooling::Rank),
        _ => None,
    }
}

/// Cloneable handle to one native model allocation.
///
/// Contexts and model-derived adapters retain their own handle, so dropping the
/// caller's `Model` never invalidates a live native dependent object.
#[derive(Clone)]
pub struct Model {
    inner: Arc<ModelInner>,
}

struct ModelInner {
    raw: NonNull<sys::llama_model>,
    _source_guard: Option<VerifiedModelFile>,
}

fn load_model_from_path(
    path: &Path,
    sanitize_path_error: bool,
) -> Result<NonNull<sys::llama_model>, FfiError> {
    let path_string = path.to_string_lossy();
    let path_c = CString::new(path_string.as_bytes()).map_err(|_| FfiError::InteriorNul)?;
    BACKEND_INIT.call_once(|| {
        // SAFETY: llama.cpp documents this as a process-wide, one-time initialization.
        unsafe { sys::llama_backend_init() };
    });
    let mut exception_status = 0;
    // SAFETY: `path_c` remains live for the call and params come from llama.cpp.
    let raw = unsafe {
        let mut params = sys::llama_model_default_params();
        params.use_extra_bufts = false;
        params.check_tensors = true;
        sys::amw_ffi_model_load_from_file(path_c.as_ptr(), params, &mut exception_status)
    };
    if let Err(error) = check_native_exception("model_load", exception_status) {
        return if sanitize_path_error {
            Err(FfiError::VerifiedModelLoad)
        } else {
            Err(error)
        };
    }
    NonNull::new(raw).ok_or_else(|| {
        if sanitize_path_error {
            FfiError::VerifiedModelLoad
        } else {
            FfiError::ModelLoad(path.to_owned())
        }
    })
}

impl Model {
    pub fn load(path: &Path) -> Result<Self, FfiError> {
        if !path.is_file() {
            return Err(FfiError::ModelNotFound(path.to_owned()));
        }
        let raw = load_model_from_path(path, false)?;
        Ok(Self {
            inner: Arc::new(ModelInner {
                raw,
                _source_guard: None,
            }),
        })
    }

    /// Loads the exact verified bytes used for GGUF inspection and fingerprinting.
    pub(crate) fn load_verified(source: VerifiedModelFile) -> Result<Self, FfiError> {
        let raw = load_model_from_path(source.native_path(), true)?;
        if source.verify_unchanged().is_err() {
            // SAFETY: `raw` is a uniquely-owned model that has not been published.
            unsafe { sys::llama_model_free(raw.as_ptr()) };
            return Err(FfiError::ModelIdentityChanged);
        }
        Ok(Self {
            inner: Arc::new(ModelInner {
                raw,
                _source_guard: Some(source),
            }),
        })
    }

    pub fn context(&self) -> Result<Context, FfiError> {
        self.context_with(ContextOptions::default())
    }

    pub fn context_with(&self, options: ContextOptions) -> Result<Context, FfiError> {
        options.validate()?;
        // SAFETY: self owns a live model and the returned context retains a
        // cloned model handle until after the native context is freed.
        let mut exception_status = 0;
        let raw = unsafe {
            let mut params = sys::llama_context_default_params();
            if options.context_tokens > 0 {
                params.n_ctx = options.context_tokens;
            }
            if options.batch_tokens > 0 {
                params.n_batch = options.batch_tokens;
            }
            if options.micro_batch_tokens > 0 {
                params.n_ubatch = options.micro_batch_tokens;
            }
            if options.max_sequences > 0 {
                params.n_seq_max = options.max_sequences;
            }
            params.kv_unified = options.unified_kv;
            params.embeddings = options.embeddings;
            if let Some(pooling) = options.pooling {
                params.pooling_type = match pooling {
                    EmbeddingPooling::None => sys::llama_pooling_type_LLAMA_POOLING_TYPE_NONE,
                    EmbeddingPooling::Mean => sys::llama_pooling_type_LLAMA_POOLING_TYPE_MEAN,
                    EmbeddingPooling::Cls => sys::llama_pooling_type_LLAMA_POOLING_TYPE_CLS,
                    EmbeddingPooling::Last => sys::llama_pooling_type_LLAMA_POOLING_TYPE_LAST,
                    EmbeddingPooling::Rank => sys::llama_pooling_type_LLAMA_POOLING_TYPE_RANK,
                };
            }
            sys::amw_ffi_init_from_model(self.raw().as_ptr(), params, &mut exception_status)
        };
        check_native_exception("context_init", exception_status)?;
        NonNull::new(raw)
            .map(|raw| Context {
                raw,
                vocabulary_size: self.vocabulary_size(),
                embedding_size: self.output_embedding_size(),
                requested_outputs: Vec::new(),
                model: self.clone(),
            })
            .ok_or(FfiError::ContextCreate)
    }

    /// Tokenizes text with the pinned model vocabulary.
    pub fn tokenize(
        &self,
        text: &str,
        add_special: bool,
        parse_special: bool,
    ) -> Result<Vec<i32>, FfiError> {
        let text_len = i32::try_from(text.len()).map_err(|_| FfiError::Tokenize)?;
        let vocab = self.vocab();
        // SAFETY: the UTF-8 buffer is live for both calls. A null output with
        // zero capacity is llama.cpp's documented sizing probe.
        let mut exception_status = 0;
        let required = unsafe {
            sys::amw_ffi_tokenize(
                vocab,
                text.as_ptr().cast(),
                text_len,
                ptr::null_mut(),
                0,
                add_special,
                parse_special,
                &mut exception_status,
            )
        };
        check_native_exception("tokenize", exception_status)?;
        let capacity = required.checked_neg().ok_or(FfiError::Tokenize)?;
        if capacity == 0 {
            return Ok(Vec::new());
        }
        let mut tokens = vec![0; usize::try_from(capacity).map_err(|_| FfiError::Tokenize)?];
        // SAFETY: tokens has exactly capacity writable elements and the source
        // buffer remains live for the call.
        let written = unsafe {
            sys::amw_ffi_tokenize(
                vocab,
                text.as_ptr().cast(),
                text_len,
                tokens.as_mut_ptr(),
                capacity,
                add_special,
                parse_special,
                &mut exception_status,
            )
        };
        check_native_exception("tokenize", exception_status)?;
        if written < 0 || written > capacity {
            return Err(FfiError::Tokenize);
        }
        tokens.truncate(usize::try_from(written).map_err(|_| FfiError::Tokenize)?);
        Ok(tokens)
    }

    /// Detokenizes a complete token slice without assuming the result is UTF-8.
    pub fn detokenize(
        &self,
        tokens: &[i32],
        remove_special: bool,
        unparse_special: bool,
    ) -> Result<Vec<u8>, FfiError> {
        let token_count = i32::try_from(tokens.len()).map_err(|_| FfiError::Detokenize)?;
        let vocab = self.vocab();
        // SAFETY: tokens remains live and a null output with zero capacity is a
        // sizing probe documented by llama.cpp.
        let mut exception_status = 0;
        let required = unsafe {
            sys::amw_ffi_detokenize(
                vocab,
                tokens.as_ptr(),
                token_count,
                ptr::null_mut(),
                0,
                remove_special,
                unparse_special,
                &mut exception_status,
            )
        };
        check_native_exception("detokenize", exception_status)?;
        let capacity = required.checked_neg().ok_or(FfiError::Detokenize)?;
        if capacity == 0 {
            return Ok(Vec::new());
        }
        let mut bytes = vec![0_u8; usize::try_from(capacity).map_err(|_| FfiError::Detokenize)?];
        // SAFETY: bytes is writable for capacity bytes and tokens remains live.
        let written = unsafe {
            sys::amw_ffi_detokenize(
                vocab,
                tokens.as_ptr(),
                token_count,
                bytes.as_mut_ptr().cast(),
                capacity,
                remove_special,
                unparse_special,
                &mut exception_status,
            )
        };
        check_native_exception("detokenize", exception_status)?;
        if written < 0 || written > capacity {
            return Err(FfiError::Detokenize);
        }
        bytes.truncate(usize::try_from(written).map_err(|_| FfiError::Detokenize)?);
        Ok(bytes)
    }

    /// Converts one token to its exact byte piece.
    pub fn token_piece(&self, token: i32, special: bool) -> Result<Vec<u8>, FfiError> {
        let vocab = self.vocab();
        // SAFETY: a null output with zero length is a sizing probe.
        let mut exception_status = 0;
        let required = unsafe {
            sys::amw_ffi_token_to_piece(
                vocab,
                token,
                ptr::null_mut(),
                0,
                0,
                special,
                &mut exception_status,
            )
        };
        check_native_exception("token_to_piece", exception_status)?;
        let capacity = required.checked_neg().ok_or(FfiError::Detokenize)?;
        if capacity == 0 {
            return Ok(Vec::new());
        }
        let mut bytes = vec![0_u8; usize::try_from(capacity).map_err(|_| FfiError::Detokenize)?];
        // SAFETY: bytes has capacity writable bytes.
        let written = unsafe {
            sys::amw_ffi_token_to_piece(
                vocab,
                token,
                bytes.as_mut_ptr().cast(),
                capacity,
                0,
                special,
                &mut exception_status,
            )
        };
        check_native_exception("token_to_piece", exception_status)?;
        if written < 0 || written > capacity {
            return Err(FfiError::Detokenize);
        }
        bytes.truncate(usize::try_from(written).map_err(|_| FfiError::Detokenize)?);
        Ok(bytes)
    }

    pub fn vocabulary_size(&self) -> usize {
        // SAFETY: the vocabulary is borrowed from this live model.
        let count = unsafe { sys::llama_vocab_n_tokens(self.vocab()) };
        usize::try_from(count).unwrap_or(0)
    }

    pub fn vocab_size(&self) -> usize {
        self.vocabulary_size()
    }

    /// Computes a native vocabulary-semantic fingerprint over IDs, pieces, scores, and attrs.
    pub fn vocabulary_fingerprint(&self) -> Result<String, FfiError> {
        let vocabulary_size = self.vocabulary_size();
        if vocabulary_size == 0 {
            return Err(FfiError::VocabularyFingerprint);
        }
        let vocab = self.vocab();
        let mut identity = std::mem::MaybeUninit::<sys::amw_ffi_vocab_identity>::uninit();
        let mut exception_status = 0;
        // SAFETY: identity is writable and the vocabulary belongs to this live model.
        let status = unsafe {
            sys::amw_ffi_vocab_identity_get(vocab, identity.as_mut_ptr(), &mut exception_status)
        };
        check_native_exception("vocab_identity", exception_status)?;
        if status != 0 {
            return Err(FfiError::VocabularyFingerprint);
        }
        // SAFETY: a zero status guarantees the firewall initialized every field.
        let identity = unsafe { identity.assume_init() };
        let mut hasher = Sha256::new();
        hasher.update(b"amw-native-vocabulary-identity-v1");
        hasher.update((vocabulary_size as u64).to_le_bytes());
        for value in [
            identity.vocabulary_type,
            identity.bos,
            identity.eos,
            identity.eot,
            identity.separator,
            identity.newline,
            identity.padding,
            identity.mask,
            identity.fim_prefix,
            identity.fim_suffix,
            identity.fim_middle,
            identity.fim_padding,
            identity.fim_repository,
            identity.fim_separator,
        ] {
            hasher.update(value.to_le_bytes());
        }
        for token in 0..vocabulary_size {
            let token = i32::try_from(token).map_err(|_| FfiError::VocabularyFingerprint)?;
            let mut text = ptr::null();
            let mut text_length = 0_usize;
            let mut score = 0.0_f32;
            let mut attributes = 0_i32;
            let mut is_eog = false;
            let mut is_control = false;
            // SAFETY: all outputs are writable and the returned text is model-owned.
            let status = unsafe {
                sys::amw_ffi_vocab_token_metadata(
                    vocab,
                    token,
                    &mut text,
                    &mut text_length,
                    &mut score,
                    &mut attributes,
                    &mut is_eog,
                    &mut is_control,
                    &mut exception_status,
                )
            };
            check_native_exception("vocab_token_metadata", exception_status)?;
            if status != 0 || text.is_null() || text_length > 1024 * 1024 {
                return Err(FfiError::VocabularyFingerprint);
            }
            // SAFETY: the firewall reports the byte length of model-owned token text.
            let text = unsafe { std::slice::from_raw_parts(text.cast::<u8>(), text_length) };
            hasher.update(token.to_le_bytes());
            hasher.update((text_length as u64).to_le_bytes());
            hasher.update(text);
            hasher.update(score.to_bits().to_le_bytes());
            hasher.update(attributes.to_le_bytes());
            hasher.update([u8::from(is_eog), u8::from(is_control)]);
        }
        let digest = hasher.finalize();
        Ok(digest.iter().map(|byte| format!("{byte:02x}")).collect())
    }

    pub fn embedding_size(&self) -> usize {
        // SAFETY: self owns a live model.
        let count = unsafe { sys::llama_model_n_embd(self.raw().as_ptr()) };
        usize::try_from(count).unwrap_or(0)
    }

    pub fn output_embedding_size(&self) -> usize {
        // SAFETY: self owns a live model.
        let count = unsafe { sys::llama_model_n_embd_out(self.raw().as_ptr()) };
        usize::try_from(count).unwrap_or(0)
    }

    pub fn training_context_size(&self) -> u32 {
        // SAFETY: self owns a live model.
        let count = unsafe { sys::llama_model_n_ctx_train(self.raw().as_ptr()) };
        u32::try_from(count).unwrap_or(0)
    }

    pub fn is_end_token(&self, token: i32) -> bool {
        // SAFETY: the vocabulary is borrowed from this live model.
        unsafe { sys::llama_vocab_is_eog(self.vocab(), token) }
    }

    pub fn is_control_token(&self, token: i32) -> bool {
        // SAFETY: the vocabulary is borrowed from this live model.
        unsafe { sys::llama_vocab_is_control(self.vocab(), token) }
    }

    pub fn beginning_token(&self) -> i32 {
        // SAFETY: the vocabulary is borrowed from this live model.
        unsafe { sys::llama_vocab_bos(self.vocab()) }
    }

    pub fn end_token(&self) -> i32 {
        // SAFETY: the vocabulary is borrowed from this live model.
        unsafe { sys::llama_vocab_eos(self.vocab()) }
    }

    pub fn fim_tokens(&self) -> Option<FimTokens> {
        // SAFETY: all token queries borrow the vocabulary from this live model.
        let (prefix, suffix, middle, padding, repository, separator) = unsafe {
            (
                sys::llama_vocab_fim_pre(self.vocab()),
                sys::llama_vocab_fim_suf(self.vocab()),
                sys::llama_vocab_fim_mid(self.vocab()),
                sys::llama_vocab_fim_pad(self.vocab()),
                sys::llama_vocab_fim_rep(self.vocab()),
                sys::llama_vocab_fim_sep(self.vocab()),
            )
        };
        if prefix < 0 || suffix < 0 || middle < 0 {
            return None;
        }
        Some(FimTokens {
            prefix,
            suffix,
            middle,
            padding: (padding >= 0).then_some(padding),
            repository: (repository >= 0).then_some(repository),
            separator: (separator >= 0).then_some(separator),
        })
    }

    fn vocab(&self) -> *const sys::llama_vocab {
        // SAFETY: the returned vocabulary is owned by this live model.
        unsafe { sys::llama_model_get_vocab(self.raw().as_ptr()) }
    }

    fn raw(&self) -> NonNull<sys::llama_model> {
        self.inner.raw
    }

    pub fn grammar(&self, grammar: &str, root: &str) -> Result<Grammar, FfiError> {
        let grammar = CString::new(grammar).map_err(|_| FfiError::InteriorNul)?;
        let root = CString::new(root).map_err(|_| FfiError::InteriorNul)?;
        // SAFETY: strings live for the call; the vocabulary is borrowed from this live model.
        let mut exception_status = 0;
        let raw = unsafe {
            let vocab = sys::llama_model_get_vocab(self.raw().as_ptr());
            sys::amw_ffi_sampler_init_grammar(
                vocab,
                grammar.as_ptr(),
                root.as_ptr(),
                &mut exception_status,
            )
        };
        check_native_exception("grammar_init", exception_status)?;
        NonNull::new(raw)
            .map(|raw| Grammar {
                raw,
                _model: self.clone(),
            })
            .ok_or(FfiError::GrammarCreate)
    }

    pub fn lora_adapter(&self, path: &Path) -> Result<LoraAdapter, FfiError> {
        if !path.is_file() {
            return Err(FfiError::AdapterNotFound(path.to_owned()));
        }
        let path_string = path.to_string_lossy();
        let path_c = CString::new(path_string.as_bytes()).map_err(|_| FfiError::InteriorNul)?;
        // SAFETY: the model and path are live for the call. The returned adapter
        // retains a model handle and is uniquely freed by its Drop implementation.
        let mut exception_status = 0;
        let raw = unsafe {
            sys::amw_ffi_adapter_lora_init(
                self.raw().as_ptr(),
                path_c.as_ptr(),
                &mut exception_status,
            )
        };
        check_native_exception("adapter_lora_init", exception_status)?;
        NonNull::new(raw)
            .map(|raw| LoraAdapter {
                raw,
                model: self.clone(),
            })
            .ok_or_else(|| FfiError::AdapterLoad(path.to_owned()))
    }

    /// Renders typed messages with the model-embedded template after an exact trust check.
    ///
    /// `trusted_embedded_template` must come from the verified model catalog. The
    /// native model's current embedded template must match it byte-for-byte, so
    /// request input cannot select or substitute executable template text.
    pub(crate) fn apply_embedded_chat_template(
        &self,
        trusted_embedded_template: &str,
        messages: &[ChatMessage<'_>],
        add_assistant: bool,
    ) -> Result<Vec<u8>, FfiError> {
        let expected =
            CString::new(trusted_embedded_template).map_err(|_| FfiError::InteriorNul)?;
        let mut exception_status = 0;
        // SAFETY: the returned template belongs to the live immutable model.
        let embedded =
            unsafe { sys::amw_ffi_model_chat_template(self.raw().as_ptr(), &mut exception_status) };
        check_native_exception("model_chat_template", exception_status)?;
        if embedded.is_null() {
            return Err(FfiError::ChatTemplateMissing);
        }
        // SAFETY: llama.cpp returns a NUL-terminated template owned by the model.
        if unsafe { CStr::from_ptr(embedded) }.to_bytes() != expected.as_bytes() {
            return Err(FfiError::ChatTemplateMismatch);
        }
        self.render_chat_template(&expected, messages, add_assistant)
    }

    /// Renders a template that the crate-local curated policy selected.
    pub(crate) fn apply_curated_chat_template(
        &self,
        curated_template: &str,
        messages: &[ChatMessage<'_>],
        add_assistant: bool,
    ) -> Result<Vec<u8>, FfiError> {
        let template = CString::new(curated_template).map_err(|_| FfiError::InteriorNul)?;
        self.render_chat_template(&template, messages, add_assistant)
    }

    fn render_chat_template(
        &self,
        template: &CString,
        messages: &[ChatMessage<'_>],
        add_assistant: bool,
    ) -> Result<Vec<u8>, FfiError> {
        let roles = messages
            .iter()
            .map(|message| CString::new(message.role.as_str()).map_err(|_| FfiError::InteriorNul))
            .collect::<Result<Vec<_>, _>>()?;
        let contents = messages
            .iter()
            .map(|message| CString::new(message.content).map_err(|_| FfiError::InteriorNul))
            .collect::<Result<Vec<_>, _>>()?;
        let role_ptrs = roles.iter().map(|value| value.as_ptr()).collect::<Vec<_>>();
        let content_ptrs = contents
            .iter()
            .map(|value| value.as_ptr())
            .collect::<Vec<_>>();
        let mut exception_status = 0;
        // SAFETY: all C strings and pointer arrays remain live for both calls;
        // a null zero-capacity output is the renderer's documented sizing probe.
        let required = unsafe {
            sys::amw_ffi_chat_apply_template(
                template.as_ptr(),
                role_ptrs.as_ptr(),
                content_ptrs.as_ptr(),
                messages.len(),
                add_assistant,
                ptr::null_mut(),
                0,
                &mut exception_status,
            )
        };
        check_native_exception("chat_apply_template", exception_status)?;
        if required < 0 {
            return Err(FfiError::ChatTemplateRender);
        }
        if required == 0 {
            return Ok(Vec::new());
        }
        let mut output =
            vec![0_u8; usize::try_from(required).map_err(|_| FfiError::ChatTemplateRender)?];
        // SAFETY: output has `required` writable bytes and every C string and
        // pointer array remains live for the duration of the renderer call.
        let written = unsafe {
            sys::amw_ffi_chat_apply_template(
                template.as_ptr(),
                role_ptrs.as_ptr(),
                content_ptrs.as_ptr(),
                messages.len(),
                add_assistant,
                output.as_mut_ptr().cast(),
                required,
                &mut exception_status,
            )
        };
        check_native_exception("chat_apply_template", exception_status)?;
        if written < 0 || written > required {
            return Err(FfiError::ChatTemplateRender);
        }
        output.truncate(usize::try_from(written).map_err(|_| FfiError::ChatTemplateRender)?);
        Ok(output)
    }
}

impl Drop for ModelInner {
    fn drop(&mut self) {
        // SAFETY: raw is unique to this wrapper and freed exactly once here.
        unsafe { sys::llama_model_free(self.raw.as_ptr()) };
    }
}

/// Native context with an owned keepalive for its originating model.
pub struct Context {
    raw: NonNull<sys::llama_context>,
    vocabulary_size: usize,
    embedding_size: usize,
    requested_outputs: Vec<i32>,
    model: Model,
}

impl Context {
    pub fn metadata(&self) -> ContextMetadata {
        // SAFETY: raw is a live context and these queries do not mutate it.
        unsafe {
            ContextMetadata {
                context_tokens: sys::llama_n_ctx(self.raw.as_ptr()),
                batch_tokens: sys::llama_n_batch(self.raw.as_ptr()),
                micro_batch_tokens: sys::llama_n_ubatch(self.raw.as_ptr()),
                max_sequences: sys::llama_n_seq_max(self.raw.as_ptr()),
                vocabulary_size: self.vocabulary_size,
                embedding_size: self.embedding_size,
                pooling: pooling_from_native(sys::llama_pooling_type(self.raw.as_ptr())),
            }
        }
    }

    pub fn decode(&mut self, batch: &mut Batch) -> Result<(), FfiError> {
        let raw = batch.raw.as_ref().ok_or(FfiError::BatchCreate)?;
        if raw.n_tokens <= 0 {
            return Err(FfiError::InvalidArgument("decode batch is empty"));
        }
        batch.validate_for_context(self)?;
        // SAFETY: both wrappers own live llama.cpp values; llama_decode borrows
        // the batch arrays only for this call.
        let mut exception_status = 0;
        let status = unsafe { sys::amw_ffi_decode(self.raw.as_ptr(), *raw, &mut exception_status) };
        if let Err(error) = check_native_exception("decode", exception_status) {
            self.invalidate_outputs();
            return Err(error);
        }
        if status == 0 {
            self.requested_outputs.clone_from(&batch.requested_outputs);
            Ok(())
        } else {
            self.invalidate_outputs();
            Err(FfiError::Decode(status))
        }
    }

    pub fn memory_seq_rm(&mut self, seq_id: i32, p0: i32, p1: i32) -> Result<bool, FfiError> {
        llama_memory_seq_rm(self, seq_id, p0, p1)
    }

    pub fn memory_seq_cp(
        &mut self,
        source: i32,
        destination: i32,
        p0: i32,
        p1: i32,
    ) -> Result<(), FfiError> {
        llama_memory_seq_cp(self, source, destination, p0, p1)
    }

    pub fn memory_seq_keep(&mut self, seq_id: i32) -> Result<(), FfiError> {
        self.validate_sequence_id(seq_id, false)?;
        // SAFETY: the context owns the live memory handle.
        let mut exception_status = 0;
        unsafe {
            let memory = sys::llama_get_memory(self.raw.as_ptr());
            sys::amw_ffi_memory_seq_keep(memory, seq_id, &mut exception_status);
        }
        let result = check_native_exception("memory_seq_keep", exception_status);
        self.invalidate_outputs();
        result
    }

    pub fn memory_seq_add(
        &mut self,
        seq_id: i32,
        p0: i32,
        p1: i32,
        delta: i32,
    ) -> Result<(), FfiError> {
        self.validate_sequence_id(seq_id, false)?;
        validate_position_range(p0, p1)?;
        if !self.memory_can_shift()? {
            return Err(FfiError::InvalidArgument(
                "memory backend does not support sequence shifting",
            ));
        }
        // SAFETY: the context owns the live memory handle.
        let mut exception_status = 0;
        unsafe {
            let memory = sys::llama_get_memory(self.raw.as_ptr());
            sys::amw_ffi_memory_seq_add(memory, seq_id, p0, p1, delta, &mut exception_status);
        }
        let result = check_native_exception("memory_seq_add", exception_status);
        self.invalidate_outputs();
        result
    }

    pub fn memory_seq_div(
        &mut self,
        seq_id: i32,
        p0: i32,
        p1: i32,
        divisor: i32,
    ) -> Result<(), FfiError> {
        self.validate_sequence_id(seq_id, false)?;
        validate_position_range(p0, p1)?;
        if divisor <= 1 {
            return Err(FfiError::InvalidArgument(
                "memory sequence divisor must exceed one",
            ));
        }
        if !self.memory_can_shift()? {
            return Err(FfiError::InvalidArgument(
                "memory backend does not support sequence shifting",
            ));
        }
        // SAFETY: the context owns the live memory handle; callers must provide
        // the llama.cpp-required divisor greater than one.
        let mut exception_status = 0;
        unsafe {
            let memory = sys::llama_get_memory(self.raw.as_ptr());
            sys::amw_ffi_memory_seq_div(memory, seq_id, p0, p1, divisor, &mut exception_status);
        }
        let result = check_native_exception("memory_seq_div", exception_status);
        self.invalidate_outputs();
        result
    }

    pub fn memory_seq_pos_min(&self, seq_id: i32) -> Result<i32, FfiError> {
        self.validate_sequence_id(seq_id, false)?;
        // SAFETY: the context owns the live memory handle.
        let mut exception_status = 0;
        let position = unsafe {
            let memory = sys::llama_get_memory(self.raw.as_ptr());
            sys::amw_ffi_memory_seq_pos_min(memory, seq_id, &mut exception_status)
        };
        check_native_exception("memory_seq_pos_min", exception_status)?;
        Ok(position)
    }

    pub fn memory_seq_pos_max(&self, seq_id: i32) -> Result<i32, FfiError> {
        self.validate_sequence_id(seq_id, false)?;
        // SAFETY: the context owns the live memory handle.
        let mut exception_status = 0;
        let position = unsafe {
            let memory = sys::llama_get_memory(self.raw.as_ptr());
            sys::amw_ffi_memory_seq_pos_max(memory, seq_id, &mut exception_status)
        };
        check_native_exception("memory_seq_pos_max", exception_status)?;
        Ok(position)
    }

    pub fn memory_can_shift(&self) -> Result<bool, FfiError> {
        // SAFETY: the context owns the live memory handle.
        let mut exception_status = 0;
        let can_shift = unsafe {
            let memory = sys::llama_get_memory(self.raw.as_ptr());
            sys::amw_ffi_memory_can_shift(memory, &mut exception_status)
        };
        check_native_exception("memory_can_shift", exception_status)?;
        Ok(can_shift)
    }

    /// Returns one output row. Positive indices are original batch-token rows;
    /// negative indices address compact requested outputs from the end.
    pub fn logits(&mut self, batch_token_index: i32) -> Result<&[f32], FfiError> {
        if !self.output_was_requested(batch_token_index) {
            return Err(FfiError::OutputUnavailable);
        }
        // SAFETY: output remains owned by the context and the mutable borrow of
        // self prevents decode from invalidating it while the slice is live.
        let mut exception_status = 0;
        let raw = unsafe {
            sys::amw_ffi_get_logits_ith(self.raw.as_ptr(), batch_token_index, &mut exception_status)
        };
        check_native_exception("get_logits_ith", exception_status)?;
        if raw.is_null() || self.vocabulary_size == 0 {
            return Err(FfiError::OutputUnavailable);
        }
        // SAFETY: llama.cpp documents this row as vocabulary_size floats.
        Ok(unsafe { std::slice::from_raw_parts(raw, self.vocabulary_size) })
    }

    /// Returns an unpooled token output. Positive indices are original batch
    /// token rows, not compact requested-output ordinals.
    pub fn embeddings(&mut self, batch_token_index: i32) -> Result<&[f32], FfiError> {
        if !self.output_was_requested(batch_token_index) {
            return Err(FfiError::OutputUnavailable);
        }
        // SAFETY: raw is a live context and this query does not mutate it.
        let pooling = unsafe { sys::llama_pooling_type(self.raw.as_ptr()) };
        if pooling != sys::llama_pooling_type_LLAMA_POOLING_TYPE_NONE {
            return Err(FfiError::OutputUnavailable);
        }
        // SAFETY: output remains owned by the context and the mutable borrow of
        // self prevents decode from invalidating it while the slice is live.
        let mut exception_status = 0;
        let raw = unsafe {
            sys::amw_ffi_get_embeddings_ith(
                self.raw.as_ptr(),
                batch_token_index,
                &mut exception_status,
            )
        };
        check_native_exception("get_embeddings_ith", exception_status)?;
        if raw.is_null() || self.embedding_size == 0 {
            return Err(FfiError::OutputUnavailable);
        }
        // SAFETY: llama.cpp documents this row as embedding_size floats.
        Ok(unsafe { std::slice::from_raw_parts(raw, self.embedding_size) })
    }

    /// Returns the pooled embedding for a sequence in Mean/CLS/Last mode.
    pub fn embeddings_sequence(&mut self, seq_id: i32) -> Result<&[f32], FfiError> {
        self.validate_sequence_id(seq_id, false)?;
        // SAFETY: raw is a live context and this query does not mutate it.
        let pooling = unsafe { sys::llama_pooling_type(self.raw.as_ptr()) };
        if !matches!(
            pooling_from_native(pooling),
            Some(EmbeddingPooling::Mean | EmbeddingPooling::Cls | EmbeddingPooling::Last)
        ) {
            return Err(FfiError::OutputUnavailable);
        }
        // SAFETY: output remains owned by the exclusively borrowed context.
        let mut exception_status = 0;
        let raw = unsafe {
            sys::amw_ffi_get_embeddings_seq(self.raw.as_ptr(), seq_id, &mut exception_status)
        };
        check_native_exception("get_embeddings_seq", exception_status)?;
        if raw.is_null() || self.embedding_size == 0 {
            return Err(FfiError::OutputUnavailable);
        }
        // SAFETY: supported pooling modes return model output-embedding width.
        Ok(unsafe { std::slice::from_raw_parts(raw, self.embedding_size) })
    }

    pub fn sequence_state(&mut self, seq_id: i32) -> Result<Vec<u8>, FfiError> {
        let size = self.sequence_state_size(seq_id)?;
        let mut state = vec![0_u8; size];
        let mut exception_status = 0;
        // SAFETY: state has size writable bytes and self is exclusively borrowed.
        let written = unsafe {
            sys::amw_ffi_state_seq_get_data(
                self.raw.as_ptr(),
                state.as_mut_ptr(),
                size,
                seq_id,
                &mut exception_status,
            )
        };
        check_native_exception("state_seq_get_data", exception_status)?;
        if written != size {
            return Err(FfiError::StateTransfer);
        }
        Ok(state)
    }

    /// Returns the exact native byte count required to snapshot one sequence.
    ///
    /// Callers use this firewalled sizing query to enforce persistence and
    /// speculative-transaction quotas before allocating or copying state.
    pub fn sequence_state_size(&self, seq_id: i32) -> Result<usize, FfiError> {
        self.validate_sequence_id(seq_id, false)?;
        let mut exception_status = 0;
        // SAFETY: self owns the live context and the query does not mutate it.
        let size = unsafe {
            sys::amw_ffi_state_seq_get_size(self.raw.as_ptr(), seq_id, &mut exception_status)
        };
        check_native_exception("state_seq_get_size", exception_status)?;
        if size == 0 || size > MAX_SEQUENCE_STATE_BYTES {
            Err(FfiError::StateTransfer)
        } else {
            Ok(size)
        }
    }

    pub fn restore_sequence_state(&mut self, seq_id: i32, state: &[u8]) -> Result<(), FfiError> {
        self.validate_sequence_id(seq_id, false)?;
        if state.is_empty() || state.len() > MAX_SEQUENCE_STATE_BYTES {
            return Err(FfiError::StateTransfer);
        }
        let mut exception_status = 0;
        // SAFETY: state remains live for the call and self is exclusively borrowed.
        let read = unsafe {
            sys::amw_ffi_state_seq_set_data(
                self.raw.as_ptr(),
                state.as_ptr(),
                state.len(),
                seq_id,
                &mut exception_status,
            )
        };
        let native_result = check_native_exception("state_seq_set_data", exception_status);
        self.invalidate_outputs();
        native_result?;
        if read == state.len() {
            Ok(())
        } else {
            Err(FfiError::StateTransfer)
        }
    }

    fn output_was_requested(&self, index: i32) -> bool {
        if index >= 0 {
            self.requested_outputs.contains(&index)
        } else {
            index
                .checked_neg()
                .and_then(|value| usize::try_from(value).ok())
                .is_some_and(|from_end| from_end > 0 && from_end <= self.requested_outputs.len())
        }
    }

    fn invalidate_outputs(&mut self) {
        self.requested_outputs.clear();
    }

    fn validate_sequence_id(&self, seq_id: i32, allow_any: bool) -> Result<(), FfiError> {
        if allow_any && seq_id == -1 {
            return Ok(());
        }
        // SAFETY: raw is a live context and this capacity query is read-only.
        let maximum = unsafe { sys::llama_n_seq_max(self.raw.as_ptr()) };
        if seq_id < 0 || u32::try_from(seq_id).map_or(true, |sequence| sequence >= maximum) {
            Err(FfiError::InvalidArgument(
                "sequence id is outside the native context capacity",
            ))
        } else {
            Ok(())
        }
    }

    pub fn set_lora_adapters(&mut self, adapters: &[(&LoraAdapter, f32)]) -> Result<(), FfiError> {
        if adapters
            .iter()
            .any(|(adapter, _)| !Arc::ptr_eq(&self.model.inner, &adapter.model.inner))
        {
            return Err(FfiError::InvalidArgument(
                "LoRA adapter belongs to a different model",
            ));
        }
        let mut raw_adapters: Vec<_> = adapters
            .iter()
            .map(|(adapter, _)| adapter.raw.as_ptr())
            .collect();
        let mut scales: Vec<_> = adapters.iter().map(|(_, scale)| *scale).collect();
        let mut exception_status = 0;
        // SAFETY: both arrays have matching lengths and remain live for the call;
        // the pointer-identity check above proves one shared live model owner.
        let result = unsafe {
            sys::amw_ffi_set_adapters_lora(
                self.raw.as_ptr(),
                raw_adapters.as_mut_ptr(),
                raw_adapters.len(),
                scales.as_mut_ptr(),
                &mut exception_status,
            )
        };
        let native_result = check_native_exception("set_adapters_lora", exception_status);
        self.invalidate_outputs();
        native_result?;
        if result == 0 {
            Ok(())
        } else {
            Err(FfiError::AdapterApply)
        }
    }
}

impl Drop for Context {
    fn drop(&mut self) {
        // SAFETY: raw is unique to this wrapper and freed before its model can be dropped.
        unsafe { sys::llama_free(self.raw.as_ptr()) };
    }
}

pub struct Batch {
    raw: Option<sys::llama_batch>,
    token_capacity: i32,
    max_sequences: i32,
    mode: BatchMode,
    requested_outputs: Vec<i32>,
}

impl Batch {
    pub fn tokens(token_capacity: i32, max_sequences: i32) -> Result<Self, FfiError> {
        Self::new(token_capacity, 0, max_sequences)
    }

    pub fn embeddings(
        token_capacity: i32,
        embedding_width: i32,
        max_sequences: i32,
    ) -> Result<Self, FfiError> {
        if embedding_width <= 0 {
            return Err(FfiError::BatchCreate);
        }
        Self::new(token_capacity, embedding_width, max_sequences)
    }

    pub fn new(
        token_capacity: i32,
        embedding_size: i32,
        max_sequences: i32,
    ) -> Result<Self, FfiError> {
        if token_capacity <= 0 || embedding_size < 0 || max_sequences <= 0 {
            return Err(FfiError::BatchCreate);
        }
        // SAFETY: positive dimensions satisfy llama_batch_init's allocation contract.
        let raw = unsafe { sys::llama_batch_init(token_capacity, embedding_size, max_sequences) };
        let mode = if embedding_size == 0 {
            BatchMode::Tokens
        } else {
            BatchMode::Embeddings {
                width: usize::try_from(embedding_size).map_err(|_| FfiError::BatchCreate)?,
            }
        };
        let storage_valid = match mode {
            BatchMode::Tokens => !raw.token.is_null() && raw.embd.is_null(),
            BatchMode::Embeddings { .. } => raw.token.is_null() && !raw.embd.is_null(),
        };
        if !storage_valid
            || raw.pos.is_null()
            || raw.n_seq_id.is_null()
            || raw.seq_id.is_null()
            || raw.logits.is_null()
        {
            // SAFETY: raw is the value returned by llama_batch_init and is
            // consumed exactly once on this failed-construction path.
            unsafe { sys::llama_batch_free(raw) };
            return Err(FfiError::BatchCreate);
        }
        Ok(Self {
            raw: Some(raw),
            token_capacity,
            max_sequences,
            mode,
            requested_outputs: Vec::new(),
        })
    }

    pub const fn mode(&self) -> BatchMode {
        self.mode
    }

    /// Returns the number of rows currently appended to this native batch.
    pub fn token_count(&self) -> usize {
        self.raw
            .as_ref()
            .and_then(|raw| usize::try_from(raw.n_tokens).ok())
            .unwrap_or(0)
    }

    pub fn clear(&mut self) {
        if let Some(raw) = &mut self.raw {
            raw.n_tokens = 0;
        }
        self.requested_outputs.clear();
    }

    pub fn add_token(
        &mut self,
        token: i32,
        position: i32,
        sequence_ids: &[i32],
        request_logits: bool,
    ) -> Result<(), FfiError> {
        if self.mode != BatchMode::Tokens {
            return Err(FfiError::BatchMode("embedding"));
        }
        let index = self.prepare_row(position, sequence_ids, request_logits)?;
        let raw = self.raw.as_mut().ok_or(FfiError::BatchCreate)?;
        // SAFETY: token-mode construction proves raw.token is non-null and
        // prepare_row checked the row against token_capacity.
        unsafe { *raw.token.add(index) = token };
        raw.n_tokens += 1;
        Ok(())
    }

    pub fn add_embedding(
        &mut self,
        embedding: &[f32],
        position: i32,
        sequence_ids: &[i32],
        request_output: bool,
    ) -> Result<(), FfiError> {
        let BatchMode::Embeddings { width } = self.mode else {
            return Err(FfiError::BatchMode("token"));
        };
        if embedding.len() != width {
            return Err(FfiError::BatchCapacity);
        }
        let index = self.prepare_row(position, sequence_ids, request_output)?;
        let raw = self.raw.as_mut().ok_or(FfiError::BatchCreate)?;
        // SAFETY: embedding-mode construction proves raw.embd is non-null;
        // index and width are checked, and the source has exactly width values.
        unsafe {
            ptr::copy_nonoverlapping(embedding.as_ptr(), raw.embd.add(index * width), width);
        }
        raw.n_tokens += 1;
        Ok(())
    }

    fn prepare_row(
        &mut self,
        position: i32,
        sequence_ids: &[i32],
        request_output: bool,
    ) -> Result<usize, FfiError> {
        if position < 0 || sequence_ids.iter().any(|sequence_id| *sequence_id < 0) {
            return Err(FfiError::InvalidArgument(
                "batch positions and sequence ids must be non-negative",
            ));
        }
        if sequence_ids.is_empty() || sequence_ids.len() > self.max_sequences as usize {
            return Err(FfiError::BatchCapacity);
        }
        let raw = self.raw.as_mut().ok_or(FfiError::BatchCreate)?;
        let index = raw.n_tokens;
        if index < 0 || index >= self.token_capacity {
            return Err(FfiError::BatchCapacity);
        }
        // SAFETY: construction validated the common arrays. llama_batch_init
        // allocated token_capacity scalar entries and max_sequences entries
        // for every seq_id row. Bounds are checked above.
        unsafe {
            *raw.pos.add(index as usize) = position;
            *raw.n_seq_id.add(index as usize) = sequence_ids.len() as i32;
            let sequence_row = *raw.seq_id.add(index as usize);
            if sequence_row.is_null() {
                return Err(FfiError::BatchCreate);
            }
            for (offset, sequence_id) in sequence_ids.iter().enumerate() {
                *sequence_row.add(offset) = *sequence_id;
            }
            *raw.logits.add(index as usize) = i8::from(request_output);
        }
        if request_output {
            self.requested_outputs.push(index);
        }
        Ok(index as usize)
    }

    fn validate_for_context(&self, context: &Context) -> Result<(), FfiError> {
        let raw = self.raw.as_ref().ok_or(FfiError::BatchCreate)?;
        let token_count = usize::try_from(raw.n_tokens).map_err(|_| FfiError::BatchCapacity)?;
        // SAFETY: context owns a live native context and this capacity query is read-only.
        let maximum_sequences = unsafe { sys::llama_n_seq_max(context.raw.as_ptr()) };
        for row in 0..token_count {
            // SAFETY: construction allocated each array for token_capacity rows,
            // and prepare_row is the only operation that increments n_tokens.
            let sequence_count = unsafe { *raw.n_seq_id.add(row) };
            if sequence_count <= 0 || sequence_count > self.max_sequences {
                return Err(FfiError::BatchCapacity);
            }
            // SAFETY: construction checked the row pointer and sequence_count is
            // bounded by the allocation width above.
            let sequence_row = unsafe { *raw.seq_id.add(row) };
            if sequence_row.is_null() {
                return Err(FfiError::BatchCreate);
            }
            for offset in 0..usize::try_from(sequence_count).map_err(|_| FfiError::BatchCapacity)? {
                // SAFETY: offset is below the checked per-row sequence capacity.
                let sequence = unsafe { *sequence_row.add(offset) };
                if sequence < 0
                    || u32::try_from(sequence)
                        .map_or(true, |sequence| sequence >= maximum_sequences)
                {
                    return Err(FfiError::InvalidArgument(
                        "batch sequence id is outside the native context capacity",
                    ));
                }
            }
            if self.mode == BatchMode::Tokens {
                // SAFETY: token-mode construction proved the token array is live.
                let token = unsafe { *raw.token.add(row) };
                if token < 0
                    || usize::try_from(token).map_or(true, |token| token >= context.vocabulary_size)
                {
                    return Err(FfiError::InvalidArgument(
                        "batch token is outside the loaded vocabulary",
                    ));
                }
            }
        }
        Ok(())
    }
}

pub fn llama_memory_seq_rm(
    context: &mut Context,
    seq_id: i32,
    p0: i32,
    p1: i32,
) -> Result<bool, FfiError> {
    context.validate_sequence_id(seq_id, true)?;
    validate_position_range(p0, p1)?;
    // SAFETY: context owns a live llama context and its memory handle remains
    // valid for the duration of this call.
    let mut exception_status = 0;
    let removed = unsafe {
        let memory = sys::llama_get_memory(context.raw.as_ptr());
        sys::amw_ffi_memory_seq_rm(memory, seq_id, p0, p1, &mut exception_status)
    };
    let native_result = check_native_exception("memory_seq_rm", exception_status);
    context.invalidate_outputs();
    native_result?;
    Ok(removed)
}

pub fn llama_memory_seq_cp(
    context: &mut Context,
    source: i32,
    destination: i32,
    p0: i32,
    p1: i32,
) -> Result<(), FfiError> {
    context.validate_sequence_id(source, false)?;
    context.validate_sequence_id(destination, false)?;
    if p0 != -1 || p1 != -1 {
        return Err(FfiError::InvalidArgument(
            "memory sequence copy requires the complete sequence range",
        ));
    }
    // SAFETY: context owns a live llama context and the sequence identifiers
    // and position range are passed through according to llama.cpp's contract.
    let mut exception_status = 0;
    unsafe {
        let memory = sys::llama_get_memory(context.raw.as_ptr());
        sys::amw_ffi_memory_seq_cp(memory, source, destination, p0, p1, &mut exception_status);
    }
    let result = check_native_exception("memory_seq_cp", exception_status);
    context.invalidate_outputs();
    result
}

fn validate_position_range(p0: i32, p1: i32) -> Result<(), FfiError> {
    if p0 < -1 || p1 < -1 || (p0 >= 0 && p1 >= 0 && p1 < p0) {
        Err(FfiError::InvalidArgument(
            "memory sequence range is invalid",
        ))
    } else {
        Ok(())
    }
}

impl Drop for Batch {
    fn drop(&mut self) {
        if let Some(raw) = self.raw.take() {
            // SAFETY: this batch was initialized by llama.cpp and is consumed exactly once.
            unsafe { sys::llama_batch_free(raw) };
        }
    }
}

pub struct Sampler {
    raw: NonNull<sys::llama_sampler>,
}

impl Sampler {
    fn from_guarded_raw(
        raw: *mut sys::llama_sampler,
        operation: &'static str,
        exception_status: i32,
    ) -> Result<Self, FfiError> {
        check_native_exception(operation, exception_status)?;
        NonNull::new(raw)
            .map(|raw| Self { raw })
            .ok_or(FfiError::SamplerCreate)
    }

    /// Creates an empty native sampler chain that takes ownership of stages added to it.
    pub fn chain() -> Result<Self, FfiError> {
        // SAFETY: default params are created by the pinned llama.cpp ABI and the
        // returned chain is uniquely owned by this wrapper.
        let mut exception_status = 0;
        let raw = unsafe {
            let params = sys::llama_sampler_chain_default_params();
            sys::amw_ffi_sampler_chain_init(params, &mut exception_status)
        };
        Self::from_guarded_raw(raw, "sampler_chain_init", exception_status)
    }

    /// Clones all request-local transform, grammar, RNG, and acceptance state.
    ///
    /// Only engine-constructed pinned llama.cpp sampler stages reach this
    /// wrapper. A null clone is a typed speculation-disable reason and never
    /// falls back to mutating the authoritative chain.
    pub fn try_clone(&self) -> Result<Self, FfiError> {
        let mut exception_status = 0;
        // SAFETY: self owns a live pinned sampler made only from cloneable
        // built-in stages; the returned pointer is independently owned.
        let raw = unsafe { sys::amw_ffi_sampler_clone(self.raw.as_ptr(), &mut exception_status) };
        check_native_exception("sampler_clone", exception_status)?;
        NonNull::new(raw)
            .map(|raw| Self { raw })
            .ok_or(FfiError::SamplerCloneUnavailable)
    }

    pub fn greedy() -> Result<Self, FfiError> {
        // SAFETY: constructor has no preconditions.
        let mut exception_status = 0;
        let raw = unsafe { sys::amw_ffi_sampler_init_greedy(&mut exception_status) };
        Self::from_guarded_raw(raw, "sampler_init_greedy", exception_status)
    }

    fn add_raw(&mut self, stage: *mut sys::llama_sampler) -> Result<(), FfiError> {
        let stage = NonNull::new(stage).ok_or(FfiError::SamplerCreate)?;
        // SAFETY: self is a live sampler chain and llama_sampler_chain_add takes
        // ownership of the uniquely-created stage. The stage is therefore not
        // wrapped separately and is freed with the chain.
        let mut exception_status = 0;
        // SAFETY: the live chain takes ownership of the uniquely-created stage;
        // the firewall receives valid pointers and initializes exception_status.
        unsafe {
            sys::amw_ffi_sampler_chain_add(
                self.raw.as_ptr(),
                stage.as_ptr(),
                &mut exception_status,
            );
        }
        if let Err(error) = check_native_exception("sampler_chain_add", exception_status) {
            // SAFETY: a failed vector insertion does not transfer ownership.
            unsafe { sys::llama_sampler_free(stage.as_ptr()) };
            return Err(error);
        }
        Ok(())
    }

    fn add_guarded_raw(
        &mut self,
        raw: *mut sys::llama_sampler,
        operation: &'static str,
        exception_status: i32,
    ) -> Result<(), FfiError> {
        check_native_exception(operation, exception_status)?;
        self.add_raw(raw)
    }

    pub fn add_logit_bias(&mut self, model: &Model, biases: &[(i32, f32)]) -> Result<(), FfiError> {
        let native: Vec<_> = biases
            .iter()
            .map(|(token, bias)| sys::llama_logit_bias {
                token: *token,
                bias: *bias,
            })
            .collect();
        // SAFETY: native remains live for the constructor call and llama.cpp
        // copies the bias table into the returned sampler.
        let mut exception_status = 0;
        let raw = unsafe {
            let vocab = sys::llama_model_get_vocab(model.raw().as_ptr());
            let vocab_size = sys::llama_vocab_n_tokens(vocab);
            sys::amw_ffi_sampler_init_logit_bias(
                vocab_size,
                native.len() as i32,
                native.as_ptr(),
                &mut exception_status,
            )
        };
        self.add_guarded_raw(raw, "sampler_init_logit_bias", exception_status)
    }

    /// Adds a request-local grammar stage to this chain.
    pub fn add_grammar(
        &mut self,
        model: &Model,
        grammar: &str,
        root: &str,
    ) -> Result<(), FfiError> {
        let grammar = CString::new(grammar).map_err(|_| FfiError::InteriorNul)?;
        let root = CString::new(root).map_err(|_| FfiError::InteriorNul)?;
        // SAFETY: strings remain live for construction and the vocabulary is
        // borrowed from the live model. add_raw transfers stage ownership.
        let mut exception_status = 0;
        let raw = unsafe {
            sys::amw_ffi_sampler_init_grammar(
                model.vocab(),
                grammar.as_ptr(),
                root.as_ptr(),
                &mut exception_status,
            )
        };
        check_native_exception("grammar_init", exception_status)?;
        NonNull::new(raw).ok_or(FfiError::GrammarCreate)?;
        self.add_raw(raw)
    }

    pub fn add_infill(&mut self, model: &Model) -> Result<(), FfiError> {
        if model.fim_tokens().is_none() {
            return Err(FfiError::SamplerCreate);
        }
        // SAFETY: the vocabulary is borrowed from the live model and add_raw
        // transfers ownership of the new stage to this chain.
        let mut exception_status = 0;
        let raw = unsafe { sys::amw_ffi_sampler_init_infill(model.vocab(), &mut exception_status) };
        self.add_guarded_raw(raw, "sampler_init_infill", exception_status)
    }

    pub fn add_penalties(
        &mut self,
        last_n: i32,
        repetition: f32,
        frequency: f32,
        presence: f32,
    ) -> Result<(), FfiError> {
        // SAFETY: the constructor accepts scalar configuration only.
        let mut exception_status = 0;
        let raw = unsafe {
            sys::amw_ffi_sampler_init_penalties(
                last_n,
                repetition,
                frequency,
                presence,
                &mut exception_status,
            )
        };
        self.add_guarded_raw(raw, "sampler_init_penalties", exception_status)
    }

    pub fn add_dry(
        &mut self,
        model: &Model,
        multiplier: f32,
        base: f32,
        allowed_length: i32,
        penalty_last_n: i32,
        sequence_breakers: &[&str],
    ) -> Result<(), FfiError> {
        let breakers: Vec<_> = sequence_breakers
            .iter()
            .map(|breaker| CString::new(*breaker).map_err(|_| FfiError::InteriorNul))
            .collect::<Result<_, _>>()?;
        let mut breaker_ptrs: Vec<_> = breakers.iter().map(|breaker| breaker.as_ptr()).collect();
        // SAFETY: vocab belongs to the live model used by the chain. Breaker
        // strings and the pointer table remain live for the constructor call.
        let mut exception_status = 0;
        let raw = unsafe {
            let vocab = sys::llama_model_get_vocab(model.raw().as_ptr());
            let context_train = sys::llama_model_n_ctx_train(model.raw().as_ptr());
            sys::amw_ffi_sampler_init_dry(
                vocab,
                context_train,
                multiplier,
                base,
                allowed_length,
                penalty_last_n,
                breaker_ptrs.as_mut_ptr(),
                breaker_ptrs.len(),
                &mut exception_status,
            )
        };
        self.add_guarded_raw(raw, "sampler_init_dry", exception_status)
    }

    pub fn add_top_k(&mut self, k: i32) -> Result<(), FfiError> {
        // SAFETY: the constructor accepts scalar configuration only.
        let mut exception_status = 0;
        let raw = unsafe { sys::amw_ffi_sampler_init_top_k(k, &mut exception_status) };
        self.add_guarded_raw(raw, "sampler_init_top_k", exception_status)
    }

    pub fn add_typical(&mut self, probability: f32, min_keep: usize) -> Result<(), FfiError> {
        // SAFETY: the constructor accepts scalar configuration only.
        let mut exception_status = 0;
        let raw = unsafe {
            sys::amw_ffi_sampler_init_typical(probability, min_keep, &mut exception_status)
        };
        self.add_guarded_raw(raw, "sampler_init_typical", exception_status)
    }

    pub fn add_top_p(&mut self, probability: f32, min_keep: usize) -> Result<(), FfiError> {
        // SAFETY: the constructor accepts scalar configuration only.
        let mut exception_status = 0;
        let raw = unsafe {
            sys::amw_ffi_sampler_init_top_p(probability, min_keep, &mut exception_status)
        };
        self.add_guarded_raw(raw, "sampler_init_top_p", exception_status)
    }

    pub fn add_min_p(&mut self, probability: f32, min_keep: usize) -> Result<(), FfiError> {
        // SAFETY: the constructor accepts scalar configuration only.
        let mut exception_status = 0;
        let raw = unsafe {
            sys::amw_ffi_sampler_init_min_p(probability, min_keep, &mut exception_status)
        };
        self.add_guarded_raw(raw, "sampler_init_min_p", exception_status)
    }

    pub fn add_xtc(
        &mut self,
        probability: f32,
        threshold: f32,
        min_keep: usize,
        seed: u32,
    ) -> Result<(), FfiError> {
        // SAFETY: the constructor accepts scalar configuration only.
        let mut exception_status = 0;
        let raw = unsafe {
            sys::amw_ffi_sampler_init_xtc(
                probability,
                threshold,
                min_keep,
                seed,
                &mut exception_status,
            )
        };
        self.add_guarded_raw(raw, "sampler_init_xtc", exception_status)
    }

    pub fn add_top_n_sigma(&mut self, sigma: f32) -> Result<(), FfiError> {
        // SAFETY: the constructor accepts scalar configuration only.
        let mut exception_status = 0;
        let raw = unsafe { sys::amw_ffi_sampler_init_top_n_sigma(sigma, &mut exception_status) };
        self.add_guarded_raw(raw, "sampler_init_top_n_sigma", exception_status)
    }

    pub fn add_temperature(&mut self, temperature: f32) -> Result<(), FfiError> {
        // SAFETY: the constructor accepts scalar configuration only.
        let mut exception_status = 0;
        let raw = unsafe { sys::amw_ffi_sampler_init_temp(temperature, &mut exception_status) };
        self.add_guarded_raw(raw, "sampler_init_temp", exception_status)
    }

    pub fn add_mirostat(
        &mut self,
        mode: u8,
        model: &Model,
        seed: u32,
        tau: f32,
        eta: f32,
    ) -> Result<(), FfiError> {
        // SAFETY: both constructors accept scalar configuration only. Mode is
        // validated by gen::SamplerChain before this wrapper is called.
        let mut exception_status = 0;
        let raw = unsafe {
            if mode == 1 {
                let vocab = sys::llama_model_get_vocab(model.raw().as_ptr());
                let vocab_size = sys::llama_vocab_n_tokens(vocab);
                sys::amw_ffi_sampler_init_mirostat(
                    vocab_size,
                    seed,
                    tau,
                    eta,
                    100,
                    &mut exception_status,
                )
            } else {
                sys::amw_ffi_sampler_init_mirostat_v2(seed, tau, eta, &mut exception_status)
            }
        };
        self.add_guarded_raw(raw, "sampler_init_mirostat", exception_status)
    }

    pub fn add_distribution(&mut self, seed: u32) -> Result<(), FfiError> {
        // SAFETY: the constructor accepts scalar configuration only.
        let mut exception_status = 0;
        let raw = unsafe { sys::amw_ffi_sampler_init_dist(seed, &mut exception_status) };
        self.add_guarded_raw(raw, "sampler_init_dist", exception_status)
    }

    pub fn add_greedy(&mut self) -> Result<(), FfiError> {
        // SAFETY: constructor has no preconditions.
        let mut exception_status = 0;
        let raw = unsafe { sys::amw_ffi_sampler_init_greedy(&mut exception_status) };
        self.add_guarded_raw(raw, "sampler_init_greedy", exception_status)
    }

    /// Samples one requested output row and commits the token to every stage.
    pub fn sample_and_accept(
        &mut self,
        context: &mut Context,
        output_index: i32,
    ) -> Result<i32, FfiError> {
        self.transform_sample_accept(context, output_index)
            .map(|sample| sample.token)
    }

    /// Applies the complete chain once, captures its distribution, selects once, and accepts once.
    pub fn transform_sample_accept(
        &mut self,
        context: &mut Context,
        output_index: i32,
    ) -> Result<SampledToken, FfiError> {
        let probed = self.transform(context, output_index, true)?;
        Ok(SampledToken {
            token: probed.selected_token,
            probability: probed.selected_probability,
            candidates: probed.candidates,
        })
    }

    /// Applies the complete chain on a transaction clone without accepting.
    pub fn transform_probe(
        &mut self,
        context: &mut Context,
        output_index: i32,
    ) -> Result<ProbedDistribution, FfiError> {
        self.transform(context, output_index, false)
    }

    fn transform(
        &mut self,
        context: &mut Context,
        output_index: i32,
        accept_selected: bool,
    ) -> Result<ProbedDistribution, FfiError> {
        if !context.output_was_requested(output_index) || context.vocabulary_size == 0 {
            return Err(FfiError::OutputUnavailable);
        }
        let capacity = context.vocabulary_size;
        let mut tokens = vec![0_i32; capacity];
        let mut logits = vec![0_f32; capacity];
        let mut probabilities = vec![0_f32; capacity];
        let mut candidate_count = 0_usize;
        let mut selected_token = -1_i32;
        let mut selected_probability = 0_f32;
        let mut exception_status = 0;
        // SAFETY: all output arrays have `capacity` writable elements and both
        // native wrappers remain exclusively borrowed for this operation.
        let status = unsafe {
            if accept_selected {
                sys::amw_ffi_sampler_transform_sample_accept(
                    self.raw.as_ptr(),
                    context.raw.as_ptr(),
                    output_index,
                    tokens.as_mut_ptr(),
                    logits.as_mut_ptr(),
                    probabilities.as_mut_ptr(),
                    capacity,
                    &mut candidate_count,
                    &mut selected_token,
                    &mut selected_probability,
                    &mut exception_status,
                )
            } else {
                sys::amw_ffi_sampler_transform_probe(
                    self.raw.as_ptr(),
                    context.raw.as_ptr(),
                    output_index,
                    tokens.as_mut_ptr(),
                    logits.as_mut_ptr(),
                    probabilities.as_mut_ptr(),
                    capacity,
                    &mut candidate_count,
                    &mut selected_token,
                    &mut selected_probability,
                    &mut exception_status,
                )
            }
        };
        check_native_exception(
            if accept_selected {
                "sampler_transform_sample_accept"
            } else {
                "sampler_transform_probe"
            },
            exception_status,
        )?;
        if status != 0
            || candidate_count == 0
            || candidate_count > capacity
            || selected_token < 0
            || !selected_probability.is_finite()
            || !(0.0 < selected_probability && selected_probability <= 1.0)
        {
            return Err(FfiError::OutputUnavailable);
        }
        let candidates: Vec<PostTransformCandidate> = tokens
            .into_iter()
            .zip(logits)
            .zip(probabilities)
            .take(candidate_count)
            .map(|((token, logit), probability)| PostTransformCandidate {
                token,
                logit,
                probability,
            })
            .collect();
        let valid_candidates = candidates.iter().all(|candidate| {
            candidate.token >= 0
                && usize::try_from(candidate.token)
                    .is_ok_and(|token| token < context.vocabulary_size)
                && (candidate.logit.is_finite() || candidate.logit == f32::NEG_INFINITY)
                && candidate.probability.is_finite()
                && (0.0..=1.0).contains(&candidate.probability)
        });
        let normalized_sum = candidates
            .iter()
            .map(|candidate| f64::from(candidate.probability))
            .sum::<f64>();
        let selected_matches = candidates.iter().any(|candidate| {
            candidate.token == selected_token
                && (candidate.probability - selected_probability).abs() <= 1.0e-6
        });
        if !valid_candidates || (normalized_sum - 1.0).abs() > 1.0e-4 || !selected_matches {
            return Err(FfiError::OutputUnavailable);
        }
        Ok(ProbedDistribution {
            selected_token,
            selected_probability,
            candidates,
        })
    }

    /// Commits an externally selected token to this request-local chain.
    pub fn accept(&mut self, token: i32) -> Result<(), FfiError> {
        // SAFETY: raw is a live sampler uniquely owned by this wrapper.
        let mut exception_status = 0;
        unsafe {
            sys::amw_ffi_sampler_accept(self.raw.as_ptr(), token, &mut exception_status);
        }
        check_native_exception("sampler_accept", exception_status)
    }
}

impl Drop for Sampler {
    fn drop(&mut self) {
        // SAFETY: raw is unique to this wrapper and freed exactly once here.
        unsafe { sys::llama_sampler_free(self.raw.as_ptr()) };
    }
}

pub struct Grammar {
    raw: NonNull<sys::llama_sampler>,
    _model: Model,
}

impl Grammar {
    /// Advances this request-local grammar after a sampled token is committed.
    pub fn accept(&mut self, token: i32) -> Result<(), FfiError> {
        // SAFETY: raw is a live grammar sampler uniquely owned by this wrapper.
        let mut exception_status = 0;
        unsafe {
            sys::amw_ffi_sampler_accept(self.raw.as_ptr(), token, &mut exception_status);
        }
        check_native_exception("grammar_accept", exception_status)
    }
}

impl Drop for Grammar {
    fn drop(&mut self) {
        // SAFETY: grammar samplers use the standard llama_sampler_free destructor.
        unsafe { sys::llama_sampler_free(self.raw.as_ptr()) };
    }
}

pub struct LoraAdapter {
    raw: NonNull<sys::llama_adapter_lora>,
    model: Model,
}

impl Drop for LoraAdapter {
    fn drop(&mut self) {
        // SAFETY: raw is unique to this wrapper and freed before its model.
        unsafe { sys::llama_adapter_lora_free(self.raw.as_ptr()) };
    }
}

#[cfg(test)]
mod tests {
    use std::path::Path;

    use crate::store::loader::VerifiedModelFile;

    use super::{
        check_native_exception, sys, ContextOptions, FfiError, Model, MAX_NATIVE_SEQUENCES,
    };

    #[test]
    fn verified_native_load_failure_hides_snapshot_paths() -> Result<(), String> {
        let source_path = Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("tests")
            .join("fixtures")
            .join("tiny-cpu.gguf");
        let source = VerifiedModelFile::open(&source_path).unwrap();
        let snapshot_path = source.native_path().to_owned();

        let error = match Model::load_verified(source) {
            Ok(_) => {
                return Err("test GGUF fixture unexpectedly loaded as a native model".to_owned())
            }
            Err(error) => error,
        };

        assert_eq!(error, FfiError::VerifiedModelLoad);
        assert!(!error
            .to_string()
            .contains(&source_path.display().to_string()));
        assert!(!error
            .to_string()
            .contains(&snapshot_path.display().to_string()));
        Ok(())
    }

    #[test]
    fn context_options_reject_native_sequence_output_overcommit() {
        let options = ContextOptions {
            context_tokens: MAX_NATIVE_SEQUENCES,
            batch_tokens: 64,
            max_sequences: MAX_NATIVE_SEQUENCES,
            ..ContextOptions::default()
        };
        assert_eq!(
            options.validate(),
            Err(FfiError::InvalidArgument(
                "maximum sequences cannot exceed logical batch capacity"
            ))
        );

        let options = ContextOptions {
            context_tokens: 32,
            batch_tokens: 64,
            max_sequences: 33,
            ..ContextOptions::default()
        };
        assert_eq!(
            options.validate(),
            Err(FfiError::InvalidArgument(
                "maximum sequences cannot exceed context capacity"
            ))
        );

        let options = ContextOptions {
            context_tokens: MAX_NATIVE_SEQUENCES + 1,
            batch_tokens: MAX_NATIVE_SEQUENCES + 1,
            max_sequences: MAX_NATIVE_SEQUENCES + 1,
            ..ContextOptions::default()
        };
        assert_eq!(
            options.validate(),
            Err(FfiError::InvalidArgument(
                "maximum sequences exceeds llama.cpp identity limit"
            ))
        );

        let options = ContextOptions {
            context_tokens: 64,
            batch_tokens: 64,
            max_sequences: 64,
            unified_kv: true,
            ..ContextOptions::default()
        };
        assert_eq!(options.validate(), Ok(()));
    }

    #[test]
    fn cpp_exceptions_are_translated_before_crossing_the_rust_abi() {
        let mut status = 0;
        // SAFETY: this purpose-built seam takes only a valid status pointer and
        // deterministically throws inside the C++ exception firewall.
        let fallback = unsafe { sys::amw_ffi_test_exception_firewall(&mut status) };
        assert_eq!(fallback, 0);
        let error = check_native_exception("firewall_probe", status).unwrap_err();
        assert!(matches!(
            error,
            FfiError::NativeException {
                operation: "firewall_probe",
                ref message,
            } if message == "deterministic exception-firewall probe"
        ));
    }

    #[test]
    fn every_native_output_wrapper_contains_injected_cpp_exceptions() {
        for (injection, operation, call) in [
            (
                1,
                "get_logits_ith",
                sys::amw_ffi_get_logits_ith
                    as unsafe extern "C" fn(*mut sys::llama_context, i32, *mut i32) -> *const f32,
            ),
            (
                2,
                "get_embeddings_ith",
                sys::amw_ffi_get_embeddings_ith
                    as unsafe extern "C" fn(*mut sys::llama_context, i32, *mut i32) -> *const f32,
            ),
            (
                3,
                "get_embeddings_seq",
                sys::amw_ffi_get_embeddings_seq
                    as unsafe extern "C" fn(*mut sys::llama_context, i32, *mut i32) -> *const f32,
            ),
        ] {
            let mut status = 0;
            // SAFETY: injection is thread-local and is consumed before the wrapper
            // dereferences the deliberately-null context test argument.
            let output = unsafe {
                sys::amw_ffi_test_inject_output_exception(injection);
                call(std::ptr::null_mut(), 0, &mut status)
            };
            assert!(output.is_null());
            assert!(matches!(
                check_native_exception(operation, status),
                Err(FfiError::NativeException {
                    operation: actual,
                    ref message,
                }) if actual == operation && message == "deterministic native output exception probe"
            ));
        }
    }
}
