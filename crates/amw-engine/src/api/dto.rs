use std::collections::BTreeMap;

use serde::{Deserialize, Serialize};
use serde_json::Value;

use super::error::{ApiError, EngineErrorCode, API_SCHEMA_VERSION};
use crate::{receipt::EvalContext, runtime::WorkloadRole};

pub const MAX_PROMPT_BYTES: usize = 8 * 1024 * 1024;
pub const MAX_REQUEST_BODY_BYTES: usize = 16 * 1024 * 1024;
pub const MAX_BATCH_ITEMS: usize = 256;
pub const MAX_BATCH_ITEM_BYTES: usize = 256 * 1024;
pub const MAX_BATCH_INPUT_BYTES: usize = 1024 * 1024;
pub const MAX_BATCH_ACTUAL_TOKENS: usize = 262_144;

const fn default_schema_version() -> u32 {
    API_SCHEMA_VERSION
}

#[derive(Clone, Copy, Debug, Default, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct ControlVersion {
    pub schema_version: u32,
}

impl ControlVersion {
    pub fn validate(self) -> Result<(), ApiError> {
        require_schema(self.schema_version)
    }
}

#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct CompletionRequest {
    #[serde(default = "default_schema_version")]
    pub schema_version: u32,
    pub model: Option<String>,
    pub prompt: String,
    #[serde(default = "default_max_tokens")]
    pub max_tokens: u32,
    pub temperature: Option<f32>,
    pub top_k: Option<u32>,
    pub top_p: Option<f32>,
    pub min_p: Option<f32>,
    pub typical_p: Option<f32>,
    pub repeat_penalty: Option<f32>,
    pub presence_penalty: Option<f32>,
    pub frequency_penalty: Option<f32>,
    pub logit_bias: Option<BTreeMap<String, f32>>,
    pub seed: Option<u64>,
    pub dry_multiplier: Option<f32>,
    pub dry_base: Option<f32>,
    pub dry_allowed_length: Option<u32>,
    pub xtc_probability: Option<f32>,
    pub xtc_threshold: Option<f32>,
    pub top_n_sigma: Option<f32>,
    pub grammar: Option<String>,
    pub json_schema: Option<Value>,
    #[serde(default)]
    pub stop: Vec<String>,
    pub priority_class: Option<String>,
    pub role: Option<WorkloadRole>,
    pub eval_slot: Option<usize>,
    pub eval_context: Option<EvalContext>,
    pub session_id: Option<String>,
    #[serde(default)]
    pub prefix_refs: Vec<PrefixReference>,
    #[serde(default)]
    pub stream: bool,
}

const fn default_max_tokens() -> u32 {
    256
}

impl CompletionRequest {
    pub fn validate(&self) -> Result<(), ApiError> {
        require_schema(self.schema_version)?;
        if self.prompt.len() > MAX_PROMPT_BYTES {
            return Err(ApiError::new(
                EngineErrorCode::ContextOverflow,
                "prompt exceeds 8 MiB",
            ));
        }
        if self.max_tokens == 0 {
            return Err(unsupported("max_tokens must be positive"));
        }
        validate_probability("top_p", self.top_p, true)?;
        validate_probability("min_p", self.min_p, true)?;
        validate_probability("typical_p", self.typical_p, true)?;
        validate_probability("xtc_probability", self.xtc_probability, true)?;
        validate_probability("xtc_threshold", self.xtc_threshold, true)?;
        validate_finite("temperature", self.temperature)?;
        validate_finite("repeat_penalty", self.repeat_penalty)?;
        validate_finite("presence_penalty", self.presence_penalty)?;
        validate_finite("frequency_penalty", self.frequency_penalty)?;
        validate_finite("dry_multiplier", self.dry_multiplier)?;
        validate_finite("dry_base", self.dry_base)?;
        validate_finite("top_n_sigma", self.top_n_sigma)?;
        if self.temperature.is_some_and(|value| value < 0.0) {
            return Err(unsupported("temperature must be non-negative"));
        }
        if self.repeat_penalty.is_some_and(|value| value <= 0.0) {
            return Err(unsupported("repeat_penalty must be positive"));
        }
        if self.dry_multiplier.is_some_and(|value| value < 0.0)
            || self.dry_base.is_some_and(|value| value <= 0.0)
            || self.top_n_sigma.is_some_and(|value| value < 0.0)
        {
            return Err(unsupported(
                "DRY and top_n_sigma values must be within their non-negative domains",
            ));
        }
        if self.top_k.is_some_and(|value| value > i32::MAX as u32)
            || self
                .dry_allowed_length
                .is_some_and(|value| value > i32::MAX as u32)
        {
            return Err(unsupported("sampler integer value exceeds native width"));
        }
        if self.logit_bias.as_ref().is_some_and(|biases| {
            biases
                .iter()
                .any(|(token, value)| token.parse::<i32>().is_err() || !value.is_finite())
        }) {
            return Err(unsupported(
                "logit_bias keys must be token ids and values must be finite",
            ));
        }
        if self
            .grammar
            .as_deref()
            .is_some_and(|grammar| grammar.trim().is_empty())
        {
            return Err(ApiError::new(
                EngineErrorCode::GrammarInvalid,
                "grammar must not be empty",
            ));
        }
        if let Some(context) = &self.eval_context {
            context.validate().map_err(|error| {
                ApiError::new(EngineErrorCode::UnsupportedParam, error.to_string())
            })?;
        }
        Ok(())
    }
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct PrefixReference {
    pub name: String,
    pub content_hash: String,
}

#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
pub struct CompletionResponse {
    pub schema_version: u32,
    pub id: String,
    pub request_id: String,
    pub trace_id: String,
    pub object: String,
    pub model: String,
    pub text: String,
    pub prompt_tokens: u32,
    pub completion_tokens: u32,
    pub confidence: f32,
    pub finish_reason: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub engine_receipt: Option<crate::receipt::SignedEvalReceipt>,
}

/// IS3.2 confidence: geometric mean of selected token probabilities.
pub fn confidence(probabilities: &[f32]) -> f32 {
    if probabilities.is_empty() {
        return 0.0;
    }
    let sum = probabilities
        .iter()
        .map(|p| p.clamp(f32::MIN_POSITIVE, 1.0).ln())
        .sum::<f32>();
    (sum / probabilities.len() as f32).exp()
}

#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct InfillRequest {
    #[serde(flatten)]
    pub completion: CompletionRequest,
    pub suffix: String,
}

#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct BatchTextRequest {
    #[serde(default = "default_schema_version")]
    pub schema_version: u32,
    pub items: Vec<String>,
    pub model: Option<String>,
    #[serde(default)]
    pub add_special: bool,
}

impl BatchTextRequest {
    pub fn validate(&self) -> Result<(), ApiError> {
        require_schema(self.schema_version)?;
        validate_batch_strings(&self.items)
    }
}

pub fn validate_batch_strings(items: &[String]) -> Result<(), ApiError> {
    if items.is_empty() {
        return Err(unsupported("items must contain at least one string"));
    }
    if items.len() > MAX_BATCH_ITEMS {
        return Err(unsupported(format!(
            "batch contains {} items; maximum is {MAX_BATCH_ITEMS}",
            items.len()
        )));
    }
    if let Some(item) = items.iter().find(|item| item.len() > MAX_BATCH_ITEM_BYTES) {
        return Err(ApiError::new(
            EngineErrorCode::ContextOverflow,
            format!(
                "batch item is {} bytes; maximum is {MAX_BATCH_ITEM_BYTES}",
                item.len()
            ),
        ));
    }
    let aggregate_bytes = items.iter().try_fold(0_usize, |total, item| {
        total.checked_add(item.len()).ok_or_else(|| {
            ApiError::new(
                EngineErrorCode::ContextOverflow,
                "batch aggregate input size overflowed",
            )
        })
    })?;
    if aggregate_bytes > MAX_BATCH_INPUT_BYTES {
        return Err(ApiError::new(
            EngineErrorCode::ContextOverflow,
            format!(
                "batch aggregate input is {aggregate_bytes} bytes; maximum is {MAX_BATCH_INPUT_BYTES}"
            ),
        ));
    }
    Ok(())
}

pub fn require_schema(version: u32) -> Result<(), ApiError> {
    if version != API_SCHEMA_VERSION {
        return Err(ApiError::new(
            EngineErrorCode::VersionMismatch,
            format!("schema_version {version} is unsupported; expected {API_SCHEMA_VERSION}"),
        ));
    }
    Ok(())
}

pub fn parse_completion(bytes: &[u8]) -> Result<CompletionRequest, ApiError> {
    if bytes.len() > MAX_REQUEST_BODY_BYTES {
        return Err(ApiError::new(
            EngineErrorCode::ContextOverflow,
            "request body exceeds 16 MiB",
        ));
    }
    let request: CompletionRequest = serde_json::from_slice(bytes)
        .map_err(|error| ApiError::new(EngineErrorCode::UnsupportedParam, error.to_string()))?;
    request.validate()?;
    Ok(request)
}

fn validate_finite(name: &'static str, value: Option<f32>) -> Result<(), ApiError> {
    if value.is_some_and(|value| !value.is_finite()) {
        return Err(unsupported(format!("{name} must be finite")));
    }
    Ok(())
}

fn validate_probability(
    name: &'static str,
    value: Option<f32>,
    inclusive: bool,
) -> Result<(), ApiError> {
    validate_finite(name, value)?;
    if value.is_some_and(|value| {
        if inclusive {
            !(0.0..=1.0).contains(&value)
        } else {
            !(0.0..1.0).contains(&value)
        }
    }) {
        return Err(unsupported(format!("{name} must be between 0 and 1")));
    }
    Ok(())
}

fn unsupported(message: impl Into<String>) -> ApiError {
    ApiError::new(EngineErrorCode::UnsupportedParam, message)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn control_version_is_required_and_wrong_major_fails_closed() {
        #[derive(Deserialize)]
        #[serde(deny_unknown_fields)]
        struct Control {
            #[serde(flatten)]
            version: ControlVersion,
            enabled: bool,
        }

        assert!(serde_json::from_value::<Control>(serde_json::json!({"enabled": true})).is_err());
        let control: Control = serde_json::from_value(serde_json::json!({
            "schema_version": 99,
            "enabled": true
        }))
        .unwrap();
        assert!(control.enabled);
        assert_eq!(
            control.version.validate().unwrap_err().body.code,
            EngineErrorCode::VersionMismatch
        );
    }

    #[test]
    fn request_roles_collapse_unrecognized_values_into_one_bounded_bucket() {
        for (wire, expected) in [
            ("foreman", WorkloadRole::Foreman),
            ("worker", WorkloadRole::Worker),
            ("inspector", WorkloadRole::Inspector),
            ("unknown", WorkloadRole::Unknown),
        ] {
            let role: WorkloadRole =
                serde_json::from_value(Value::String(wire.to_owned())).unwrap();
            assert_eq!(role, expected);
        }
        let role: WorkloadRole = serde_json::from_str("\"attacker-unique-role\"").unwrap();
        assert_eq!(role, WorkloadRole::Unknown);
        assert_eq!(role.as_str(), "unknown");
    }

    #[test]
    fn fragmented_batches_hit_each_preallocation_boundary() {
        let too_many = vec!["x".to_owned(); MAX_BATCH_ITEMS + 1];
        assert!(validate_batch_strings(&too_many).is_err());
        assert!(validate_batch_strings(&["x".repeat(MAX_BATCH_ITEM_BYTES + 1)]).is_err());
        let aggregate = vec!["x".repeat(MAX_BATCH_ITEM_BYTES); 5];
        assert!(validate_batch_strings(&aggregate).is_err());
        assert!(validate_batch_strings(&vec!["x".to_owned(); MAX_BATCH_ITEMS]).is_ok());
    }
}
