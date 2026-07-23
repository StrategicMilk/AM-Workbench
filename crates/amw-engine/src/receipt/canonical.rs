//! Cross-language canonicalization for terminal evaluation receipt claims.

use std::fmt;

use serde::{Deserialize, Deserializer, Serialize, Serializer};
use sha2::{Digest, Sha256};
use thiserror::Error;

/// Versioned canonicalization name carried on signed receipt envelopes.
pub const CANONICALIZATION_NAME: &str = "amw-eval-tlv-v1";

const RECEIPT_DOMAIN: &[u8] = b"AMW\0engine-eval-terminal-receipt\0";
const ATTEMPT_DOMAIN: &[u8] = b"AMW\0engine-eval-attempt\0";
const ABSENT_DOMAIN: &[u8] = b"AMW\0engine-eval-absent-v1\0";
const ORIGINAL_MESSAGES_DOMAIN: &[u8] = b"AMW\0engine-eval-original-messages-v1\0";
const SYSTEM_MESSAGES_DOMAIN: &[u8] = b"AMW\0engine-eval-system-messages-v1\0";
const CURRENT_SCHEMA_VERSION: u16 = 1;
const MAX_IDENTIFIER_BYTES: usize = 128;
const MAX_STRING_BYTES: usize = 4_096;

/// A raw SHA-256 value with strict lowercase-hex JSON representation.
#[derive(Clone, Copy, Eq, Hash, PartialEq)]
pub struct Digest32([u8; 32]);

impl Digest32 {
    /// Constructs a digest from its raw 32 bytes.
    #[must_use]
    pub const fn from_bytes(bytes: [u8; 32]) -> Self {
        Self(bytes)
    }

    /// Hashes bytes with SHA-256.
    #[must_use]
    pub fn sha256(bytes: &[u8]) -> Self {
        Self(Sha256::digest(bytes).into())
    }

    /// Parses exactly 64 lowercase hexadecimal characters.
    pub fn from_lower_hex(value: &str) -> Result<Self, CanonicalError> {
        if value.len() != 64
            || !value
                .as_bytes()
                .iter()
                .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(byte))
        {
            return Err(CanonicalError::InvalidDigestHex);
        }
        let mut bytes = [0_u8; 32];
        hex::decode_to_slice(value, &mut bytes).map_err(|_| CanonicalError::InvalidDigestHex)?;
        Ok(Self(bytes))
    }

    /// Returns the digest as raw bytes.
    #[must_use]
    pub const fn as_bytes(&self) -> &[u8; 32] {
        &self.0
    }

    /// Returns the strict lowercase hexadecimal representation.
    #[must_use]
    pub fn to_lower_hex(self) -> String {
        hex::encode(self.0)
    }
}

impl fmt::Debug for Digest32 {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_tuple("Digest32")
            .field(&self.to_lower_hex())
            .finish()
    }
}

impl fmt::Display for Digest32 {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(&self.to_lower_hex())
    }
}

impl Serialize for Digest32 {
    fn serialize<S>(&self, serializer: S) -> Result<S::Ok, S::Error>
    where
        S: Serializer,
    {
        serializer.serialize_str(&self.to_lower_hex())
    }
}

impl<'de> Deserialize<'de> for Digest32 {
    fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
    where
        D: Deserializer<'de>,
    {
        let value = String::deserialize(deserializer)?;
        Self::from_lower_hex(&value).map_err(serde::de::Error::custom)
    }
}

/// The immutable identity whose uniqueness is consumed before generation.
#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct AttemptIdentity {
    pub installation_id: String,
    pub run_id: String,
    pub suite_id: String,
    pub case_id: String,
    pub ordinal: u32,
}

/// Evaluation correlation supplied by an authorized EVAL request.
#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct EvalContext {
    pub schema_version: u16,
    pub run_id: String,
    pub suite_id: String,
    pub suite_revision_sha256: Digest32,
    pub case_id: String,
    pub ordinal: u32,
    pub case_spec_sha256: Digest32,
}

impl EvalContext {
    /// Validates the request-side schema and bounded correlation identifiers.
    pub fn validate(&self) -> Result<(), CanonicalError> {
        if self.schema_version != CURRENT_SCHEMA_VERSION {
            return Err(CanonicalError::UnsupportedSchemaVersion(
                self.schema_version,
            ));
        }
        validate_identifier("run_id", &self.run_id)?;
        validate_identifier("suite_id", &self.suite_id)?;
        validate_identifier("case_id", &self.case_id)
    }
}

impl AttemptIdentity {
    /// Validates all bounded correlation identifiers.
    pub fn validate(&self) -> Result<(), CanonicalError> {
        validate_identifier("installation_id", &self.installation_id)?;
        validate_identifier("run_id", &self.run_id)?;
        validate_identifier("suite_id", &self.suite_id)?;
        validate_identifier("case_id", &self.case_id)
    }
}

/// Ordered terminal claims defined by ADR-0174.
#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct EvalReceiptClaims {
    pub schema_version: u16,
    pub installation_id: String,
    pub anchor_sha256: Digest32,
    pub key_id: Digest32,
    pub key_epoch: u64,
    pub engine_release: String,
    pub source_commit: String,
    pub libllama_revision: String,
    pub release_manifest_sha256: Digest32,
    pub engine_binary_sha256: Digest32,
    pub engine_instance_id: String,
    pub principal_id: String,
    pub request_id: String,
    pub trace_id: String,
    pub endpoint: String,
    pub run_id: String,
    pub suite_id: String,
    pub suite_revision_sha256: Digest32,
    pub case_id: String,
    pub ordinal: u32,
    pub attempt_key: Digest32,
    pub eval_slot: u32,
    pub seed: u64,
    pub case_spec_sha256: Digest32,
    pub model_id: String,
    pub model_sha256: Digest32,
    pub adapter_set_sha256: Digest32,
    pub template_sha256: Digest32,
    pub system_messages_sha256: Digest32,
    pub grammar_sha256: Digest32,
    pub sampler_sha256: Digest32,
    pub generation_control_sha256: Digest32,
    pub original_messages_sha256: Digest32,
    pub rendered_prompt_sha256: Digest32,
    pub output_sha256: Digest32,
    pub prompt_tokens: u64,
    pub completion_tokens: u64,
    pub finish_reason: String,
}

impl EvalReceiptClaims {
    /// Returns the attempt identity embedded in the claims.
    #[must_use]
    pub fn attempt_identity(&self) -> AttemptIdentity {
        AttemptIdentity {
            installation_id: self.installation_id.clone(),
            run_id: self.run_id.clone(),
            suite_id: self.suite_id.clone(),
            case_id: self.case_id.clone(),
            ordinal: self.ordinal,
        }
    }

    /// Validates schema, bounded strings, and the derived attempt binding.
    pub fn validate(&self) -> Result<(), CanonicalError> {
        if self.schema_version != CURRENT_SCHEMA_VERSION {
            return Err(CanonicalError::UnsupportedSchemaVersion(
                self.schema_version,
            ));
        }
        self.attempt_identity().validate()?;
        for (name, value) in [
            ("engine_instance_id", self.engine_instance_id.as_str()),
            ("principal_id", self.principal_id.as_str()),
            ("request_id", self.request_id.as_str()),
            ("trace_id", self.trace_id.as_str()),
        ] {
            validate_identifier(name, value)?;
        }
        for (name, value) in [
            ("engine_release", self.engine_release.as_str()),
            ("source_commit", self.source_commit.as_str()),
            ("libllama_revision", self.libllama_revision.as_str()),
            ("endpoint", self.endpoint.as_str()),
            ("model_id", self.model_id.as_str()),
            ("finish_reason", self.finish_reason.as_str()),
        ] {
            validate_claim_string(name, value)?;
        }
        let expected_attempt_key = attempt_key(&self.attempt_identity())?;
        if self.attempt_key != expected_attempt_key {
            return Err(CanonicalError::AttemptKeyMismatch);
        }
        Ok(())
    }
}

/// Digest-bearing claims for which absence has a canonical sentinel.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum AbsentDigestField {
    AdapterSet,
    Template,
    SystemMessages,
    Grammar,
}

impl AbsentDigestField {
    /// Returns the exact canonical field name.
    #[must_use]
    pub const fn field_name(self) -> &'static str {
        match self {
            Self::AdapterSet => "adapter_set_sha256",
            Self::Template => "template_sha256",
            Self::SystemMessages => "system_messages_sha256",
            Self::Grammar => "grammar_sha256",
        }
    }
}

/// Errors raised by strict canonical claim processing.
#[derive(Debug, Error)]
pub enum CanonicalError {
    #[error("digest must be exactly 64 lowercase hexadecimal characters")]
    InvalidDigestHex,
    #[error("{field} must be a non-empty bounded ASCII identifier")]
    InvalidIdentifier { field: &'static str },
    #[error("{field} must be non-empty and at most 4096 UTF-8 bytes")]
    InvalidString { field: &'static str },
    #[error("canonical field is too large")]
    FieldTooLarge,
    #[error("unsupported receipt schema version {0}")]
    UnsupportedSchemaVersion(u16),
    #[error("attempt_key does not match the canonical attempt identity")]
    AttemptKeyMismatch,
    #[error("message role must be one of system, user, assistant, or tool")]
    InvalidMessageRole,
}

/// Produces the exact ADR-0174 domain-separated canonical receipt bytes.
pub fn canonical_receipt_bytes(claims: &EvalReceiptClaims) -> Result<Vec<u8>, CanonicalError> {
    claims.validate()?;
    let mut output = Vec::with_capacity(1_024);
    output.extend_from_slice(RECEIPT_DOMAIN);
    push_u16(&mut output, 1, claims.schema_version);
    push_string(&mut output, 2, &claims.installation_id)?;
    push_digest(&mut output, 3, claims.anchor_sha256);
    push_digest(&mut output, 4, claims.key_id);
    push_u64(&mut output, 5, claims.key_epoch);
    push_string(&mut output, 6, &claims.engine_release)?;
    push_string(&mut output, 7, &claims.source_commit)?;
    push_string(&mut output, 8, &claims.libllama_revision)?;
    push_digest(&mut output, 9, claims.release_manifest_sha256);
    push_digest(&mut output, 10, claims.engine_binary_sha256);
    push_string(&mut output, 11, &claims.engine_instance_id)?;
    push_string(&mut output, 12, &claims.principal_id)?;
    push_string(&mut output, 13, &claims.request_id)?;
    push_string(&mut output, 14, &claims.trace_id)?;
    push_string(&mut output, 15, &claims.endpoint)?;
    push_string(&mut output, 16, &claims.run_id)?;
    push_string(&mut output, 17, &claims.suite_id)?;
    push_digest(&mut output, 18, claims.suite_revision_sha256);
    push_string(&mut output, 19, &claims.case_id)?;
    push_u32(&mut output, 20, claims.ordinal);
    push_digest(&mut output, 21, claims.attempt_key);
    push_u32(&mut output, 22, claims.eval_slot);
    push_u64(&mut output, 23, claims.seed);
    push_digest(&mut output, 24, claims.case_spec_sha256);
    push_string(&mut output, 25, &claims.model_id)?;
    push_digest(&mut output, 26, claims.model_sha256);
    push_digest(&mut output, 27, claims.adapter_set_sha256);
    push_digest(&mut output, 28, claims.template_sha256);
    push_digest(&mut output, 29, claims.system_messages_sha256);
    push_digest(&mut output, 30, claims.grammar_sha256);
    push_digest(&mut output, 31, claims.sampler_sha256);
    push_digest(&mut output, 32, claims.generation_control_sha256);
    push_digest(&mut output, 33, claims.original_messages_sha256);
    push_digest(&mut output, 34, claims.rendered_prompt_sha256);
    push_digest(&mut output, 35, claims.output_sha256);
    push_u64(&mut output, 36, claims.prompt_tokens);
    push_u64(&mut output, 37, claims.completion_tokens);
    push_string(&mut output, 38, &claims.finish_reason)?;
    Ok(output)
}

/// Derives the domain-separated uniqueness key for one evaluation attempt.
pub fn attempt_key(identity: &AttemptIdentity) -> Result<Digest32, CanonicalError> {
    identity.validate()?;
    let mut output = Vec::with_capacity(256);
    output.extend_from_slice(ATTEMPT_DOMAIN);
    push_string(&mut output, 1, &identity.installation_id)?;
    push_string(&mut output, 2, &identity.run_id)?;
    push_string(&mut output, 3, &identity.suite_id)?;
    push_string(&mut output, 4, &identity.case_id)?;
    push_u32(&mut output, 5, identity.ordinal);
    Ok(Digest32::sha256(&output))
}

/// Derives a field-specific, domain-separated absence sentinel.
#[must_use]
pub fn absent_sha256(field: AbsentDigestField) -> Digest32 {
    let mut output = Vec::with_capacity(96);
    output.extend_from_slice(ABSENT_DOMAIN);
    push_string(&mut output, 1, field.field_name())
        .expect("static receipt field names always fit in a u32 length");
    Digest32::sha256(&output)
}

/// Hashes request messages using an ordered, domain-separated TLV encoding.
pub fn original_messages_sha256(messages: &[(String, String)]) -> Result<Digest32, CanonicalError> {
    messages_sha256(ORIGINAL_MESSAGES_DOMAIN, messages)
}

/// Hashes system-role request messages or returns the canonical absent value.
pub fn system_messages_sha256(messages: &[(String, String)]) -> Result<Digest32, CanonicalError> {
    let system_messages = messages
        .iter()
        .filter(|(role, _)| role == "system")
        .cloned()
        .collect::<Vec<_>>();
    if system_messages.is_empty() {
        return Ok(absent_sha256(AbsentDigestField::SystemMessages));
    }
    messages_sha256(SYSTEM_MESSAGES_DOMAIN, &system_messages)
}

fn messages_sha256(
    domain: &[u8],
    messages: &[(String, String)],
) -> Result<Digest32, CanonicalError> {
    let count = u32::try_from(messages.len()).map_err(|_| CanonicalError::FieldTooLarge)?;
    let mut output = Vec::with_capacity(256);
    output.extend_from_slice(domain);
    push_u32(&mut output, 1, count);
    for (role, content) in messages {
        if !matches!(role.as_str(), "system" | "user" | "assistant" | "tool") {
            return Err(CanonicalError::InvalidMessageRole);
        }
        validate_string("message_content", content)?;
        push_string(&mut output, 2, role)?;
        push_string(&mut output, 3, content)?;
    }
    Ok(Digest32::sha256(&output))
}

fn push_tlv(output: &mut Vec<u8>, tag: u16, value: &[u8]) -> Result<(), CanonicalError> {
    let length = u32::try_from(value.len()).map_err(|_| CanonicalError::FieldTooLarge)?;
    output.extend_from_slice(&tag.to_be_bytes());
    output.extend_from_slice(&length.to_be_bytes());
    output.extend_from_slice(value);
    Ok(())
}

fn push_string(output: &mut Vec<u8>, tag: u16, value: &str) -> Result<(), CanonicalError> {
    push_tlv(output, tag, value.as_bytes())
}

fn push_digest(output: &mut Vec<u8>, tag: u16, value: Digest32) {
    push_tlv(output, tag, value.as_bytes()).expect("SHA-256 values always fit in a u32 length");
}

fn push_u16(output: &mut Vec<u8>, tag: u16, value: u16) {
    push_tlv(output, tag, &value.to_be_bytes()).expect("u16 values always fit in a u32 length");
}

fn push_u32(output: &mut Vec<u8>, tag: u16, value: u32) {
    push_tlv(output, tag, &value.to_be_bytes()).expect("u32 values always fit in a u32 length");
}

fn push_u64(output: &mut Vec<u8>, tag: u16, value: u64) {
    push_tlv(output, tag, &value.to_be_bytes()).expect("u64 values always fit in a u32 length");
}

fn validate_identifier(field: &'static str, value: &str) -> Result<(), CanonicalError> {
    let mut bytes = value.bytes();
    let Some(first) = bytes.next() else {
        return Err(CanonicalError::InvalidIdentifier { field });
    };
    if value.len() > MAX_IDENTIFIER_BYTES
        || !first.is_ascii_alphanumeric()
        || !bytes
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_' | b'.' | b':'))
    {
        return Err(CanonicalError::InvalidIdentifier { field });
    }
    Ok(())
}

fn validate_string(field: &'static str, value: &str) -> Result<(), CanonicalError> {
    if value.is_empty() || value.len() > MAX_STRING_BYTES {
        return Err(CanonicalError::InvalidString { field });
    }
    Ok(())
}

fn validate_claim_string(field: &'static str, value: &str) -> Result<(), CanonicalError> {
    validate_string(field, value)?;
    if value.chars().any(char::is_control) {
        return Err(CanonicalError::InvalidString { field });
    }
    Ok(())
}
