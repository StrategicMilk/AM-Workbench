//! Per-sequence execution façade consumed once per scheduler decode boundary.

use std::{
    collections::VecDeque,
    sync::{
        atomic::{AtomicU8, Ordering},
        Arc,
    },
    time::Instant,
};

use super::{
    GenError, GenerationSender, SamplerCapabilities, SamplerChain, SamplerParams, StopDecision,
    StopEvaluator, StopReason, TokenLogprob,
};

#[cfg(any(feature = "cpu", feature = "cuda"))]
use super::{ProbabilityRow, TargetProbe, TokenProbability};

/// Token accounting emitted with terminal generation events.
#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub struct GenerationUsage {
    /// Prompt tokens evaluated before generation.
    pub prompt_tokens: usize,
    /// Tokens sampled by this sequence.
    pub completion_tokens: usize,
}

/// Typed event carried by the bounded scheduler-to-transport stream.
#[derive(Clone, Debug, PartialEq)]
pub enum GenerationEvent {
    /// One safe-to-emit decoded delta.
    Delta {
        /// Sampled native token identifier.
        token_id: i32,
        /// Exact decoded bytes, with potential stop prefixes withheld.
        bytes: Vec<u8>,
        /// Selected token's normalized natural-log probability when captured.
        logprob: Option<f32>,
        /// Highest normalized candidates for the same decode boundary.
        top_logprobs: Vec<TokenLogprob>,
    },
    /// Normal sequence termination.
    Finished {
        /// Boundary that terminated generation.
        reason: StopReason,
        /// Final prompt/completion token accounting.
        usage: GenerationUsage,
        /// Geometric-mean selected-token probability when at least one token was sampled.
        confidence: Option<f32>,
    },
    /// Request-local failure; other scheduler sequences remain valid.
    Failed(GenError),
}

impl GenerationEvent {
    pub(crate) fn retained_bytes(&self) -> usize {
        match self {
            Self::Delta {
                bytes,
                top_logprobs,
                ..
            } => bytes
                .capacity()
                .saturating_add(
                    top_logprobs
                        .capacity()
                        .saturating_mul(std::mem::size_of::<TokenLogprob>()),
                )
                .saturating_add(top_logprobs.iter().fold(0_usize, |total, entry| {
                    total.saturating_add(entry.bytes.capacity())
                }))
                .saturating_add(std::mem::size_of::<Self>()),
            Self::Finished { .. } => std::mem::size_of::<Self>(),
            Self::Failed(error) => {
                std::mem::size_of::<Self>().saturating_add(error.retained_bytes())
            }
        }
    }
}

/// Externally observable request control state.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum GenerationControlState {
    /// Decode may continue.
    Running,
    /// Caller explicitly cancelled the request.
    Cancelled,
    /// Transport receiver was dropped.
    Disconnected,
    /// Request deadline elapsed.
    DeadlineExceeded,
}

#[derive(Debug)]
struct ControlInner {
    state: AtomicU8,
    deadline: Option<Instant>,
}

/// Cloneable cancellation/disconnect/deadline signal checked every decode boundary.
#[derive(Clone, Debug)]
pub struct GenerationControl(Arc<ControlInner>);

impl GenerationControl {
    /// Constructs a running signal with an optional monotonic deadline.
    pub fn new(deadline: Option<Instant>) -> Self {
        Self(Arc::new(ControlInner {
            state: AtomicU8::new(0),
            deadline,
        }))
    }

    /// Requests cancellation. A prior disconnect remains authoritative.
    pub fn cancel(&self) {
        let _ = self
            .0
            .state
            .compare_exchange(0, 1, Ordering::AcqRel, Ordering::Acquire);
    }

    /// Returns the effective state, including monotonic deadline expiry.
    pub fn state(&self) -> GenerationControlState {
        match self.0.state.load(Ordering::Acquire) {
            1 => GenerationControlState::Cancelled,
            2 => GenerationControlState::Disconnected,
            _ if self
                .0
                .deadline
                .is_some_and(|deadline| Instant::now() >= deadline) =>
            {
                GenerationControlState::DeadlineExceeded
            }
            _ => GenerationControlState::Running,
        }
    }

    pub(crate) fn disconnect(&self) {
        self.0.state.store(2, Ordering::Release);
    }
}

impl Default for GenerationControl {
    fn default() -> Self {
        Self::new(None)
    }
}

/// Immutable execution choices applied at every decode step.
#[derive(Clone, Debug, PartialEq)]
pub struct GenerationPlan {
    /// Validated sampler descriptor.
    pub sampler: SamplerChain,
    /// Number of top logprobs returned per generated token.
    pub top_logprobs: usize,
}

/// Coherent native sampler and grammar configuration for one sequence.
///
/// Attention is deliberately absent: the pinned llama.cpp model graph selects
/// dense, sliding-window, hybrid, or DSA/ISWA attention from GGUF architecture
/// metadata during context construction. The engine has no supported per-step
/// override and therefore does not expose a label-only route.
#[derive(Clone, Copy, Debug)]
pub struct NativeGenerationConfig<'a> {
    /// Complete validated sampler parameters.
    pub params: &'a SamplerParams,
    /// Pinned-backend feature support.
    pub capabilities: SamplerCapabilities,
    /// Optional request-local compiled grammar.
    pub grammar: Option<&'a super::CompiledGrammar>,
    /// Requested top-logprob count.
    pub top_logprobs: usize,
    /// Number of evaluated prompt tokens.
    pub prompt_tokens: usize,
}

impl GenerationPlan {
    /// Builds one request plan from validated sampler and response controls.
    pub fn build(
        params: &SamplerParams,
        capabilities: SamplerCapabilities,
        top_logprobs: usize,
    ) -> Result<Self, GenError> {
        if top_logprobs > super::logprobs::MAX_TOP_LOGPROBS {
            return Err(GenError::InvalidLogits(
                "requested top-logprob count exceeds the bound",
            ));
        }
        Ok(Self {
            sampler: SamplerChain::build(params, capabilities)?,
            top_logprobs,
        })
    }
}

/// Backend output made available after one native decode call.
#[derive(Clone, Debug, PartialEq)]
pub struct GenerationStep {
    /// Native output row to sample.
    pub output_index: i32,
}

/// One candidate in the post-transform sampler distribution.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct DistributionCandidate {
    pub token_id: i32,
    pub logit: f32,
    pub probability: f32,
}

/// Exactly one selected and accepted token plus the distribution that selected it.
#[derive(Clone, Debug, PartialEq)]
pub struct SamplingResult {
    pub token_id: i32,
    pub probability: f32,
    pub candidates: Vec<DistributionCandidate>,
}

/// One externally verified token and the target row that assigned its log probability.
#[cfg(any(feature = "cpu", feature = "cuda"))]
#[derive(Clone, Debug, PartialEq)]
pub struct ExternalBundleToken {
    /// Native vocabulary token identifier to emit.
    pub token_id: i32,
    /// Complete target distribution at this conditional decode boundary.
    pub distribution: ProbabilityRow,
    /// Native sampler probe state that must be committed for this token.
    pub sampler_probe_index: usize,
}

/// Side-effect-free stop/logprob preview for one verified multi-token bundle.
#[cfg(any(feature = "cpu", feature = "cuda"))]
#[derive(Clone, Debug)]
pub struct ExternalBundlePreview {
    tokens: Box<[PreparedExternalToken]>,
    terminal_reason: Option<StopReason>,
}

#[cfg(any(feature = "cpu", feature = "cuda"))]
impl ExternalBundlePreview {
    /// Returns the number of output tokens retained after stop/EOG/max truncation.
    pub fn output_tokens(&self) -> usize {
        self.tokens.len()
    }

    /// Returns the number of tokens represented in KV before the final pending token.
    pub fn kv_tokens(&self) -> usize {
        self.tokens.len().saturating_sub(1)
    }

    /// Returns the terminal reason detected during preview, if any.
    pub fn terminal_reason(&self) -> Option<&StopReason> {
        self.terminal_reason.as_ref()
    }

    /// Returns the final emitted token, which remains pending outside target KV.
    pub fn pending_token(&self) -> i32 {
        self.tokens.last().map_or(-1, |token| token.token_id)
    }
}

/// Result of atomically committing a verified external token bundle.
#[cfg(any(feature = "cpu", feature = "cuda"))]
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ExternalBundleOutcome {
    /// Scheduler-visible continuation or terminal boundary.
    pub outcome: StepOutcome,
    /// Exact output-token progress committed by this bundle.
    pub output_tokens: usize,
    /// Exact accepted-prefix tokens installed in target KV.
    pub kv_tokens: usize,
    /// Final emitted token left pending for the next decode boundary.
    pub pending_token: i32,
}

#[cfg(any(feature = "cpu", feature = "cuda"))]
#[derive(Clone, Debug)]
struct PreparedExternalToken {
    token_id: i32,
    token_bytes: Vec<u8>,
    selected_logprob: Option<f32>,
    top_logprobs: Vec<TokenLogprob>,
    sampler_probe_index: usize,
}

/// Native operations required by the request-local executor.
pub trait DecodeBackend {
    /// Applies all transforms once, selects once, and accepts exactly that token once.
    fn transform_sample_accept(&mut self, output_index: i32) -> Result<SamplingResult, GenError>;
    /// Accepts an externally selected replacement token into every sampler stage.
    fn accept(&mut self, token: i32) -> Result<(), GenError>;
    /// Converts one native token to its exact byte representation.
    fn token_piece(&mut self, token: i32) -> Result<Vec<u8>, GenError>;
}

/// Native llama.cpp request-local sampler state.
///
/// The backend intentionally does not borrow a context. The scheduler owns the
/// shared context and may batch multiple sequences in one native decode call,
/// then pass each output row to its request-local executor.
#[cfg(any(feature = "cpu", feature = "cuda"))]
pub struct NativeDecodeBackend {
    sampler: crate::ffi::Sampler,
}

/// Fallible native sampler transaction used only for speculative target probing.
#[cfg(any(feature = "cpu", feature = "cuda"))]
pub struct NativeSamplerTxn {
    working: crate::ffi::Sampler,
    probed_states: Vec<crate::ffi::Sampler>,
}

#[cfg(any(feature = "cpu", feature = "cuda"))]
impl NativeSamplerTxn {
    /// Applies the cloned sampler to one target output without accepting its selected token.
    pub fn probe_distribution(
        &mut self,
        context: &mut crate::ffi::Context,
        output_index: i32,
    ) -> Result<TargetProbe, GenError> {
        let probed = self
            .working
            .transform_probe(context, output_index)
            .map_err(|error| GenError::Backend(error.to_string()))?;
        let distribution = ProbabilityRow::new(
            context.metadata().vocabulary_size,
            probed
                .candidates
                .into_iter()
                .map(|candidate| TokenProbability {
                    token_id: candidate.token,
                    probability: candidate.probability,
                })
                .collect(),
        )?;
        if distribution.probability(probed.selected_token) <= 0.0 {
            return Err(GenError::SpeculationInvalid(
                "native target sampler selected a token with no probability mass",
            ));
        }
        self.probed_states.push(
            self.working
                .try_clone()
                .map_err(|_| GenError::SpeculationInvalid("target sampler cannot be cloned"))?,
        );
        Ok(TargetProbe {
            selected_token: probed.selected_token,
            distribution,
        })
    }

    /// Advances conditional sampler stages with an accepted proposal token.
    pub fn accept_proposal(&mut self, token: i32) -> Result<(), GenError> {
        self.working
            .accept(token)
            .map_err(|error| GenError::Backend(error.to_string()))
    }

    fn finalize(
        self,
        sampler_probe_index: usize,
        pending_token: i32,
    ) -> Result<crate::ffi::Sampler, GenError> {
        let mut sampler = self
            .probed_states
            .into_iter()
            .nth(sampler_probe_index)
            .ok_or(GenError::SpeculationInvalid(
                "bundle references an unavailable sampler probe state",
            ))?;
        sampler
            .accept(pending_token)
            .map_err(|error| GenError::Backend(error.to_string()))?;
        Ok(sampler)
    }
}

#[cfg(any(feature = "cpu", feature = "cuda"))]
impl NativeDecodeBackend {
    /// Constructs request-local state from a complete native sampler chain.
    pub fn new(sampler: crate::ffi::Sampler) -> Self {
        Self { sampler }
    }
}

/// Result returned to the scheduler after one committed decode boundary.
#[derive(Clone, Debug, Eq, PartialEq)]
pub enum StepOutcome {
    /// One token was committed; scheduler may enqueue another decode step.
    Continue { token_id: i32 },
    /// Sequence reached a terminal boundary.
    Finished(StopReason),
}

/// Concrete request-local generation state; scheduler owns only ordering.
pub struct GenerationExecutor<B> {
    backend: B,
    plan: GenerationPlan,
    stop: StopEvaluator,
    sender: GenerationSender,
    control: GenerationControl,
    usage: GenerationUsage,
    last_sampled_token: Option<i32>,
    selected_logprobs: Vec<f32>,
    pending_deltas: VecDeque<PendingDelta>,
}

#[derive(Clone, Debug)]
struct PendingDelta {
    token_id: i32,
    bytes: Vec<u8>,
    offset: usize,
    logprob: Option<f32>,
    top_logprobs: Vec<TokenLogprob>,
}

#[cfg(any(feature = "cpu", feature = "cuda"))]
#[derive(Clone, Debug)]
struct ExecutionStateCheckpoint {
    stop: StopEvaluator,
    usage: GenerationUsage,
    last_sampled_token: Option<i32>,
    selected_logprobs: Vec<f32>,
    pending_deltas: VecDeque<PendingDelta>,
}

impl<B> GenerationExecutor<B> {
    /// Constructs an executor from validated state and a bounded event sender.
    pub fn new(
        backend: B,
        plan: GenerationPlan,
        stop: StopEvaluator,
        sender: GenerationSender,
        control: GenerationControl,
        prompt_tokens: usize,
    ) -> Self {
        Self {
            backend,
            plan,
            stop,
            sender,
            control,
            usage: GenerationUsage {
                prompt_tokens,
                completion_tokens: 0,
            },
            last_sampled_token: None,
            selected_logprobs: Vec::new(),
            pending_deltas: VecDeque::new(),
        }
    }

    fn commit_sample(
        &mut self,
        token: i32,
        token_bytes: Vec<u8>,
        selected_logprob: Option<f32>,
        top_logprobs: Vec<TokenLogprob>,
    ) -> Result<(usize, StopDecision), GenError> {
        self.usage.completion_tokens = self.usage.completion_tokens.saturating_add(1);
        self.last_sampled_token = Some(token);
        if let Some(logprob) = selected_logprob {
            self.selected_logprobs.push(logprob);
        }
        let observation = self.stop.observe_bytes(token, &token_bytes)?;
        self.pending_deltas.push_back(PendingDelta {
            token_id: token,
            bytes: token_bytes,
            offset: 0,
            logprob: selected_logprob,
            top_logprobs,
        });
        Ok((observation.emit.len(), observation.decision))
    }

    async fn after_sample(
        &mut self,
        token: i32,
        token_bytes: Vec<u8>,
        selected_logprob: Option<f32>,
        top_logprobs: Vec<TokenLogprob>,
    ) -> Result<StepOutcome, GenError> {
        let (emit_bytes, decision) =
            self.commit_sample(token, token_bytes, selected_logprob, top_logprobs)?;
        self.emit_safe(emit_bytes).await?;
        match decision {
            StopDecision::Continue => Ok(StepOutcome::Continue { token_id: token }),
            StopDecision::Stop(reason) => {
                self.pending_deltas.clear();
                self.finish(reason.clone()).await?;
                Ok(StepOutcome::Finished(reason))
            }
        }
    }

    fn after_sample_try(
        &mut self,
        token: i32,
        token_bytes: Vec<u8>,
        selected_logprob: Option<f32>,
        top_logprobs: Vec<TokenLogprob>,
    ) -> Result<StepOutcome, GenError> {
        let (emit_bytes, decision) =
            self.commit_sample(token, token_bytes, selected_logprob, top_logprobs)?;
        self.emit_safe_try(emit_bytes)?;
        match decision {
            StopDecision::Continue => Ok(StepOutcome::Continue { token_id: token }),
            StopDecision::Stop(reason) => {
                self.pending_deltas.clear();
                self.finish_try(reason.clone())?;
                Ok(StepOutcome::Finished(reason))
            }
        }
    }

    /// Emits a terminal failure without converting it into normal completion.
    pub async fn fail(&self, error: GenError) -> Result<(), GenError> {
        self.sender.send(GenerationEvent::Failed(error)).await
    }

    /// Returns shared cancellation/disconnect/deadline control.
    pub fn control(&self) -> GenerationControl {
        self.control.clone()
    }

    /// Returns a reference to the native/backend adapter for diagnostics.
    pub fn backend(&self) -> &B {
        &self.backend
    }

    /// Returns authoritative token accounting even if terminal stream delivery failed.
    pub fn usage(&self) -> GenerationUsage {
        self.usage
    }

    #[cfg(any(feature = "cpu", feature = "cuda"))]
    fn state_checkpoint(&self) -> ExecutionStateCheckpoint {
        ExecutionStateCheckpoint {
            stop: self.stop.clone(),
            usage: self.usage,
            last_sampled_token: self.last_sampled_token,
            selected_logprobs: self.selected_logprobs.clone(),
            pending_deltas: self.pending_deltas.clone(),
        }
    }

    #[cfg(any(feature = "cpu", feature = "cuda"))]
    fn restore_state(&mut self, checkpoint: ExecutionStateCheckpoint) {
        self.stop = checkpoint.stop;
        self.usage = checkpoint.usage;
        self.last_sampled_token = checkpoint.last_sampled_token;
        self.selected_logprobs = checkpoint.selected_logprobs;
        self.pending_deltas = checkpoint.pending_deltas;
    }

    /// Returns the most recent token committed by this request-local sampler.
    ///
    /// Scheduler session persistence uses this even when the same token reaches
    /// a terminal stop boundary and [`StepOutcome::Finished`] carries no token.
    pub fn last_sampled_token(&self) -> Option<i32> {
        self.last_sampled_token
    }

    /// Emits a nonblocking terminal event when request control is no longer running.
    ///
    /// Synchronous scheduler actors call this during their pre-decode control
    /// sweep. A returned outcome is authoritative: no sampler or stop state was
    /// advanced, and [`Self::usage`] remains the final token accounting.
    pub fn finish_from_control_try(&self) -> Result<Option<StepOutcome>, GenError> {
        let Some(reason) = control_stop_reason(self.control.state()) else {
            return Ok(None);
        };
        self.finish_try(reason.clone())?;
        Ok(Some(StepOutcome::Finished(reason)))
    }

    fn finished_event(&self, reason: StopReason) -> GenerationEvent {
        let confidence = if self.selected_logprobs.is_empty() {
            None
        } else {
            let mean = self
                .selected_logprobs
                .iter()
                .map(|value| f64::from(*value))
                .sum::<f64>()
                / self.selected_logprobs.len() as f64;
            Some(mean.exp().clamp(0.0, 1.0) as f32)
        };
        GenerationEvent::Finished {
            reason,
            usage: self.usage,
            confidence,
        }
    }

    async fn finish(&self, reason: StopReason) -> Result<(), GenError> {
        self.sender.send(self.finished_event(reason)).await
    }

    fn finish_try(&self, reason: StopReason) -> Result<(), GenError> {
        self.sender.try_send(self.finished_event(reason))
    }

    async fn emit_safe(&mut self, mut byte_count: usize) -> Result<(), GenError> {
        while byte_count > 0 {
            let event = self.take_safe_event(&mut byte_count)?;
            self.sender.send(event).await?;
        }
        Ok(())
    }

    fn emit_safe_try(&mut self, mut byte_count: usize) -> Result<(), GenError> {
        while byte_count > 0 {
            let event = self.take_safe_event(&mut byte_count)?;
            self.sender.try_send(event)?;
        }
        Ok(())
    }

    fn take_safe_event(&mut self, byte_count: &mut usize) -> Result<GenerationEvent, GenError> {
        loop {
            let Some(front) = self.pending_deltas.front_mut() else {
                return Err(GenError::InvalidStop(
                    "stop matcher emitted bytes absent from pending token state",
                ));
            };
            let remaining = front.bytes.len().saturating_sub(front.offset);
            let take = remaining.min(*byte_count);
            if take == 0 {
                self.pending_deltas.pop_front();
                continue;
            }
            let first_segment = front.offset == 0;
            let event = GenerationEvent::Delta {
                token_id: front.token_id,
                bytes: front.bytes[front.offset..front.offset + take].to_vec(),
                logprob: front.logprob,
                top_logprobs: if first_segment {
                    std::mem::take(&mut front.top_logprobs)
                } else {
                    Vec::new()
                },
            };
            front.offset += take;
            *byte_count -= take;
            if front.offset == front.bytes.len() {
                self.pending_deltas.pop_front();
            }
            return Ok(event);
        }
    }
}

impl<B: DecodeBackend> GenerationExecutor<B> {
    /// Samples, renders, evaluates stopping, and emits one bounded event.
    pub async fn after_decode(&mut self, step: GenerationStep) -> Result<StepOutcome, GenError> {
        let result = self.after_decode_inner(step).await;
        if let Err(error) = &result {
            let _ = self
                .sender
                .send(GenerationEvent::Failed(error.clone()))
                .await;
        }
        result
    }

    async fn after_decode_inner(&mut self, step: GenerationStep) -> Result<StepOutcome, GenError> {
        if let Some(reason) = control_stop_reason(self.control.state()) {
            self.finish(reason.clone()).await?;
            return Ok(StepOutcome::Finished(reason));
        }
        let sampled = self.backend.transform_sample_accept(step.output_index)?;
        let token_bytes = self.backend.token_piece(sampled.token_id)?;
        if let Some(reason) = control_stop_reason(self.control.state()) {
            self.pending_deltas.clear();
            self.finish(reason.clone()).await?;
            return Ok(StepOutcome::Finished(reason));
        }
        let (selected_logprob, top_logprobs) =
            normalize_distribution(&sampled, self.plan.top_logprobs, |candidate| {
                self.backend.token_piece(candidate)
            })?;
        self.after_sample(
            sampled.token_id,
            token_bytes,
            selected_logprob,
            top_logprobs,
        )
        .await
    }

    /// Executes one decoded row without waiting for transport capacity.
    ///
    /// Backpressure and disconnect errors are request-local terminal signals for
    /// synchronous scheduler actors; callers must remove only this sequence.
    pub fn after_decode_try(&mut self, step: GenerationStep) -> Result<StepOutcome, GenError> {
        let result = self.after_decode_try_inner(step);
        if let Err(error) = &result {
            let _ = self.sender.try_send(GenerationEvent::Failed(error.clone()));
        }
        result
    }

    fn after_decode_try_inner(&mut self, step: GenerationStep) -> Result<StepOutcome, GenError> {
        if let Some(reason) = control_stop_reason(self.control.state()) {
            self.finish_try(reason.clone())?;
            return Ok(StepOutcome::Finished(reason));
        }
        let sampled = self.backend.transform_sample_accept(step.output_index)?;
        let token_bytes = self.backend.token_piece(sampled.token_id)?;
        if let Some(reason) = control_stop_reason(self.control.state()) {
            self.pending_deltas.clear();
            self.finish_try(reason.clone())?;
            return Ok(StepOutcome::Finished(reason));
        }
        let (selected_logprob, top_logprobs) =
            normalize_distribution(&sampled, self.plan.top_logprobs, |candidate| {
                self.backend.token_piece(candidate)
            })?;
        self.after_sample_try(
            sampled.token_id,
            token_bytes,
            selected_logprob,
            top_logprobs,
        )
    }

    /// Commits a target-selected speculative replacement into sampler state.
    pub fn accept_external(&mut self, token: i32) -> Result<(), GenError> {
        self.backend.accept(token)
    }
}

#[cfg(any(feature = "cpu", feature = "cuda"))]
impl GenerationExecutor<NativeDecodeBackend> {
    /// Builds one coherent native executor so descriptor and native sampler cannot drift.
    pub fn new_native(
        model: &crate::ffi::Model,
        config: NativeGenerationConfig<'_>,
        stop: StopEvaluator,
        sender: GenerationSender,
        control: GenerationControl,
    ) -> Result<Self, GenError> {
        let plan = GenerationPlan::build(config.params, config.capabilities, config.top_logprobs)?;
        let sampler = SamplerChain::build_native_with_grammar(
            config.params,
            config.capabilities,
            model,
            config.grammar,
        )?;
        Ok(Self::new(
            NativeDecodeBackend::new(sampler),
            plan,
            stop,
            sender,
            control,
            config.prompt_tokens,
        ))
    }

    /// Clones every native sampler stage before speculative target probing begins.
    pub fn begin_speculative_sampler(&self) -> Result<NativeSamplerTxn, GenError> {
        let working = self
            .backend
            .sampler
            .try_clone()
            .map_err(|_| GenError::SpeculationInvalid("target sampler cannot be cloned"))?;
        Ok(NativeSamplerTxn {
            working,
            probed_states: Vec::with_capacity(super::MAX_DRAFT_TOKENS + 1),
        })
    }

    /// Probes one complete target distribution through a transaction-owned sampler clone.
    pub fn probe_distribution(
        &self,
        transaction: &mut NativeSamplerTxn,
        context: &mut crate::ffi::Context,
        output_index: i32,
    ) -> Result<TargetProbe, GenError> {
        transaction.probe_distribution(context, output_index)
    }

    /// Previews rendering, logprobs, and stop truncation without mutating executor state.
    pub fn preview_external_bundle(
        &self,
        model: &crate::ffi::Model,
        tokens: &[ExternalBundleToken],
    ) -> Result<ExternalBundlePreview, GenError> {
        if tokens.is_empty() || tokens.len() > super::MAX_DRAFT_TOKENS + 1 {
            return Err(GenError::SpeculationInvalid(
                "external bundle is empty or exceeds the speculation bound",
            ));
        }
        if let Some(reason) = control_stop_reason(self.control.state()) {
            return Err(GenError::Backend(format!(
                "external bundle cannot be previewed after {reason:?}"
            )));
        }
        let mut stop = self.stop.clone();
        let mut prepared = Vec::with_capacity(tokens.len());
        let mut terminal_reason = None;
        for token in tokens {
            let selected_probability = token.distribution.probability(token.token_id);
            if selected_probability <= 0.0 {
                return Err(GenError::SpeculationInvalid(
                    "external token has no target probability mass",
                ));
            }
            let token_bytes = model
                .token_piece(token.token_id, false)
                .map_err(|error| GenError::Backend(error.to_string()))?;
            let sampled = SamplingResult {
                token_id: token.token_id,
                probability: selected_probability,
                candidates: token
                    .distribution
                    .candidates()
                    .iter()
                    .map(|candidate| DistributionCandidate {
                        token_id: candidate.token_id,
                        logit: 0.0,
                        probability: candidate.probability,
                    })
                    .collect(),
            };
            let (selected_logprob, top_logprobs) =
                normalize_distribution(&sampled, self.plan.top_logprobs, |candidate| {
                    model
                        .token_piece(candidate, false)
                        .map_err(|error| GenError::Backend(error.to_string()))
                })?;
            let observation = stop.observe_bytes(token.token_id, &token_bytes)?;
            prepared.push(PreparedExternalToken {
                token_id: token.token_id,
                token_bytes,
                selected_logprob,
                top_logprobs,
                sampler_probe_index: token.sampler_probe_index,
            });
            if let StopDecision::Stop(reason) = observation.decision {
                terminal_reason = Some(reason);
                break;
            }
        }
        Ok(ExternalBundlePreview {
            tokens: prepared.into_boxed_slice(),
            terminal_reason,
        })
    }

    /// Atomically commits sampler, stop, usage, logprobs, and bounded output events.
    pub fn commit_external_bundle_try(
        &mut self,
        transaction: NativeSamplerTxn,
        preview: ExternalBundlePreview,
    ) -> Result<ExternalBundleOutcome, GenError> {
        let final_token = preview
            .tokens
            .last()
            .ok_or(GenError::SpeculationInvalid("external bundle is empty"))?;
        let committed_sampler =
            transaction.finalize(final_token.sampler_probe_index, final_token.token_id)?;
        let checkpoint = self.state_checkpoint();
        let expected_terminal = preview.terminal_reason.clone();
        let commit_result = (|| {
            let mut events = Vec::new();
            let mut outcome = StepOutcome::Continue {
                token_id: final_token.token_id,
            };
            for token in preview.tokens.iter() {
                let (mut emit_bytes, decision) = self.commit_sample(
                    token.token_id,
                    token.token_bytes.clone(),
                    token.selected_logprob,
                    token.top_logprobs.clone(),
                )?;
                while emit_bytes > 0 {
                    events.push(self.take_safe_event(&mut emit_bytes)?);
                }
                if let StopDecision::Stop(reason) = decision {
                    self.pending_deltas.clear();
                    events.push(self.finished_event(reason.clone()));
                    outcome = StepOutcome::Finished(reason);
                    break;
                }
            }
            let actual_terminal = match &outcome {
                StepOutcome::Continue { .. } => None,
                StepOutcome::Finished(reason) => Some(reason),
            };
            if actual_terminal != expected_terminal.as_ref() {
                return Err(GenError::SpeculationInvalid(
                    "external bundle preview diverged during commit",
                ));
            }
            self.sender.try_send_batch(events)?;
            Ok(outcome)
        })();
        let outcome = match commit_result {
            Ok(outcome) => outcome,
            Err(error) => {
                self.restore_state(checkpoint);
                return Err(error);
            }
        };
        self.backend.sampler = committed_sampler;
        Ok(ExternalBundleOutcome {
            outcome,
            output_tokens: preview.tokens.len(),
            kv_tokens: preview.tokens.len().saturating_sub(1),
            pending_token: final_token.token_id,
        })
    }

    /// Consumes one output row after the scheduler's shared native decode call.
    pub async fn after_native_decode(
        &mut self,
        model: &crate::ffi::Model,
        context: &mut crate::ffi::Context,
        step: GenerationStep,
    ) -> Result<StepOutcome, GenError> {
        let result = self.after_native_decode_inner(model, context, step).await;
        if let Err(error) = &result {
            let _ = self
                .sender
                .send(GenerationEvent::Failed(error.clone()))
                .await;
        }
        result
    }

    async fn after_native_decode_inner(
        &mut self,
        model: &crate::ffi::Model,
        context: &mut crate::ffi::Context,
        step: GenerationStep,
    ) -> Result<StepOutcome, GenError> {
        if let Some(reason) = control_stop_reason(self.control.state()) {
            self.finish(reason.clone()).await?;
            return Ok(StepOutcome::Finished(reason));
        }
        let sampled = self
            .backend
            .sampler
            .transform_sample_accept(context, step.output_index)
            .map_err(|error| GenError::Backend(error.to_string()))?;
        let token_bytes = model
            .token_piece(sampled.token, false)
            .map_err(|error| GenError::Backend(error.to_string()))?;
        if let Some(reason) = control_stop_reason(self.control.state()) {
            self.pending_deltas.clear();
            self.finish(reason.clone()).await?;
            return Ok(StepOutcome::Finished(reason));
        }
        let (selected_logprob, top_logprobs) =
            normalize_native_distribution(&sampled, self.plan.top_logprobs, |candidate| {
                model
                    .token_piece(candidate, false)
                    .map_err(|error| GenError::Backend(error.to_string()))
            })?;
        self.after_sample(sampled.token, token_bytes, selected_logprob, top_logprobs)
            .await
    }

    /// Consumes one native output row without blocking the shared model actor.
    ///
    /// A full or disconnected stream returns a typed request-local error. The
    /// scheduler must retire only this sequence and continue serving siblings.
    pub fn after_native_decode_try(
        &mut self,
        model: &crate::ffi::Model,
        context: &mut crate::ffi::Context,
        step: GenerationStep,
    ) -> Result<StepOutcome, GenError> {
        let result = self.after_native_decode_try_inner(model, context, step);
        if let Err(error) = &result {
            let _ = self.sender.try_send(GenerationEvent::Failed(error.clone()));
        }
        result
    }

    fn after_native_decode_try_inner(
        &mut self,
        model: &crate::ffi::Model,
        context: &mut crate::ffi::Context,
        step: GenerationStep,
    ) -> Result<StepOutcome, GenError> {
        if let Some(reason) = control_stop_reason(self.control.state()) {
            self.finish_try(reason.clone())?;
            return Ok(StepOutcome::Finished(reason));
        }
        let sampled = self
            .backend
            .sampler
            .transform_sample_accept(context, step.output_index)
            .map_err(|error| GenError::Backend(error.to_string()))?;
        let token_bytes = model
            .token_piece(sampled.token, false)
            .map_err(|error| GenError::Backend(error.to_string()))?;
        if let Some(reason) = control_stop_reason(self.control.state()) {
            self.pending_deltas.clear();
            self.finish_try(reason.clone())?;
            return Ok(StepOutcome::Finished(reason));
        }
        let (selected_logprob, top_logprobs) =
            normalize_native_distribution(&sampled, self.plan.top_logprobs, |candidate| {
                model
                    .token_piece(candidate, false)
                    .map_err(|error| GenError::Backend(error.to_string()))
            })?;
        self.after_sample_try(sampled.token, token_bytes, selected_logprob, top_logprobs)
    }

    /// Commits a target-selected speculative replacement into the native sampler.
    pub fn accept_external(
        &mut self,
        model: &crate::ffi::Model,
        token: i32,
    ) -> Result<(), GenError> {
        if token < 0
            || usize::try_from(token)
                .ok()
                .is_none_or(|token| token >= model.vocab_size())
        {
            return Err(GenError::InvalidLogits(
                "accepted token is outside the loaded vocabulary",
            ));
        }
        self.backend
            .sampler
            .accept(token)
            .map_err(|error| GenError::Backend(error.to_string()))
    }
}

fn normalize_distribution(
    sampled: &SamplingResult,
    top_logprobs: usize,
    mut token_piece: impl FnMut(i32) -> Result<Vec<u8>, GenError>,
) -> Result<(Option<f32>, Vec<TokenLogprob>), GenError> {
    validate_distribution(sampled.token_id, sampled.probability, &sampled.candidates)?;
    let selected = sampled.probability.ln();
    let mut normalized = sampled
        .candidates
        .iter()
        .map(|candidate| TokenLogprob {
            token_id: candidate.token_id,
            bytes: Vec::new(),
            logprob: if candidate.probability == 0.0 {
                f32::NEG_INFINITY
            } else {
                candidate.probability.ln()
            },
        })
        .collect::<Vec<_>>();
    normalized.sort_by(|left, right| right.logprob.total_cmp(&left.logprob));
    let mut top = super::logprobs::into_bounded_top(normalized, top_logprobs);
    for entry in &mut top {
        if entry.bytes.is_empty() {
            entry.bytes = token_piece(entry.token_id)?;
        }
    }
    Ok((Some(selected), top))
}

#[cfg(any(feature = "cpu", feature = "cuda"))]
fn normalize_native_distribution(
    sampled: &crate::ffi::SampledToken,
    top_logprobs: usize,
    token_piece: impl FnMut(i32) -> Result<Vec<u8>, GenError>,
) -> Result<(Option<f32>, Vec<TokenLogprob>), GenError> {
    normalize_distribution(
        &SamplingResult {
            token_id: sampled.token,
            probability: sampled.probability,
            candidates: sampled
                .candidates
                .iter()
                .map(|candidate| DistributionCandidate {
                    token_id: candidate.token,
                    logit: candidate.logit,
                    probability: candidate.probability,
                })
                .collect(),
        },
        top_logprobs,
        token_piece,
    )
}

fn validate_distribution(
    selected_token: i32,
    selected_probability: f32,
    candidates: &[DistributionCandidate],
) -> Result<(), GenError> {
    if selected_token < 0
        || candidates.is_empty()
        || !selected_probability.is_finite()
        || selected_probability <= 0.0
        || selected_probability > 1.0
        || candidates.iter().any(|candidate| {
            candidate.token_id < 0
                || !candidate.logit.is_finite() && candidate.logit != f32::NEG_INFINITY
                || !candidate.probability.is_finite()
                || !(0.0..=1.0).contains(&candidate.probability)
        })
    {
        return Err(GenError::InvalidLogits(
            "post-transform sampler distribution is invalid",
        ));
    }
    let selected = candidates
        .iter()
        .find(|candidate| candidate.token_id == selected_token)
        .ok_or(GenError::InvalidLogits(
            "sampled token is absent from the post-transform distribution",
        ))?;
    if (selected.probability - selected_probability).abs() > 1.0e-6 {
        return Err(GenError::InvalidLogits(
            "selected probability disagrees with the sampler distribution",
        ));
    }
    let sum = candidates
        .iter()
        .map(|candidate| f64::from(candidate.probability))
        .sum::<f64>();
    if (sum - 1.0).abs() > 1.0e-4 {
        return Err(GenError::InvalidLogits(
            "post-transform probabilities are not normalized",
        ));
    }
    Ok(())
}

fn control_stop_reason(state: GenerationControlState) -> Option<StopReason> {
    match state {
        GenerationControlState::Running => None,
        GenerationControlState::Cancelled => Some(StopReason::Cancelled),
        GenerationControlState::Disconnected => Some(StopReason::Disconnected),
        GenerationControlState::DeadlineExceeded => Some(StopReason::DeadlineExceeded),
    }
}
