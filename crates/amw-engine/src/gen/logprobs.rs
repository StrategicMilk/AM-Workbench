//! Optional top-k log-probability derivation from backend logits.

use std::collections::BTreeSet;

use super::GenError;

/// Maximum OpenAI-compatible alternatives retained for one generated token.
pub const MAX_TOP_LOGPROBS: usize = 20;
/// Maximum vocabulary rows normalized at one decode boundary.
pub const MAX_LOGIT_CANDIDATES: usize = 1_048_576;
/// Maximum caller-supplied token-piece bytes retained during normalization.
pub const MAX_LOGIT_METADATA_BYTES: usize = 64 * 1024 * 1024;

/// One raw backend candidate before softmax normalization.
#[derive(Clone, Debug, PartialEq)]
pub struct LogitCandidate {
    /// Backend token identifier.
    pub token_id: i32,
    /// Exact token bytes.
    pub bytes: Vec<u8>,
    /// Raw model logit.
    pub logit: f32,
}

/// One token candidate in OpenAI-compatible log-domain form.
#[derive(Clone, Debug, PartialEq)]
pub struct TokenLogprob {
    /// Backend token identifier.
    pub token_id: i32,
    /// Exact token bytes.
    pub bytes: Vec<u8>,
    /// Natural-log probability normalized across the full candidate set.
    pub logprob: f32,
}

/// Derives and captures exactly `k` highest log probabilities when enabled.
///
/// All logits participate in the log-sum-exp denominator before truncation;
/// sorting raw caller-supplied values would not produce valid logprobs.
pub fn capture_top_logprobs(
    capture: bool,
    k: usize,
    candidates: impl IntoIterator<Item = LogitCandidate>,
) -> Result<Option<Vec<TokenLogprob>>, GenError> {
    if !capture {
        return Ok(None);
    }
    if k > MAX_TOP_LOGPROBS {
        return Err(GenError::InvalidLogits(
            "requested top-logprob count exceeds the bound",
        ));
    }
    if k == 0 {
        return Ok(Some(Vec::new()));
    }
    let normalized = normalize_all(candidates)?;
    Ok(Some(into_bounded_top(normalized, k)))
}

pub(crate) fn into_bounded_top(
    normalized: Vec<TokenLogprob>,
    requested: usize,
) -> Vec<TokenLogprob> {
    let retained = requested.min(normalized.len());
    let mut top = Vec::with_capacity(retained);
    top.extend(normalized.into_iter().take(retained));
    top
}

pub(crate) fn normalize_all(
    candidates: impl IntoIterator<Item = LogitCandidate>,
) -> Result<Vec<TokenLogprob>, GenError> {
    let values: Vec<_> = candidates
        .into_iter()
        .take(MAX_LOGIT_CANDIDATES + 1)
        .collect();
    if values.len() > MAX_LOGIT_CANDIDATES {
        return Err(GenError::InvalidLogits(
            "candidate count exceeds the vocabulary bound",
        ));
    }
    if values.is_empty() {
        return Err(GenError::InvalidLogits("candidate set is empty"));
    }
    if values
        .iter()
        .any(|candidate| candidate.token_id < 0 || !candidate.logit.is_finite())
    {
        return Err(GenError::InvalidLogits(
            "token identifiers and logits must be valid and finite",
        ));
    }
    let metadata_bytes = values.iter().try_fold(0_usize, |total, candidate| {
        total.checked_add(candidate.bytes.capacity())
    });
    if metadata_bytes.is_none_or(|bytes| bytes > MAX_LOGIT_METADATA_BYTES) {
        return Err(GenError::InvalidLogits(
            "candidate token bytes exceed the metadata bound",
        ));
    }
    let unique_tokens: BTreeSet<_> = values.iter().map(|candidate| candidate.token_id).collect();
    if unique_tokens.len() != values.len() {
        return Err(GenError::InvalidLogits(
            "candidate token identifiers must be unique",
        ));
    }
    let maximum = values
        .iter()
        .map(|candidate| f64::from(candidate.logit))
        .fold(f64::NEG_INFINITY, f64::max);
    let scaled_sum = values
        .iter()
        .map(|candidate| (f64::from(candidate.logit) - maximum).exp())
        .sum::<f64>();
    if !scaled_sum.is_finite() || scaled_sum <= 0.0 {
        return Err(GenError::InvalidLogits("softmax denominator is invalid"));
    }
    let log_denominator = maximum + scaled_sum.ln();
    let mut normalized: Vec<_> = values
        .into_iter()
        .map(|candidate| TokenLogprob {
            token_id: candidate.token_id,
            bytes: candidate.bytes,
            logprob: ((f64::from(candidate.logit) - log_denominator).min(0.0)) as f32,
        })
        .collect();
    normalized.sort_by(|left, right| right.logprob.total_cmp(&left.logprob));
    Ok(normalized)
}
