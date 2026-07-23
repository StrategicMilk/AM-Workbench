//! Fail-closed fill-in-the-middle metadata validation and sequence assembly.

use super::GenError;

/// Maximum assembled FIM prompt length, including three sentinels.
pub const MAX_INFILL_TOKENS: usize = 1_048_576;

/// Model families with explicit FIM sentinel conventions.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ModelFamily {
    /// Code Llama family.
    CodeLlama,
    /// DeepSeek Coder family.
    DeepSeekCoder,
    /// StarCoder family.
    StarCoder,
    /// Qwen Coder family.
    QwenCoder,
}

/// Raw, optional sentinel identifiers read from loaded model metadata.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct FimTokenMetadata {
    /// Model family whose tokenizer owns the sentinels.
    pub family: ModelFamily,
    /// Prefix sentinel, if advertised.
    pub prefix: Option<i32>,
    /// Suffix sentinel, if advertised.
    pub suffix: Option<i32>,
    /// Middle sentinel, if advertised.
    pub middle: Option<i32>,
}

/// Validated, model-family-owned FIM sentinel map.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct FimTokenMap {
    family: ModelFamily,
    prefix: i32,
    suffix: i32,
    middle: i32,
}

impl FimTokenMap {
    /// Validates complete, non-negative, distinct sentinel metadata.
    pub fn from_metadata(metadata: FimTokenMetadata) -> Result<Self, GenError> {
        let (Some(prefix), Some(suffix), Some(middle)) =
            (metadata.prefix, metadata.suffix, metadata.middle)
        else {
            return Err(GenError::FimUnsupported);
        };
        if prefix < 0 || suffix < 0 || middle < 0 {
            return Err(GenError::InvalidFimSentinels(
                "token identifiers must be non-negative",
            ));
        }
        if prefix == suffix || prefix == middle || suffix == middle {
            return Err(GenError::InvalidFimSentinels(
                "prefix, suffix, and middle sentinels must be distinct",
            ));
        }
        Ok(Self {
            family: metadata.family,
            prefix,
            suffix,
            middle,
        })
    }

    /// Resolves and validates FIM identifiers directly from loaded native metadata.
    #[cfg(any(feature = "cpu", feature = "cuda"))]
    pub fn from_model(family: ModelFamily, model: &crate::ffi::Model) -> Result<Self, GenError> {
        let tokens = model.fim_tokens().ok_or(GenError::FimUnsupported)?;
        Self::from_metadata(FimTokenMetadata {
            family,
            prefix: Some(tokens.prefix),
            suffix: Some(tokens.suffix),
            middle: Some(tokens.middle),
        })
    }

    /// Model family proven by the loaded metadata.
    pub fn family(self) -> ModelFamily {
        self.family
    }

    /// Prefix sentinel token.
    pub fn prefix(self) -> i32 {
        self.prefix
    }

    /// Suffix sentinel token.
    pub fn suffix(self) -> i32 {
        self.suffix
    }

    /// Middle sentinel token.
    pub fn middle(self) -> i32 {
        self.middle
    }

    fn contains(self, token: i32) -> bool {
        token == self.prefix || token == self.suffix || token == self.middle
    }
}

/// Assembles `[prefix-sentinel, prefix, suffix-sentinel, suffix, middle-sentinel]`.
pub fn assemble_infill(
    sentinels: Option<FimTokenMap>,
    prefix: &[i32],
    suffix: &[i32],
) -> Result<Vec<i32>, GenError> {
    let map = sentinels.ok_or(GenError::FimUnsupported)?;
    if prefix
        .iter()
        .chain(suffix)
        .any(|token| *token < 0 || map.contains(*token))
    {
        return Err(GenError::InvalidFimSentinels(
            "prompt tokens must not contain invalid or reserved sentinel identifiers",
        ));
    }
    let capacity = prefix
        .len()
        .checked_add(suffix.len())
        .and_then(|length| length.checked_add(3))
        .filter(|length| *length <= MAX_INFILL_TOKENS)
        .ok_or(GenError::InvalidFimSentinels(
            "assembled infill prompt exceeds the token bound",
        ))?;
    let mut tokens = Vec::with_capacity(capacity);
    tokens.push(map.prefix);
    tokens.extend_from_slice(prefix);
    tokens.push(map.suffix);
    tokens.extend_from_slice(suffix);
    tokens.push(map.middle);
    Ok(tokens)
}
