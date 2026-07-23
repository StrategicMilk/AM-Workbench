//! Bounded speculative proposals, verification decisions, and native KV forks.

use std::{collections::BTreeMap, mem::size_of};

#[cfg(any(feature = "cpu", feature = "cuda"))]
use std::path::Path;

use crate::store::registry::ModelRecord;

use super::GenError;

/// Maximum proposed tokens in one speculative step.
pub const MAX_DRAFT_TOKENS: usize = 16;
/// Maximum history copied into one draft-actor job.
pub const MAX_SPECULATIVE_HISTORY: usize = 1_048_576;
/// Maximum number of entries in a complete post-transform distribution.
pub const MAX_PROBABILITY_CANDIDATES: usize = 262_144;
/// Maximum retained bytes in one complete probability row.
pub const MAX_PROBABILITY_ROW_BYTES: usize =
    MAX_PROBABILITY_CANDIDATES * size_of::<TokenProbability>();
/// Maximum proposal-distribution bytes retained by one request.
pub const MAX_SPECULATION_REQUEST_RETAINED_BYTES: usize = 64 * 1024 * 1024;
/// Maximum draft jobs/results retained by one paired-model actor.
pub const MAX_DRAFT_ACTOR_IN_FLIGHT: usize = 3;
/// Maximum job/result bytes retained across one paired-model actor.
pub const MAX_SPECULATION_WORKER_RETAINED_BYTES: usize = 256 * 1024 * 1024;
/// Maximum opaque native KV snapshot retained by one speculative transaction.
pub const MAX_SPECULATIVE_KV_STATE_BYTES: usize = 64 * 1024 * 1024;
/// Allowed absolute error for a normalized post-transform probability row.
pub const PROBABILITY_NORMALIZATION_TOLERANCE: f64 = 1.0e-4;

/// Request facts that determine whether speculative work can improve this step.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct SpeculationEligibility {
    /// Whether at least one safe proposal token may be generated.
    pub eligible: bool,
    /// Maximum proposal depth after all request and worker bounds.
    pub maximum_budget: usize,
    /// Stable skip reason when `eligible` is false.
    pub reason: Option<SpeculationIneligibleReason>,
}

/// Stable reason that speculation was skipped without failing normal decoding.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum SpeculationIneligibleReason {
    /// No prior token can seed either proposal source.
    EmptyHistory,
    /// One remaining output cannot hold both a proposal and final token.
    InsufficientRemainingTokens,
    /// Context headroom cannot hold a proposal and final token.
    ContextTooSmall,
    /// The scheduler did not grant speculative compute.
    SchedulerGrantExhausted,
    /// Complete retained distributions would exceed the request cap.
    DistributionMemoryLimit,
    /// A configured draft pair failed compatibility or load validation.
    DraftPairUnavailable,
    /// The active native sampler cannot be transactionally cloned.
    SamplerUncloneable,
}

impl SpeculationEligibility {
    /// Computes bounded eligibility using exact output, context, compute, and memory limits.
    pub fn for_request_with_limits(
        record: &ModelRecord,
        history_tokens: usize,
        remaining_outputs: usize,
        context_capacity: usize,
        scheduler_grant: usize,
        retained_probability_row_bytes: usize,
    ) -> Self {
        if history_tokens == 0 {
            return Self::ineligible(SpeculationIneligibleReason::EmptyHistory);
        }
        if remaining_outputs < 2 {
            return Self::ineligible(SpeculationIneligibleReason::InsufficientRemainingTokens);
        }
        let context_headroom = context_capacity.saturating_sub(history_tokens);
        if context_headroom < 2
            || record.draft_pair.as_ref().is_some_and(|pair| {
                pair.minimum_context
                    .is_some_and(|minimum| context_capacity < minimum as usize)
            })
        {
            return Self::ineligible(SpeculationIneligibleReason::ContextTooSmall);
        }
        if scheduler_grant == 0 {
            return Self::ineligible(SpeculationIneligibleReason::SchedulerGrantExhausted);
        }
        if retained_probability_row_bytes == 0
            || retained_probability_row_bytes > MAX_PROBABILITY_ROW_BYTES
        {
            return Self::ineligible(SpeculationIneligibleReason::DistributionMemoryLimit);
        }
        let memory_depth = MAX_SPECULATION_REQUEST_RETAINED_BYTES
            .checked_div(retained_probability_row_bytes)
            .unwrap_or(0);
        let maximum_budget = MAX_DRAFT_TOKENS
            .min(remaining_outputs - 1)
            .min(context_headroom - 1)
            .min(scheduler_grant)
            .min(memory_depth);
        if maximum_budget == 0 {
            Self::ineligible(SpeculationIneligibleReason::DistributionMemoryLimit)
        } else {
            Self {
                eligible: true,
                maximum_budget,
                reason: None,
            }
        }
    }

    const fn ineligible(reason: SpeculationIneligibleReason) -> Self {
        Self {
            eligible: false,
            maximum_budget: 0,
            reason: Some(reason),
        }
    }
}

/// Drafter selection for one request.
#[derive(Clone, Debug, Eq, PartialEq)]
pub enum DraftMode {
    /// Registered, compatibility-verified draft model.
    DraftModel(String),
    /// Prompt lookup used only when the target has no configured draft pair.
    PromptLookup,
}

/// Loaded draft identity used to fail closed before spawning a paired actor.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct DraftModelCompatibility {
    /// Canonical registry model identifier.
    pub model_id: String,
    /// Native vocabulary-semantic SHA-256 fingerprint.
    pub vocabulary_fingerprint: String,
    /// Actual draft context capacity.
    pub context_capacity: usize,
}

/// Resolves prompt lookup or a compatibility-proven configured draft pair.
pub fn resolve_draft_mode(
    record: &ModelRecord,
    target_vocabulary_fingerprint: &str,
    target_context_capacity: usize,
    loaded_draft: Option<&DraftModelCompatibility>,
) -> Result<DraftMode, GenError> {
    let Some(pair) = &record.draft_pair else {
        return Ok(DraftMode::PromptLookup);
    };
    let declared_fingerprint =
        pair.vocabulary_fingerprint
            .as_deref()
            .ok_or(GenError::SpeculationInvalid(
                "configured draft pair lacks a vocabulary-semantic fingerprint",
            ))?;
    let draft = loaded_draft.ok_or(GenError::SpeculationInvalid(
        "configured draft pair could not be loaded",
    ))?;
    let valid_fingerprint = |fingerprint: &str| {
        fingerprint.len() == 64
            && fingerprint
                .bytes()
                .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
    };
    let minimum_context = pair.minimum_context.unwrap_or(1) as usize;
    if pair.draft_model_id != draft.model_id
        || !valid_fingerprint(declared_fingerprint)
        || declared_fingerprint != target_vocabulary_fingerprint
        || declared_fingerprint != draft.vocabulary_fingerprint
        || target_context_capacity < minimum_context
        || draft.context_capacity < minimum_context
    {
        return Err(GenError::SpeculationInvalid(
            "configured draft pair is unavailable or vocabulary/context incompatible",
        ));
    }
    Ok(DraftMode::DraftModel(draft.model_id.clone()))
}

/// Exact accepted/proposed token accounting.
#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub struct SpeculationCounters {
    /// Tokens proposed by the drafter.
    pub proposed: u64,
    /// Tokens accepted by target verification.
    pub accepted: u64,
    /// Successfully committed speculative steps.
    pub steps: u64,
}

impl SpeculationCounters {
    /// Returns the aggregate acceptance ratio, or zero before any proposal.
    pub fn acceptance_rate(self) -> f64 {
        if self.proposed == 0 {
            0.0
        } else {
            self.accepted as f64 / self.proposed as f64
        }
    }
}

/// One token and probability from a complete post-transform distribution.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct TokenProbability {
    /// Native vocabulary token identifier.
    pub token_id: i32,
    /// Normalized probability in `[0, 1]`.
    pub probability: f32,
}

/// Sorted, unique, bounded, normalized post-transform distribution.
#[derive(Clone, Debug, PartialEq)]
pub struct ProbabilityRow {
    candidates: Box<[TokenProbability]>,
}

impl ProbabilityRow {
    /// Validates and canonicalizes one complete vocabulary distribution.
    pub fn new(
        vocabulary_size: usize,
        mut candidates: Vec<TokenProbability>,
    ) -> Result<Self, GenError> {
        if vocabulary_size == 0
            || vocabulary_size > MAX_PROBABILITY_CANDIDATES
            || candidates.is_empty()
            || candidates.len() > vocabulary_size
            || candidates.len() > MAX_PROBABILITY_CANDIDATES
        {
            return Err(GenError::SpeculationInvalid(
                "probability row shape exceeds its vocabulary or memory bound",
            ));
        }
        candidates.sort_by_key(|candidate| candidate.token_id);
        let mut previous = None;
        let mut sum = 0.0_f64;
        for candidate in &candidates {
            let token = usize::try_from(candidate.token_id).ok();
            if token.is_none_or(|token| token >= vocabulary_size)
                || previous == Some(candidate.token_id)
                || !candidate.probability.is_finite()
                || !(0.0..=1.0).contains(&candidate.probability)
            {
                return Err(GenError::SpeculationInvalid(
                    "probability row has an invalid, duplicate, or out-of-vocabulary entry",
                ));
            }
            previous = Some(candidate.token_id);
            sum += f64::from(candidate.probability);
        }
        if (sum - 1.0).abs() > PROBABILITY_NORMALIZATION_TOLERANCE {
            return Err(GenError::SpeculationInvalid(
                "probability row is not normalized",
            ));
        }
        Ok(Self {
            candidates: candidates.into_boxed_slice(),
        })
    }

    /// Returns candidates in canonical token-id order.
    pub fn candidates(&self) -> &[TokenProbability] {
        &self.candidates
    }

    /// Returns a token probability, treating absent sparse entries as zero.
    pub fn probability(&self, token_id: i32) -> f32 {
        self.candidates
            .binary_search_by_key(&token_id, |candidate| candidate.token_id)
            .ok()
            .map_or(0.0, |index| self.candidates[index].probability)
    }

    /// Returns the exact retained candidate-array byte count.
    pub fn retained_bytes(&self) -> usize {
        self.candidates
            .len()
            .saturating_mul(size_of::<TokenProbability>())
    }

    fn sample(&self, unit: f64) -> Result<i32, GenError> {
        if !unit.is_finite() || !(0.0..1.0).contains(&unit) {
            return Err(GenError::SpeculationInvalid(
                "probability sample must be in the half-open unit interval",
            ));
        }
        let mut cumulative = 0.0_f64;
        let mut final_positive = None;
        for candidate in &self.candidates {
            if candidate.probability > 0.0 {
                final_positive = Some(candidate.token_id);
            }
            cumulative += f64::from(candidate.probability);
            if unit < cumulative {
                return Ok(candidate.token_id);
            }
        }
        final_positive.ok_or(GenError::SpeculationInvalid(
            "probability row contains no positive mass",
        ))
    }

    fn residual(&self, draft: &Self) -> Result<Self, GenError> {
        let mut differences = BTreeMap::<i32, f64>::new();
        for candidate in &self.candidates {
            differences.insert(candidate.token_id, f64::from(candidate.probability));
        }
        for candidate in &draft.candidates {
            *differences.entry(candidate.token_id).or_default() -= f64::from(candidate.probability);
        }
        let total = differences
            .values_mut()
            .map(|probability| {
                *probability = probability.max(0.0);
                *probability
            })
            .sum::<f64>();
        if !total.is_finite() || total <= f64::EPSILON {
            return Err(GenError::SpeculationInvalid(
                "rejected proposal produced no positive residual probability mass",
            ));
        }
        let candidates = differences
            .into_iter()
            .filter(|(_, probability)| *probability > 0.0)
            .map(|(token_id, probability)| TokenProbability {
                token_id,
                probability: (probability / total) as f32,
            })
            .collect();
        let vocabulary_size = self
            .candidates
            .last()
            .into_iter()
            .chain(draft.candidates.last())
            .map(|candidate| candidate.token_id)
            .max()
            .and_then(|token| usize::try_from(token).ok())
            .and_then(|token| token.checked_add(1))
            .ok_or(GenError::SpeculationInvalid(
                "residual vocabulary is invalid",
            ))?;
        Self::new(vocabulary_size, candidates)
    }
}

/// One proposed token with the complete draft distribution that selected it.
#[derive(Clone, Debug, PartialEq)]
pub struct ProposedToken {
    /// Selected native token identifier.
    pub token_id: i32,
    /// Complete draft distribution before this selection.
    pub draft: ProbabilityRow,
}

/// Proposal produced by a draft actor or prompt lookup.
#[derive(Clone, Debug, PartialEq)]
pub struct DraftProposal {
    /// Source mode, which determines verification semantics.
    pub mode: DraftMode,
    /// Bounded proposed tokens and their complete draft rows.
    pub tokens: Box<[ProposedToken]>,
}

impl DraftProposal {
    /// Validates the proposal source, depth, token selections, and retained bytes.
    pub fn new(mode: DraftMode, tokens: Vec<ProposedToken>) -> Result<Self, GenError> {
        if tokens.len() > MAX_DRAFT_TOKENS {
            return Err(GenError::SpeculationInvalid(
                "draft proposal exceeds the depth bound",
            ));
        }
        let retained = tokens.iter().try_fold(0_usize, |total, proposed| {
            if proposed.token_id < 0 || proposed.draft.probability(proposed.token_id) <= 0.0 {
                None
            } else {
                total.checked_add(proposed.draft.retained_bytes())
            }
        });
        if retained.is_none_or(|retained| retained > MAX_SPECULATION_REQUEST_RETAINED_BYTES) {
            return Err(GenError::SpeculationInvalid(
                "draft proposal selection or retained distribution bytes are invalid",
            ));
        }
        if matches!(&mode, DraftMode::DraftModel(model_id) if model_id.trim().is_empty() || model_id.len() > 512)
        {
            return Err(GenError::SpeculationInvalid(
                "draft model id is empty or exceeds its bound",
            ));
        }
        Ok(Self {
            mode,
            tokens: tokens.into_boxed_slice(),
        })
    }

    /// Returns the native token identifiers in proposal order.
    pub fn token_ids(&self) -> Vec<i32> {
        self.tokens.iter().map(|token| token.token_id).collect()
    }

    /// Returns the exact retained distribution bytes for actor accounting.
    pub fn retained_bytes(&self) -> usize {
        self.tokens
            .iter()
            .map(|token| token.draft.retained_bytes())
            .sum()
    }
}

/// Versioned bounded input sent to a dedicated draft-model actor.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct DraftJob {
    /// Scheduler sequence identity.
    pub sequence_id: u32,
    /// Request-local generation version; increments after every target commit/cancel.
    pub version: u64,
    /// Bounded target history ending in the current pending token.
    pub history: Box<[i32]>,
    /// Exact maximum proposal depth granted by the target scheduler.
    pub budget: usize,
    /// Shared target/draft vocabulary size after fingerprint validation.
    pub vocabulary_size: usize,
}

impl DraftJob {
    /// Constructs a bounded draft job suitable for a non-`Send` actor owner.
    pub fn new(
        sequence_id: u32,
        version: u64,
        history: &[i32],
        budget: usize,
        vocabulary_size: usize,
    ) -> Result<Self, GenError> {
        if history.is_empty()
            || history.len() > MAX_SPECULATIVE_HISTORY
            || history.iter().any(|token| {
                usize::try_from(*token)
                    .ok()
                    .is_none_or(|token| token >= vocabulary_size)
            })
            || budget == 0
            || budget > MAX_DRAFT_TOKENS
            || vocabulary_size == 0
            || vocabulary_size > MAX_PROBABILITY_CANDIDATES
        {
            return Err(GenError::SpeculationInvalid(
                "draft job history, budget, or vocabulary is invalid",
            ));
        }
        Ok(Self {
            sequence_id,
            version,
            history: history.to_vec().into_boxed_slice(),
            budget,
            vocabulary_size,
        })
    }

    /// Rejects stale/cross-sequence actor results deterministically.
    pub fn validate_result(&self, result: &DraftResult) -> Result<(), GenError> {
        if result.sequence_id != self.sequence_id || result.version != self.version {
            return Err(GenError::SpeculationInvalid(
                "draft result is stale or belongs to another sequence",
            ));
        }
        if result.proposal.tokens.len() > self.budget {
            return Err(GenError::SpeculationInvalid(
                "draft result exceeds its scheduler grant",
            ));
        }
        Ok(())
    }
}

/// Versioned bounded output returned from a dedicated draft-model actor.
#[derive(Clone, Debug, PartialEq)]
pub struct DraftResult {
    /// Scheduler sequence identity copied from the job.
    pub sequence_id: u32,
    /// Request-local generation version copied from the job.
    pub version: u64,
    /// Validated bounded proposal.
    pub proposal: DraftProposal,
}

impl DraftResult {
    /// Constructs a result and preserves the job identity/version contract.
    pub fn new(job: &DraftJob, proposal: DraftProposal) -> Result<Self, GenError> {
        let result = Self {
            sequence_id: job.sequence_id,
            version: job.version,
            proposal,
        };
        job.validate_result(&result)?;
        Ok(result)
    }

    /// Returns total retained bytes for bounded actor-queue accounting.
    pub fn retained_bytes(&self) -> usize {
        size_of::<Self>().saturating_add(self.proposal.retained_bytes())
    }

    /// Reconciles an optimistic result after the target commits an all-accepted bonus token.
    ///
    /// An optimistic job starts after the proposal tokens but before the target bonus is known.
    /// Its first proposed token is therefore useful only when it exactly matches that committed
    /// bonus. In that case the remaining rows are already conditioned on the real target history
    /// and can be consumed by the next target step without repeating draft work.
    ///
    /// # Arguments
    ///
    /// * `committed_bonus` - Target-selected all-accepted bonus token left pending in target KV.
    ///
    /// # Returns
    ///
    /// A version-preserving proposal tail when the optimistic prefix reconciles, or `None` when
    /// the prediction did not match or contained no useful tail.
    pub fn reconcile_optimistic_bonus(
        &self,
        committed_bonus: i32,
    ) -> Result<Option<Self>, GenError> {
        let Some(first) = self.proposal.tokens.first() else {
            return Ok(None);
        };
        if first.token_id != committed_bonus || self.proposal.tokens.len() == 1 {
            return Ok(None);
        }
        let proposal = DraftProposal::new(
            self.proposal.mode.clone(),
            self.proposal.tokens[1..].to_vec(),
        )?;
        Ok(Some(Self {
            sequence_id: self.sequence_id,
            version: self.version,
            proposal,
        }))
    }
}

/// Backend-neutral draft actor seam.
pub trait ProposalSource {
    /// Processes one bounded versioned job without borrowing target-native state.
    fn propose(&mut self, job: &DraftJob) -> Result<DraftResult, GenError>;
}

/// Draft-model backend owned exclusively by its actor thread.
pub trait DraftModelBackend {
    /// Runs bounded draft decoding and returns complete per-token distributions.
    fn propose_tokens(&mut self, job: &DraftJob) -> Result<Vec<ProposedToken>, GenError>;
}

/// Proposal source backed by a compatibility-verified registered draft model.
#[derive(Debug)]
pub struct DraftModelProposer<B> {
    model_id: String,
    backend: B,
}

impl<B> DraftModelProposer<B> {
    /// Binds a non-empty registry model id to its isolated actor backend.
    pub fn new(model_id: String, backend: B) -> Result<Self, GenError> {
        if model_id.trim().is_empty() || model_id.len() > 512 {
            return Err(GenError::SpeculationInvalid(
                "draft model id is empty or exceeds its bound",
            ));
        }
        Ok(Self { model_id, backend })
    }

    /// Exposes the backend for actor diagnostics and lifecycle management.
    pub fn backend(&self) -> &B {
        &self.backend
    }

    /// Exposes mutable backend ownership for actor configure/remove commands.
    pub fn backend_mut(&mut self) -> &mut B {
        &mut self.backend
    }
}

impl<B: DraftModelBackend> ProposalSource for DraftModelProposer<B> {
    fn propose(&mut self, job: &DraftJob) -> Result<DraftResult, GenError> {
        let tokens = self.backend.propose_tokens(job)?;
        let proposal = DraftProposal::new(DraftMode::DraftModel(self.model_id.clone()), tokens)?;
        DraftResult::new(job, proposal)
    }
}

/// Bounded prompt-lookup proposer used only when no draft pair is configured.
#[derive(Clone, Debug)]
pub struct PromptLookupProposer {
    minimum_match: usize,
    maximum_match: usize,
}

impl PromptLookupProposer {
    /// Constructs a bounded suffix-match proposer.
    pub fn new(minimum_match: usize, maximum_match: usize) -> Result<Self, GenError> {
        if minimum_match == 0 || maximum_match < minimum_match || maximum_match > 128 {
            return Err(GenError::SpeculationInvalid(
                "prompt lookup match bounds are invalid",
            ));
        }
        Ok(Self {
            minimum_match,
            maximum_match,
        })
    }
}

impl Default for PromptLookupProposer {
    fn default() -> Self {
        Self {
            minimum_match: 2,
            maximum_match: 32,
        }
    }
}

impl ProposalSource for PromptLookupProposer {
    fn propose(&mut self, job: &DraftJob) -> Result<DraftResult, GenError> {
        let start = job.history.len().saturating_sub(MAX_SPECULATIVE_HISTORY);
        let bounded = &job.history[start..];
        let maximum_match = self.maximum_match.min(bounded.len().saturating_sub(1));
        let mut best: Option<(usize, usize)> = None;
        for match_length in self.minimum_match..=maximum_match {
            let suffix_start = bounded.len() - match_length;
            let suffix = &bounded[suffix_start..];
            for candidate_start in (0..suffix_start).rev() {
                let candidate_end = candidate_start + match_length;
                if candidate_end <= suffix_start
                    && candidate_end < bounded.len()
                    && bounded[candidate_start..candidate_end] == *suffix
                {
                    best = Some((candidate_end, match_length));
                    break;
                }
            }
        }
        let tokens = if let Some((proposal_start, _)) = best {
            bounded[proposal_start..]
                .iter()
                .take(job.budget)
                .map(|token_id| {
                    ProbabilityRow::new(
                        job.vocabulary_size,
                        vec![TokenProbability {
                            token_id: *token_id,
                            probability: 1.0,
                        }],
                    )
                    .map(|draft| ProposedToken {
                        token_id: *token_id,
                        draft,
                    })
                })
                .collect::<Result<Vec<_>, _>>()?
        } else {
            Vec::new()
        };
        let proposal = DraftProposal::new(DraftMode::PromptLookup, tokens)?;
        DraftResult::new(job, proposal)
    }
}

/// Draft-model backend whose model, context, batch, and samplers stay on one actor thread.
#[cfg(any(feature = "cpu", feature = "cuda"))]
pub struct NativeDraftBackend {
    model: crate::ffi::Model,
    context: crate::ffi::Context,
    batch: crate::ffi::Batch,
    sequences: BTreeMap<u32, NativeDraftSequence>,
}

#[cfg(any(feature = "cpu", feature = "cuda"))]
struct NativeDraftSequence {
    sampler_template: crate::ffi::Sampler,
    sampler: crate::ffi::Sampler,
    committed_history: Vec<i32>,
    latest_version: u64,
}

#[cfg(any(feature = "cpu", feature = "cuda"))]
impl NativeDraftBackend {
    /// Loads an isolated draft model and context for ownership by a dedicated actor thread.
    pub fn load(
        model_path: &Path,
        context_options: crate::ffi::ContextOptions,
    ) -> Result<Self, GenError> {
        let model = crate::ffi::Model::load(model_path)
            .map_err(|error| GenError::Backend(error.to_string()))?;
        Self::from_model(model, context_options)
    }

    /// Loads a draft from the exact file already inspected and fingerprinted.
    pub(crate) fn load_verified(
        source: crate::store::loader::VerifiedModelFile,
        context_options: crate::ffi::ContextOptions,
    ) -> Result<Self, GenError> {
        let model = crate::ffi::Model::load_verified(source)
            .map_err(|error| GenError::Backend(error.to_string()))?;
        Self::from_model(model, context_options)
    }

    fn from_model(
        model: crate::ffi::Model,
        context_options: crate::ffi::ContextOptions,
    ) -> Result<Self, GenError> {
        let context = model
            .context_with(context_options)
            .map_err(|error| GenError::Backend(error.to_string()))?;
        let metadata = context.metadata();
        if metadata.max_sequences == 0
            || metadata.batch_tokens == 0
            || metadata.vocabulary_size == 0
            || metadata.vocabulary_size > MAX_PROBABILITY_CANDIDATES
        {
            return Err(GenError::SpeculationInvalid(
                "draft context dimensions cannot support bounded proposal work",
            ));
        }
        let batch_capacity = i32::try_from(metadata.batch_tokens)
            .map_err(|_| GenError::SpeculationInvalid("draft batch exceeds native width"))?;
        let batch = crate::ffi::Batch::tokens(batch_capacity, 1)
            .map_err(|error| GenError::Backend(error.to_string()))?;
        Ok(Self {
            model,
            context,
            batch,
            sequences: BTreeMap::new(),
        })
    }

    /// Configures exact request-local sampler and grammar state before submitting jobs.
    pub fn configure_sequence(
        &mut self,
        sequence_id: u32,
        params: &super::SamplerParams,
        grammar: Option<&super::CompiledGrammar>,
    ) -> Result<(), GenError> {
        self.validate_sequence_id(sequence_id)?;
        let sampler_template = super::SamplerChain::build_native_with_grammar(
            params,
            super::SamplerCapabilities::pinned_revision(),
            &self.model,
            grammar,
        )?;
        let sampler = sampler_template
            .try_clone()
            .map_err(|_| GenError::SpeculationInvalid("draft sampler cannot be cloned"))?;
        self.clear_sequence(sequence_id)?;
        self.sequences.insert(
            sequence_id,
            NativeDraftSequence {
                sampler_template,
                sampler,
                committed_history: Vec::new(),
                latest_version: 0,
            },
        );
        Ok(())
    }

    /// Removes one request's sampler and KV state after cancellation or completion.
    pub fn remove_sequence(&mut self, sequence_id: u32) -> Result<(), GenError> {
        self.validate_sequence_id(sequence_id)?;
        self.clear_sequence(sequence_id)?;
        self.sequences.remove(&sequence_id);
        Ok(())
    }

    /// Returns the draft vocabulary size used by job construction and compatibility checks.
    pub fn vocabulary_size(&self) -> usize {
        self.model.vocab_size()
    }

    /// Returns the actual isolated draft context capacity selected by llama.cpp.
    pub fn context_capacity(&self) -> usize {
        self.context.metadata().context_tokens as usize
    }

    /// Returns the loaded native draft model for fingerprint and lifecycle diagnostics.
    pub fn model(&self) -> &crate::ffi::Model {
        &self.model
    }

    /// Returns the highest committed draft KV position for lifecycle diagnostics.
    pub fn sequence_position(&self, sequence_id: u32) -> Result<i32, GenError> {
        let sequence_id = self.validate_sequence_id(sequence_id)?;
        self.context
            .memory_seq_pos_max(sequence_id)
            .map_err(|error| GenError::Backend(error.to_string()))
    }

    fn validate_sequence_id(&self, sequence_id: u32) -> Result<i32, GenError> {
        if sequence_id >= self.context.metadata().max_sequences {
            return Err(GenError::SpeculationInvalid(
                "draft sequence id exceeds the isolated context",
            ));
        }
        i32::try_from(sequence_id)
            .map_err(|_| GenError::SpeculationInvalid("draft sequence id exceeds native width"))
    }

    fn clear_sequence(&mut self, sequence_id: u32) -> Result<(), GenError> {
        let sequence_id = self.validate_sequence_id(sequence_id)?;
        let removed = self
            .context
            .memory_seq_rm(sequence_id, -1, -1)
            .map_err(|error| GenError::Backend(error.to_string()))?;
        if removed {
            Ok(())
        } else {
            Err(GenError::Backend(
                "draft KV cleanup did not remove the complete sequence".to_owned(),
            ))
        }
    }

    fn decode_tokens(
        &mut self,
        sequence_id: i32,
        start_position: usize,
        tokens: &[i32],
        request_final_output: bool,
    ) -> Result<(), GenError> {
        if tokens.is_empty() {
            return Ok(());
        }
        let chunk_size = usize::try_from(self.context.metadata().batch_tokens)
            .ok()
            .filter(|size| *size > 0)
            .ok_or(GenError::SpeculationInvalid(
                "draft context has no batch capacity",
            ))?;
        for (chunk_index, chunk) in tokens.chunks(chunk_size).enumerate() {
            self.batch.clear();
            let chunk_offset = chunk_index.saturating_mul(chunk_size);
            for (offset, token) in chunk.iter().enumerate() {
                let position = start_position
                    .checked_add(chunk_offset)
                    .and_then(|position| position.checked_add(offset))
                    .and_then(|position| i32::try_from(position).ok())
                    .ok_or(GenError::SpeculationInvalid(
                        "draft decode position exceeds native width",
                    ))?;
                let is_final = chunk_offset + offset + 1 == tokens.len();
                self.batch
                    .add_token(
                        *token,
                        position,
                        &[sequence_id],
                        request_final_output && is_final,
                    )
                    .map_err(|error| GenError::Backend(error.to_string()))?;
            }
            self.context
                .decode(&mut self.batch)
                .map_err(|error| GenError::Backend(error.to_string()))?;
        }
        Ok(())
    }

    fn sync_committed_history(
        &mut self,
        sequence_id: u32,
        state: &mut NativeDraftSequence,
        desired: &[i32],
    ) -> Result<(), GenError> {
        let native_sequence = self.validate_sequence_id(sequence_id)?;
        if !desired.starts_with(&state.committed_history) {
            self.clear_sequence(sequence_id)?;
            state.sampler = state
                .sampler_template
                .try_clone()
                .map_err(|_| GenError::SpeculationInvalid("draft sampler cannot be cloned"))?;
            state.committed_history.clear();
        }
        let start = state.committed_history.len();
        let extension = &desired[start..];
        self.decode_tokens(native_sequence, start, extension, false)?;
        for token in extension {
            state
                .sampler
                .accept(*token)
                .map_err(|error| GenError::Backend(error.to_string()))?;
        }
        state.committed_history.extend_from_slice(extension);
        Ok(())
    }

    fn restore_committed(
        &mut self,
        sequence_id: u32,
        state: Option<&[u8]>,
    ) -> Result<(), GenError> {
        self.clear_sequence(sequence_id)
            .map_err(|error| GenError::SpeculationContextInvalidated(error.to_string()))?;
        if let Some(state) = state {
            let native_sequence = self.validate_sequence_id(sequence_id)?;
            self.context
                .restore_sequence_state(native_sequence, state)
                .map_err(|error| GenError::SpeculationContextInvalidated(error.to_string()))?;
            let restored = self
                .context
                .sequence_state(native_sequence)
                .map_err(|error| GenError::SpeculationContextInvalidated(error.to_string()))?;
            if restored != state {
                return Err(GenError::SpeculationContextInvalidated(
                    "draft KV rollback was not byte-exact".to_owned(),
                ));
            }
        }
        Ok(())
    }
}

#[cfg(any(feature = "cpu", feature = "cuda"))]
impl DraftModelBackend for NativeDraftBackend {
    fn propose_tokens(&mut self, job: &DraftJob) -> Result<Vec<ProposedToken>, GenError> {
        if job.vocabulary_size != self.model.vocab_size() {
            return Err(GenError::SpeculationInvalid(
                "draft job vocabulary differs from the compatibility-verified model",
            ));
        }
        let mut state =
            self.sequences
                .remove(&job.sequence_id)
                .ok_or(GenError::SpeculationInvalid(
                    "draft sequence was not configured",
                ))?;
        let result = (|| {
            if job.version < state.latest_version {
                return Err(GenError::SpeculationInvalid("draft job version is stale"));
            }
            let (pending, committed) = job
                .history
                .split_last()
                .ok_or(GenError::SpeculationInvalid("draft job history is empty"))?;
            let previous_history = state.committed_history.clone();
            let previous_sampler = state
                .sampler
                .try_clone()
                .map_err(|_| GenError::SpeculationInvalid("draft sampler cannot be cloned"))?;
            let previous_snapshot = if previous_history.is_empty() {
                None
            } else {
                let native_sequence = self.validate_sequence_id(job.sequence_id)?;
                Some(
                    self.context
                        .sequence_state(native_sequence)
                        .map_err(|error| GenError::Backend(error.to_string()))?,
                )
            };
            if let Err(sync_error) =
                self.sync_committed_history(job.sequence_id, &mut state, committed)
            {
                self.restore_committed(job.sequence_id, previous_snapshot.as_deref())?;
                state.committed_history = previous_history;
                state.sampler = previous_sampler;
                return Err(sync_error);
            }
            let native_sequence = self.validate_sequence_id(job.sequence_id)?;
            let snapshot = if committed.is_empty() {
                None
            } else {
                Some(
                    self.context
                        .sequence_state(native_sequence)
                        .map_err(|error| GenError::Backend(error.to_string()))?,
                )
            };
            let proposal_result = (|| {
                let mut working_sampler = state
                    .sampler
                    .try_clone()
                    .map_err(|_| GenError::SpeculationInvalid("draft sampler cannot be cloned"))?;
                working_sampler
                    .accept(*pending)
                    .map_err(|error| GenError::Backend(error.to_string()))?;
                self.decode_tokens(native_sequence, committed.len(), &[*pending], true)?;
                let mut proposed = Vec::with_capacity(job.budget);
                for depth in 0..job.budget {
                    let sampled = working_sampler
                        .transform_sample_accept(&mut self.context, 0)
                        .map_err(|error| GenError::Backend(error.to_string()))?;
                    let row = ProbabilityRow::new(
                        job.vocabulary_size,
                        sampled
                            .candidates
                            .into_iter()
                            .map(|candidate| TokenProbability {
                                token_id: candidate.token,
                                probability: candidate.probability,
                            })
                            .collect(),
                    )?;
                    proposed.push(ProposedToken {
                        token_id: sampled.token,
                        draft: row,
                    });
                    if depth + 1 < job.budget {
                        let position = committed.len().saturating_add(depth + 1);
                        self.decode_tokens(native_sequence, position, &[sampled.token], true)?;
                    }
                }
                Ok(proposed)
            })();
            let restore_result = self.restore_committed(job.sequence_id, snapshot.as_deref());
            restore_result?;
            let proposed = proposal_result?;
            state.latest_version = job.version;
            Ok(proposed)
        })();
        self.sequences.insert(job.sequence_id, state);
        result
    }
}

/// One target sampler probe paired with its complete target distribution.
#[derive(Clone, Debug, PartialEq)]
pub struct TargetProbe {
    /// Token selected by the cloned target sampler.
    pub selected_token: i32,
    /// Complete target distribution before selection.
    pub distribution: ProbabilityRow,
}

/// Target verification rows for every proposal plus the all-accepted bonus row.
#[derive(Clone, Debug, PartialEq)]
pub struct TargetVerification {
    /// Row `i` predicts proposal token `i`; the final row predicts the bonus token.
    pub probes: Box<[TargetProbe]>,
}

impl TargetVerification {
    /// Validates target-selected tokens and the exact proposal-plus-bonus shape.
    pub fn new(proposal: &DraftProposal, probes: Vec<TargetProbe>) -> Result<Self, GenError> {
        if probes.len() != proposal.tokens.len().saturating_add(1)
            || probes.iter().any(|probe| {
                probe.selected_token < 0
                    || probe.distribution.probability(probe.selected_token) <= 0.0
            })
        {
            return Err(GenError::SpeculationInvalid(
                "target verification must contain valid proposal-plus-bonus probes",
            ));
        }
        Ok(Self {
            probes: probes.into_boxed_slice(),
        })
    }
}

/// Pure verification result; the final output token remains pending outside KV.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct SpeculationDecision {
    /// Number of proposal tokens accepted before the first rejection.
    pub accepted: usize,
    /// Total number of tokens presented for verification.
    pub proposed: usize,
    /// Accepted proposal prefix already represented in committed target KV.
    pub kv_tokens: Vec<i32>,
    /// Replacement or all-accepted bonus emitted but not yet decoded into KV.
    pub pending_token: i32,
    /// Whether target verification rejected a proposed token.
    pub rejected: bool,
}

/// Request-local deterministic acceptance state.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct SpeculationPlan {
    /// Active, already-resolved proposal mode.
    pub mode: DraftMode,
    /// Adaptive proposal budget for the next committed step.
    pub draft_budget: usize,
    /// Exact counters updated only after a successful KV/sampler/bundle commit.
    pub counters: SpeculationCounters,
    seed: u64,
}

impl SpeculationPlan {
    /// Constructs a plan after draft-pair compatibility resolution.
    pub fn new(mode: DraftMode, seed: u64) -> Result<Self, GenError> {
        if matches!(&mode, DraftMode::DraftModel(model_id) if model_id.trim().is_empty()) {
            return Err(GenError::SpeculationInvalid("draft model id is empty"));
        }
        Ok(Self {
            mode,
            draft_budget: 4,
            counters: SpeculationCounters::default(),
            seed,
        })
    }

    /// Applies exact prompt or draft-model verification without mutating commit counters.
    pub fn decide(
        &self,
        proposal: &DraftProposal,
        verification: &TargetVerification,
    ) -> Result<SpeculationDecision, GenError> {
        if proposal.mode != self.mode || proposal.tokens.is_empty() {
            return Err(GenError::SpeculationInvalid(
                "proposal is empty or does not match the active plan",
            ));
        }
        if verification.probes.len() != proposal.tokens.len() + 1 {
            return Err(GenError::SpeculationInvalid(
                "target verification shape does not match the proposal",
            ));
        }
        let mut rng = DecisionRng::new(self.seed, self.counters.steps);
        let mut accepted = 0_usize;
        let mut pending = None;
        for (index, proposed) in proposal.tokens.iter().enumerate() {
            let target = &verification.probes[index];
            let accepted_here = match proposal.mode {
                DraftMode::PromptLookup => target.selected_token == proposed.token_id,
                DraftMode::DraftModel(_) => {
                    let q = proposed.draft.probability(proposed.token_id);
                    if q <= 0.0 {
                        return Err(GenError::SpeculationInvalid(
                            "draft selected a token with no probability mass",
                        ));
                    }
                    let p = target.distribution.probability(proposed.token_id);
                    rng.next_unit() < f64::from((p / q).min(1.0))
                }
            };
            if accepted_here {
                accepted += 1;
                continue;
            }
            pending = Some(match proposal.mode {
                DraftMode::PromptLookup => target.selected_token,
                DraftMode::DraftModel(_) => target
                    .distribution
                    .residual(&proposed.draft)?
                    .sample(rng.next_unit())?,
            });
            break;
        }
        let rejected = accepted < proposal.tokens.len();
        let pending_token =
            pending.unwrap_or_else(|| verification.probes[proposal.tokens.len()].selected_token);
        Ok(SpeculationDecision {
            accepted,
            proposed: proposal.tokens.len(),
            kv_tokens: proposal.tokens[..accepted]
                .iter()
                .map(|token| token.token_id)
                .collect(),
            pending_token,
            rejected,
        })
    }

    /// Records a successfully committed decision and adapts the next depth.
    pub fn record_commit(&mut self, decision: &SpeculationDecision) -> Result<(), GenError> {
        self.record_commit_prefix(decision, decision.accepted.saturating_add(1))
    }

    /// Records the exact committed prefix when stop handling truncates a verified bundle.
    ///
    /// `proposed` measures target verification work, while `accepted` counts only accepted draft
    /// tokens that actually reached the committed output bundle. This distinction prevents a
    /// stop string or EOG inside a multi-token bundle from inflating acceptance telemetry.
    ///
    /// # Arguments
    ///
    /// * `decision` - Pure target-verification result for the full proposal.
    /// * `output_tokens` - Number of bundle tokens atomically committed to output and sampler state.
    pub fn record_commit_prefix(
        &mut self,
        decision: &SpeculationDecision,
        output_tokens: usize,
    ) -> Result<(), GenError> {
        let maximum_output_tokens = decision.accepted.saturating_add(1);
        if decision.proposed == 0
            || decision.proposed > MAX_DRAFT_TOKENS
            || decision.accepted > decision.proposed
            || decision.kv_tokens.len() != decision.accepted
            || decision.pending_token < 0
            || output_tokens == 0
            || output_tokens > maximum_output_tokens
        {
            return Err(GenError::SpeculationInvalid(
                "committed speculation decision is internally inconsistent",
            ));
        }
        let committed_accepted = decision.accepted.min(output_tokens);
        self.counters.proposed = self
            .counters
            .proposed
            .saturating_add(decision.proposed as u64);
        self.counters.accepted = self
            .counters
            .accepted
            .saturating_add(committed_accepted as u64);
        self.counters.steps = self.counters.steps.saturating_add(1);
        let rate = self.counters.acceptance_rate();
        self.draft_budget = if rate >= 0.75 {
            (self.draft_budget + 1).min(MAX_DRAFT_TOKENS)
        } else if rate < 0.25 {
            self.draft_budget.saturating_sub(1).max(1)
        } else {
            self.draft_budget
        };
        Ok(())
    }
}

#[derive(Clone, Copy, Debug)]
struct DecisionRng {
    state: u64,
}

impl DecisionRng {
    fn new(seed: u64, step: u64) -> Self {
        Self {
            state: seed ^ step.wrapping_mul(0xD134_2543_DE82_EF95),
        }
    }

    fn next_unit(&mut self) -> f64 {
        self.state = self.state.wrapping_add(0x9E37_79B9_7F4A_7C15);
        let mut value = self.state;
        value = (value ^ (value >> 30)).wrapping_mul(0xBF58_476D_1CE4_E5B9);
        value = (value ^ (value >> 27)).wrapping_mul(0x94D0_49BB_1331_11EB);
        value ^= value >> 31;
        (value >> 11) as f64 * (1.0 / ((1_u64 << 53) as f64))
    }
}

/// Recoverable target KV fork descriptor with a byte-exact original snapshot.
#[cfg(any(feature = "cpu", feature = "cuda"))]
#[derive(Debug)]
pub struct TargetKvFork {
    /// Live target scheduler sequence.
    pub target_sequence_id: i32,
    /// Reserved scratch scheduler sequence.
    pub scratch_sequence_id: i32,
    /// First position after the original target prefix.
    pub position: usize,
    /// Exact target sequence bytes captured before speculative verification.
    pub original_state: Box<[u8]>,
    /// Scheduler cells reserved for this fork.
    pub reserved_cells: usize,
}

#[cfg(any(feature = "cpu", feature = "cuda"))]
impl TargetKvFork {
    /// Snapshots target state and copies its complete KV prefix into empty scratch.
    pub fn begin(
        context: &mut crate::ffi::Context,
        target_sequence_id: i32,
        scratch_sequence_id: i32,
        reserved_cells: usize,
    ) -> Result<Self, GenError> {
        let max_sequences = context.metadata().max_sequences;
        let valid = |sequence_id: i32| {
            u32::try_from(sequence_id)
                .ok()
                .is_some_and(|sequence_id| sequence_id < max_sequences)
        };
        if !valid(target_sequence_id)
            || !valid(scratch_sequence_id)
            || target_sequence_id == scratch_sequence_id
            || reserved_cells == 0
            || reserved_cells > MAX_DRAFT_TOKENS
        {
            return Err(GenError::SpeculationInvalid(
                "target KV fork sequence ids or reservation are invalid",
            ));
        }
        let maximum_position = context
            .memory_seq_pos_max(target_sequence_id)
            .map_err(|error| GenError::Backend(error.to_string()))?;
        if maximum_position < 0 {
            return Err(GenError::SpeculationInvalid(
                "target KV fork requires a non-empty sequence",
            ));
        }
        let position = usize::try_from(maximum_position)
            .ok()
            .and_then(|position| position.checked_add(1))
            .ok_or(GenError::SpeculationInvalid(
                "target KV position exceeds the supported width",
            ))?;
        let state_size = context
            .sequence_state_size(target_sequence_id)
            .map_err(|error| GenError::Backend(error.to_string()))?;
        if state_size == 0 || state_size > MAX_SPECULATIVE_KV_STATE_BYTES {
            return Err(GenError::SpeculationInvalid(
                "target KV snapshot exceeds the retained-state bound",
            ));
        }
        let original_state = context
            .sequence_state(target_sequence_id)
            .map_err(|error| GenError::Backend(error.to_string()))?
            .into_boxed_slice();
        Self::clear_sequence(context, scratch_sequence_id, "fork scratch cleanup")?;
        context
            .memory_seq_cp(target_sequence_id, scratch_sequence_id, -1, -1)
            .map_err(|error| GenError::Backend(error.to_string()))?;
        if context
            .memory_seq_pos_max(scratch_sequence_id)
            .map_err(|error| GenError::Backend(error.to_string()))?
            != maximum_position
        {
            let _ = Self::clear_sequence(context, scratch_sequence_id, "failed fork cleanup");
            return Err(GenError::Backend(
                "native KV fork did not preserve sequence extent".to_owned(),
            ));
        }
        Ok(Self {
            target_sequence_id,
            scratch_sequence_id,
            position,
            original_state,
            reserved_cells,
        })
    }

    /// Appends scratch-only proposal rows to a caller-owned shared verification batch.
    pub fn append_proposals(
        &self,
        batch: &mut crate::ffi::Batch,
        proposal_tokens: &[i32],
    ) -> Result<Vec<i32>, GenError> {
        if proposal_tokens.is_empty()
            || proposal_tokens.len() > self.reserved_cells
            || proposal_tokens.len() > MAX_DRAFT_TOKENS
        {
            return Err(GenError::SpeculationInvalid(
                "proposal decode exceeds the target KV fork reservation",
            ));
        }
        let first_row = batch.token_count();
        for (offset, token) in proposal_tokens.iter().enumerate() {
            let position = self
                .position
                .checked_add(offset)
                .and_then(|position| i32::try_from(position).ok())
                .ok_or(GenError::SpeculationInvalid(
                    "proposal position exceeds native width",
                ))?;
            batch
                .add_token(*token, position, &[self.scratch_sequence_id], true)
                .map_err(|error| GenError::Backend(error.to_string()))?;
        }
        (first_row..first_row.saturating_add(proposal_tokens.len()))
            .map(|index| {
                i32::try_from(index).map_err(|_| {
                    GenError::SpeculationInvalid("shared verification row exceeds native width")
                })
            })
            .collect()
    }

    /// Trims rejected scratch suffix, commits accepted KV, and releases scratch.
    pub fn commit(
        &self,
        context: &mut crate::ffi::Context,
        accepted_tokens: usize,
    ) -> Result<(), GenError> {
        if accepted_tokens > self.reserved_cells || accepted_tokens > MAX_DRAFT_TOKENS {
            return Err(GenError::SpeculationInvalid(
                "accepted KV prefix exceeds the fork reservation",
            ));
        }
        let trim_position = self
            .position
            .checked_add(accepted_tokens)
            .and_then(|position| i32::try_from(position).ok())
            .ok_or(GenError::SpeculationInvalid(
                "accepted KV position exceeds native width",
            ))?;
        if !context
            .memory_seq_rm(self.scratch_sequence_id, trim_position, -1)
            .map_err(|error| GenError::Backend(error.to_string()))?
        {
            return self.restore_after_failure(context, "failed to trim speculative scratch KV");
        }
        if accepted_tokens == 0 {
            let state = context
                .sequence_state(self.target_sequence_id)
                .map_err(|error| GenError::Backend(error.to_string()))?;
            Self::clear_sequence(context, self.scratch_sequence_id, "empty commit cleanup")?;
            if state.as_slice() != self.original_state.as_ref() {
                return self
                    .restore_after_failure(context, "empty speculative commit changed target KV");
            }
            return Ok(());
        }
        if Self::clear_sequence(context, self.target_sequence_id, "commit target cleanup").is_err()
            || context
                .memory_seq_cp(self.scratch_sequence_id, self.target_sequence_id, -1, -1)
                .is_err()
        {
            return self.restore_after_failure(context, "failed to install speculative target KV");
        }
        let expected = self
            .position
            .checked_add(accepted_tokens)
            .and_then(|position| position.checked_sub(1))
            .and_then(|position| i32::try_from(position).ok())
            .ok_or(GenError::SpeculationInvalid(
                "committed KV extent exceeds native width",
            ))?;
        let actual = context
            .memory_seq_pos_max(self.target_sequence_id)
            .map_err(|error| GenError::Backend(error.to_string()))?;
        if actual != expected {
            return self.restore_after_failure(context, "committed target KV extent is incorrect");
        }
        Self::clear_sequence(context, self.scratch_sequence_id, "commit scratch cleanup")
    }

    /// Restores byte-exact target state and releases scratch after verification failure/cancel.
    pub fn rollback(&self, context: &mut crate::ffi::Context) -> Result<(), GenError> {
        self.restore_original(context)?;
        Self::clear_sequence(
            context,
            self.scratch_sequence_id,
            "rollback scratch cleanup",
        )
    }

    fn restore_after_failure(
        &self,
        context: &mut crate::ffi::Context,
        operation: &str,
    ) -> Result<(), GenError> {
        match self.restore_original(context) {
            Ok(()) => {
                let _ = Self::clear_sequence(context, self.scratch_sequence_id, operation);
                Err(GenError::Backend(operation.to_owned()))
            }
            Err(error) => Err(error),
        }
    }

    fn restore_original(&self, context: &mut crate::ffi::Context) -> Result<(), GenError> {
        Self::clear_sequence(context, self.target_sequence_id, "restore target cleanup")
            .map_err(|error| GenError::SpeculationContextInvalidated(error.to_string()))?;
        context
            .restore_sequence_state(self.target_sequence_id, &self.original_state)
            .map_err(|error| GenError::SpeculationContextInvalidated(error.to_string()))?;
        let restored = context
            .sequence_state(self.target_sequence_id)
            .map_err(|error| GenError::SpeculationContextInvalidated(error.to_string()))?;
        if restored.as_slice() != self.original_state.as_ref() {
            return Err(GenError::SpeculationContextInvalidated(
                "byte-exact speculative KV restore verification failed".to_owned(),
            ));
        }
        Ok(())
    }

    fn clear_sequence(
        context: &mut crate::ffi::Context,
        sequence_id: i32,
        operation: &str,
    ) -> Result<(), GenError> {
        let removed = context
            .memory_seq_rm(sequence_id, -1, -1)
            .map_err(|error| GenError::Backend(error.to_string()))?;
        if removed {
            Ok(())
        } else {
            Err(GenError::Backend(format!(
                "native KV {operation} did not remove the complete sequence"
            )))
        }
    }
}
