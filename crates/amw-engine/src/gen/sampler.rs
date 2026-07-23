//! Deterministic request-local sampler-chain construction.
//!
//! A fixed seed is bit-stable for a single sequence. Continuous batching can
//! alter candidate composition and therefore does not promise bit identity.

use std::collections::BTreeMap;

use sha2::{Digest, Sha256};

use super::GenError;

/// Complete engine-side sampler request.
#[derive(Clone, Debug, PartialEq)]
pub struct SamplerParams {
    pub temperature: f32,
    pub top_k: u32,
    pub top_p: f32,
    pub min_p: f32,
    pub typical_p: f32,
    pub repetition_penalty: f32,
    pub presence_penalty: f32,
    pub frequency_penalty: f32,
    pub logit_bias: BTreeMap<i32, f32>,
    pub seed: u64,
    pub mirostat_mode: u8,
    pub mirostat_tau: f32,
    pub mirostat_eta: f32,
    pub dry_multiplier: f32,
    pub dry_base: f32,
    pub dry_allowed_length: u32,
    pub xtc_probability: f32,
    pub xtc_threshold: f32,
    pub top_n_sigma: f32,
}

impl Default for SamplerParams {
    fn default() -> Self {
        Self {
            temperature: 0.8,
            top_k: 40,
            top_p: 0.95,
            min_p: 0.05,
            typical_p: 1.0,
            repetition_penalty: 1.0,
            presence_penalty: 0.0,
            frequency_penalty: 0.0,
            logit_bias: BTreeMap::new(),
            seed: 0,
            mirostat_mode: 0,
            mirostat_tau: 5.0,
            mirostat_eta: 0.1,
            dry_multiplier: 0.0,
            dry_base: 1.75,
            dry_allowed_length: 2,
            xtc_probability: 0.0,
            xtc_threshold: 0.1,
            top_n_sigma: 0.0,
        }
    }
}

/// Feature support exposed by the pinned native backend.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct SamplerCapabilities {
    pub typical: bool,
    pub logit_bias: bool,
    pub mirostat: bool,
    pub dry: bool,
    pub xtc: bool,
    pub top_n_sigma: bool,
}

impl SamplerCapabilities {
    /// Capabilities present at the crate's pinned llama.cpp revision.
    pub const fn pinned_revision() -> Self {
        Self {
            typical: true,
            logit_bias: true,
            mirostat: true,
            dry: true,
            xtc: true,
            top_n_sigma: true,
        }
    }
}

/// Ordered stage in the native sampler chain.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum SamplerStage {
    LogitBias,
    Penalties,
    Dry,
    TopNSigma,
    TopK,
    Typical,
    TopP,
    MinP,
    Xtc,
    Temperature,
    Mirostat,
    Distribution,
    Greedy,
}

/// Validated chain descriptor consumed by scheduler sequence construction.
#[derive(Clone, Debug, PartialEq)]
pub struct SamplerChain {
    /// Per-sequence random seed.
    pub seed: u64,
    /// Active stages in llama.cpp default-chain order.
    pub stages: Vec<SamplerStage>,
}

impl SamplerChain {
    /// Builds a fail-closed chain; active unsupported stages never disappear silently.
    pub fn build(params: &SamplerParams, caps: SamplerCapabilities) -> Result<Self, GenError> {
        params.validate(caps)?;
        let mut stages = Vec::new();
        if !params.logit_bias.is_empty() {
            stages.push(SamplerStage::LogitBias);
        }
        if params.repetition_penalty != 1.0
            || params.presence_penalty != 0.0
            || params.frequency_penalty != 0.0
        {
            stages.push(SamplerStage::Penalties);
        }
        if params.dry_multiplier > 0.0 {
            stages.push(SamplerStage::Dry);
        }
        if params.top_n_sigma > 0.0 {
            stages.push(SamplerStage::TopNSigma);
        }
        if params.top_k > 0 {
            stages.push(SamplerStage::TopK);
        }
        if params.typical_p < 1.0 {
            stages.push(SamplerStage::Typical);
        }
        if params.top_p < 1.0 {
            stages.push(SamplerStage::TopP);
        }
        if params.min_p > 0.0 {
            stages.push(SamplerStage::MinP);
        }
        if params.xtc_probability > 0.0 {
            stages.push(SamplerStage::Xtc);
        }
        if params.temperature > 0.0 {
            stages.push(SamplerStage::Temperature);
            if params.mirostat_mode > 0 {
                stages.push(SamplerStage::Mirostat);
            } else {
                stages.push(SamplerStage::Distribution);
            }
        } else {
            stages.push(SamplerStage::Greedy);
        }
        Ok(Self {
            seed: params.seed,
            stages,
        })
    }

    /// Constructs the pinned llama.cpp chain through the RAII-only FFI surface.
    #[cfg(any(feature = "cpu", feature = "cuda"))]
    pub fn build_native(
        params: &SamplerParams,
        caps: SamplerCapabilities,
        model: &crate::ffi::Model,
    ) -> Result<crate::ffi::Sampler, GenError> {
        Self::build_native_with_grammar(params, caps, model, None)
    }

    /// Constructs the complete native sampler chain with grammar filtering before selection.
    #[cfg(any(feature = "cpu", feature = "cuda"))]
    pub fn build_native_with_grammar(
        params: &SamplerParams,
        caps: SamplerCapabilities,
        model: &crate::ffi::Model,
        grammar: Option<&super::CompiledGrammar>,
    ) -> Result<crate::ffi::Sampler, GenError> {
        let _descriptor = Self::build(params, caps)?;
        params.validate_for_vocab(model.vocab_size())?;
        let seed = u32::try_from(params.seed).map_err(|_| GenError::UnsupportedParam("seed"))?;
        let top_k = i32::try_from(params.top_k)
            .map_err(|_| invalid("top_k", "must fit the native signed width"))?;
        let dry_allowed_length = i32::try_from(params.dry_allowed_length)
            .map_err(|_| invalid("dry_allowed_length", "must fit the native signed width"))?;
        let mut chain = crate::ffi::Sampler::chain().map_err(native_error)?;

        if !params.logit_bias.is_empty() {
            let biases: Vec<_> = params.logit_bias.iter().map(|(k, v)| (*k, *v)).collect();
            chain.add_logit_bias(model, &biases).map_err(native_error)?;
        }
        if let Some(grammar) = grammar {
            let started = std::time::Instant::now();
            chain
                .add_grammar(model, grammar.source(), "root")
                .map_err(|error| GenError::GrammarInvalid(error.to_string()))?;
            if started.elapsed() > super::grammar::GRAMMAR_NATIVE_COMPILE_TIMEOUT {
                return Err(GenError::GrammarResourceLimit(
                    "native grammar compile deadline",
                ));
            }
        }
        if params.repetition_penalty != 1.0
            || params.frequency_penalty != 0.0
            || params.presence_penalty != 0.0
        {
            chain
                .add_penalties(
                    -1,
                    params.repetition_penalty,
                    params.frequency_penalty,
                    params.presence_penalty,
                )
                .map_err(native_error)?;
        }
        if params.dry_multiplier > 0.0 {
            chain
                .add_dry(
                    model,
                    params.dry_multiplier,
                    params.dry_base,
                    dry_allowed_length,
                    -1,
                    &["\n", ":", "\"", "*"],
                )
                .map_err(native_error)?;
        }
        if params.top_n_sigma > 0.0 {
            chain
                .add_top_n_sigma(params.top_n_sigma)
                .map_err(native_error)?;
        }
        if params.top_k > 0 {
            chain.add_top_k(top_k).map_err(native_error)?;
        }
        if params.typical_p < 1.0 {
            chain
                .add_typical(params.typical_p, 1)
                .map_err(native_error)?;
        }
        if params.top_p < 1.0 {
            chain.add_top_p(params.top_p, 1).map_err(native_error)?;
        }
        if params.min_p > 0.0 {
            chain.add_min_p(params.min_p, 1).map_err(native_error)?;
        }
        if params.xtc_probability > 0.0 {
            chain
                .add_xtc(params.xtc_probability, params.xtc_threshold, 1, seed)
                .map_err(native_error)?;
        }
        if params.temperature > 0.0 {
            chain
                .add_temperature(params.temperature)
                .map_err(native_error)?;
            if params.mirostat_mode > 0 {
                chain
                    .add_mirostat(
                        params.mirostat_mode,
                        model,
                        seed,
                        params.mirostat_tau,
                        params.mirostat_eta,
                    )
                    .map_err(native_error)?;
            } else {
                chain.add_distribution(seed).map_err(native_error)?;
            }
        } else {
            chain.add_greedy().map_err(native_error)?;
        }
        Ok(chain)
    }

    /// Produces a deterministic choice from a stable candidate slice.
    pub fn deterministic_index(&self, candidate_count: usize, step: u64) -> Option<usize> {
        if candidate_count == 0 {
            return None;
        }
        let candidate_count = u64::try_from(candidate_count).ok()?;
        let mut x = self.seed ^ step.wrapping_mul(0x9E37_79B9_7F4A_7C15);
        x ^= x >> 12;
        x ^= x << 25;
        x ^= x >> 27;
        usize::try_from(x.wrapping_mul(0x2545_F491_4F6C_DD1D) % candidate_count).ok()
    }
}

impl SamplerParams {
    /// Returns the canonical SHA-256 identity of every sampler control.
    ///
    /// Floating-point values are encoded through their exact IEEE-754 bit
    /// patterns, while the `BTreeMap` preserves a stable token-bias order.
    pub fn identity_sha256(&self) -> [u8; 32] {
        let mut hasher = Sha256::new();
        hasher.update(b"AMW\0sampler-params-v1\0");
        for value in [
            self.temperature,
            self.top_p,
            self.min_p,
            self.typical_p,
            self.repetition_penalty,
            self.presence_penalty,
            self.frequency_penalty,
            self.mirostat_tau,
            self.mirostat_eta,
            self.dry_multiplier,
            self.dry_base,
            self.xtc_probability,
            self.xtc_threshold,
            self.top_n_sigma,
        ] {
            hasher.update(value.to_bits().to_be_bytes());
        }
        hasher.update(self.top_k.to_be_bytes());
        hasher.update(self.seed.to_be_bytes());
        hasher.update([self.mirostat_mode]);
        hasher.update(self.dry_allowed_length.to_be_bytes());
        let bias_count = u32::try_from(self.logit_bias.len())
            .expect("sampler logit-bias count cannot exceed u32");
        hasher.update(bias_count.to_be_bytes());
        for (token_id, bias) in &self.logit_bias {
            hasher.update(token_id.to_be_bytes());
            hasher.update(bias.to_bits().to_be_bytes());
        }
        hasher.finalize().into()
    }

    /// Validates every sampler field before stage selection or integer narrowing.
    pub fn validate(&self, caps: SamplerCapabilities) -> Result<(), GenError> {
        non_negative("temperature", self.temperature)?;
        checked_i32("top_k", self.top_k)?;
        probability("top_p", self.top_p)?;
        probability("min_p", self.min_p)?;
        probability("typical_p", self.typical_p)?;
        positive("repetition_penalty", self.repetition_penalty)?;
        range("presence_penalty", self.presence_penalty, -2.0, 2.0)?;
        range("frequency_penalty", self.frequency_penalty, -2.0, 2.0)?;
        if self.seed > u64::from(u32::MAX) {
            return Err(invalid("seed", "must fit the native 32-bit seed width"));
        }
        if self.mirostat_mode > 2 {
            return Err(invalid("mirostat_mode", "must be 0, 1, or 2"));
        }
        positive("mirostat_tau", self.mirostat_tau)?;
        range("mirostat_eta", self.mirostat_eta, f32::MIN_POSITIVE, 1.0)?;
        non_negative("dry_multiplier", self.dry_multiplier)?;
        range("dry_base", self.dry_base, 1.0, f32::MAX)?;
        checked_i32("dry_allowed_length", self.dry_allowed_length)?;
        if self.dry_allowed_length == 0 {
            return Err(invalid("dry_allowed_length", "must be at least one token"));
        }
        probability("xtc_probability", self.xtc_probability)?;
        probability("xtc_threshold", self.xtc_threshold)?;
        non_negative("top_n_sigma", self.top_n_sigma)?;
        if self.mirostat_mode > 0 && self.temperature == 0.0 {
            return Err(invalid("mirostat_mode", "requires a positive temperature"));
        }
        for (token, bias) in &self.logit_bias {
            if *token < 0 {
                return Err(invalid("logit_bias", "token ids must be non-negative"));
            }
            range("logit_bias", *bias, -100.0, 100.0)?;
        }

        if self.typical_p < 1.0 && !caps.typical {
            return Err(GenError::UnsupportedParam("typical_p"));
        }
        if !self.logit_bias.is_empty() && !caps.logit_bias {
            return Err(GenError::UnsupportedParam("logit_bias"));
        }
        if self.mirostat_mode > 0 && !caps.mirostat {
            return Err(GenError::UnsupportedParam("mirostat"));
        }
        if self.dry_multiplier > 0.0 && !caps.dry {
            return Err(GenError::UnsupportedParam("dry_multiplier"));
        }
        if self.xtc_probability > 0.0 && !caps.xtc {
            return Err(GenError::UnsupportedParam("xtc_probability"));
        }
        if self.top_n_sigma > 0.0 && !caps.top_n_sigma {
            return Err(GenError::UnsupportedParam("top_n_sigma"));
        }
        Ok(())
    }

    /// Validates logit-bias token identifiers against the loaded native vocabulary.
    pub fn validate_for_vocab(&self, vocab_size: usize) -> Result<(), GenError> {
        if vocab_size == 0 {
            return Err(invalid("logit_bias", "loaded vocabulary is empty"));
        }
        for token in self.logit_bias.keys() {
            let token = usize::try_from(*token)
                .map_err(|_| invalid("logit_bias", "token ids must be non-negative"))?;
            if token >= vocab_size {
                return Err(invalid(
                    "logit_bias",
                    "token id is outside the loaded vocabulary",
                ));
            }
        }
        Ok(())
    }
}

fn invalid(name: &'static str, reason: &'static str) -> GenError {
    GenError::InvalidSamplerParam(name, reason)
}

fn finite(name: &'static str, value: f32) -> Result<(), GenError> {
    if value.is_finite() {
        Ok(())
    } else {
        Err(invalid(name, "must be finite"))
    }
}

fn range(name: &'static str, value: f32, minimum: f32, maximum: f32) -> Result<(), GenError> {
    finite(name, value)?;
    if (minimum..=maximum).contains(&value) {
        Ok(())
    } else {
        Err(invalid(name, "is outside the supported domain"))
    }
}

fn probability(name: &'static str, value: f32) -> Result<(), GenError> {
    range(name, value, 0.0, 1.0)
}

fn positive(name: &'static str, value: f32) -> Result<(), GenError> {
    finite(name, value)?;
    if value > 0.0 {
        Ok(())
    } else {
        Err(invalid(name, "must be greater than zero"))
    }
}

fn non_negative(name: &'static str, value: f32) -> Result<(), GenError> {
    finite(name, value)?;
    if value >= 0.0 {
        Ok(())
    } else {
        Err(invalid(name, "must not be negative"))
    }
}

fn checked_i32(name: &'static str, value: u32) -> Result<(), GenError> {
    i32::try_from(value)
        .map(|_| ())
        .map_err(|_| invalid(name, "must fit the native signed width"))
}

#[cfg(any(feature = "cpu", feature = "cuda"))]
fn native_error(error: crate::ffi::FfiError) -> GenError {
    GenError::NativeSampler(error.to_string())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sampler_identity_binds_float_bits_seed_and_sorted_biases() {
        let mut first = SamplerParams::default();
        first.seed = 17;
        first.logit_bias.insert(9, 0.25);
        first.logit_bias.insert(2, -0.5);
        let same = first.clone();
        let mut changed_seed = first.clone();
        changed_seed.seed = 18;
        let mut changed_float = first.clone();
        changed_float.temperature = 0.9;

        assert_eq!(first.identity_sha256(), same.identity_sha256());
        assert_ne!(first.identity_sha256(), changed_seed.identity_sha256());
        assert_ne!(first.identity_sha256(), changed_float.identity_sha256());
    }
}
