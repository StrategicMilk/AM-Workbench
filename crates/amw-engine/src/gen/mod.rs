//! Native generation primitives shared by the scheduler and API layers.
//!
//! This module deliberately owns request-local generation state. Scheduling,
//! KV-arena ownership, and transport framing remain in their respective layers.

mod embed;
mod execution;
mod grammar;
mod infill;
mod logprobs;
mod sampler;
mod spec;
mod stop;
mod stream;

#[cfg(any(feature = "cpu", feature = "cuda"))]
pub use embed::NativeEmbeddingBackend;
pub use embed::{
    execute_embedding_batch, normalize_embedding, normalize_embedding_batch, pool_embedding,
    EmbeddingBackend, EmbeddingInput, EmbeddingOptions, PoolingMode,
};
pub use execution::{
    DecodeBackend, DistributionCandidate, GenerationControl, GenerationControlState,
    GenerationEvent, GenerationExecutor, GenerationPlan, GenerationStep, GenerationUsage,
    NativeGenerationConfig, SamplingResult, StepOutcome,
};
#[cfg(any(feature = "cpu", feature = "cuda"))]
pub use execution::{
    ExternalBundleOutcome, ExternalBundlePreview, ExternalBundleToken, NativeDecodeBackend,
    NativeSamplerTxn,
};
#[cfg(any(feature = "cpu", feature = "cuda"))]
pub use grammar::ActiveGrammar;
pub use grammar::{
    CompiledGrammar, GRAMMAR_COMPILE_BUDGET, GRAMMAR_NATIVE_COMPILE_TIMEOUT,
    MAX_GRAMMAR_ACCEPTED_TOKENS, MAX_GRAMMAR_BYTES, MAX_GRAMMAR_NESTING, MAX_GRAMMAR_RULES,
};
pub use infill::{assemble_infill, FimTokenMap, FimTokenMetadata, ModelFamily};
pub use logprobs::{
    capture_top_logprobs, LogitCandidate, TokenLogprob, MAX_LOGIT_CANDIDATES,
    MAX_LOGIT_METADATA_BYTES, MAX_TOP_LOGPROBS,
};
pub use sampler::{SamplerCapabilities, SamplerChain, SamplerParams, SamplerStage};
pub use spec::{
    resolve_draft_mode, DraftJob, DraftMode, DraftModelBackend, DraftModelCompatibility,
    DraftModelProposer, DraftProposal, DraftResult, ProbabilityRow, PromptLookupProposer,
    ProposalSource, ProposedToken, SpeculationCounters, SpeculationDecision,
    SpeculationEligibility, SpeculationIneligibleReason, SpeculationPlan, TargetProbe,
    TargetVerification, TokenProbability, MAX_DRAFT_ACTOR_IN_FLIGHT, MAX_DRAFT_TOKENS,
    MAX_PROBABILITY_CANDIDATES, MAX_PROBABILITY_ROW_BYTES, MAX_SPECULATION_REQUEST_RETAINED_BYTES,
    MAX_SPECULATION_WORKER_RETAINED_BYTES, MAX_SPECULATIVE_HISTORY, MAX_SPECULATIVE_KV_STATE_BYTES,
    PROBABILITY_NORMALIZATION_TOLERANCE,
};
#[cfg(any(feature = "cpu", feature = "cuda"))]
pub use spec::{NativeDraftBackend, TargetKvFork};
pub use stop::{StopDecision, StopEvaluator, StopObservation, StopReason};
pub use stream::{
    bounded_generation_stream, bounded_token_stream, GenerationReceiver, GenerationSender,
    TokenReceiver, TokenSender, MAX_GENERATION_EVENT_BYTES, OUTPUT_CHANNEL_BYTE_CAPACITY,
    OUTPUT_CHANNEL_CAPACITY,
};

use thiserror::Error;

/// Stable failure categories carried from the model worker to the API stream.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum GenerationFailureCode {
    /// The native backend required by the loaded model is unavailable.
    BackendUnavailable,
    /// A native allocation failed before generation could begin.
    AllocationFailed,
    /// The bounded model-worker admission queue cannot accept another request.
    QueueFull,
    /// The engine is shutting down and cannot continue queued or active work.
    Draining,
    /// Native memory allocation or capacity reservation was refused.
    Oom,
    /// A referenced persistent session does not exist.
    SessionUnknown,
    /// Evaluation exceeded its request deadline.
    EvalTimeout,
    /// Model bytes or metadata failed integrity or readability checks.
    ModelCorrupt,
    /// The requested model is not present in the governed registry.
    ModelNotLoaded,
    /// A governed storage or tenant resource quota was exhausted.
    QuotaExhausted,
    /// The request was cancelled before a successful terminal event.
    Cancelled,
    /// An internal runtime invariant failed without a safe client diagnostic.
    Internal,
}

/// Typed failures consumed by the scheduler and mapped by the API error layer.
#[derive(Clone, Debug, Error, PartialEq)]
pub enum GenError {
    /// A requested sampler feature is unavailable in the active backend.
    #[error("unsupported sampler parameter: {0}")]
    UnsupportedParam(&'static str),
    /// A sampler value cannot be represented safely or violates its native domain.
    #[error("invalid sampler parameter {0}: {1}")]
    InvalidSamplerParam(&'static str, &'static str),
    /// llama.cpp refused to construct a validated native sampler stage.
    #[error("native sampler construction failed: {0}")]
    NativeSampler(String),
    /// GBNF input failed bounded validation for one request.
    #[error("invalid grammar: {0}")]
    GrammarInvalid(String),
    /// The loaded model does not advertise all required FIM sentinels.
    #[error("the loaded model does not support fill-in-the-middle generation")]
    FimUnsupported,
    /// An embedding was empty or contained a non-finite value.
    #[error("invalid embedding vector")]
    InvalidEmbedding,
    /// Backend output logits were missing or malformed.
    #[error("invalid logits: {0}")]
    InvalidLogits(&'static str),
    /// Prompt and requested output tokens exceed the loaded context window.
    #[error("context window overflow: requested {requested} tokens exceeds limit {limit}")]
    ContextOverflow {
        /// Total prompt and output tokens requested by the caller.
        requested: u32,
        /// Maximum token count supported by the active context.
        limit: u32,
    },
    /// A typed model-worker failure that must survive asynchronous streaming.
    #[error("{message}")]
    RuntimeFailure {
        /// Stable category used for exhaustive API error mapping.
        code: GenerationFailureCode,
        /// Human-readable diagnostic with retained allocation accounted by the stream.
        message: String,
    },
    /// A generation backend failed at a request-local boundary.
    #[error("generation backend failed: {0}")]
    Backend(String),
    /// Speculative verification inputs or transaction state were invalid.
    #[error("invalid speculative decoding state: {0}")]
    SpeculationInvalid(&'static str),
    /// Byte-exact target KV restoration failed; the shared native worker is unsafe to reuse.
    #[error("speculative decoding invalidated the shared native context: {0}")]
    SpeculationContextInvalidated(String),
    /// Native or parsed grammar state exceeded its per-request resource bound.
    #[error("grammar resource limit exceeded: {0}")]
    GrammarResourceLimit(&'static str),
    /// FIM metadata contained invalid or ambiguous sentinel token identifiers.
    #[error("invalid fill-in-the-middle sentinels: {0}")]
    InvalidFimSentinels(&'static str),
    /// Stop strings or decoded token bytes violate request-local bounds.
    #[error("invalid stop configuration or output: {0}")]
    InvalidStop(&'static str),
    /// The sequence consumer disappeared.
    #[error("generation stream receiver was dropped")]
    StreamDisconnected,
    /// The bounded stream could not accept another token.
    #[error("generation stream reached its backpressure bound")]
    Backpressure,
    /// One event was too large for the per-sequence byte budget.
    #[error("generation event exceeds the per-event byte limit")]
    EventTooLarge,
}

impl GenError {
    fn retained_bytes(&self) -> usize {
        match self {
            Self::NativeSampler(message)
            | Self::GrammarInvalid(message)
            | Self::Backend(message)
            | Self::SpeculationContextInvalidated(message)
            | Self::RuntimeFailure { message, .. } => message.capacity(),
            _ => 0,
        }
    }
}
