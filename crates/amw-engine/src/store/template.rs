//! Curated SHA-256 trust authority for GGUF chat templates.

use sha2::{Digest, Sha256};
use thiserror::Error;

const CHATML: &str = concat!(
    "{% for message in messages %}",
    "<|im_start|>{{ message.role }}\n{{ message.content }}<|im_end|>\n",
    "{% endfor %}",
    "<|im_start|>assistant\n",
);
const LLAMA: &str = concat!(
    "{% for message in messages %}",
    "{% if message.role == 'system' %}",
    "<|begin_of_text|><|start_header_id|>system<|end_header_id|>\n{{ message.content }}<|eot_id|>",
    "{% elif message.role == 'user' %}",
    "<|start_header_id|>user<|end_header_id|>\n{{ message.content }}<|eot_id|>",
    "{% elif message.role == 'assistant' %}",
    "<|start_header_id|>assistant<|end_header_id|>\n{{ message.content }}<|eot_id|>",
    "{% endif %}",
    "{% endfor %}",
    "<|start_header_id|>assistant<|end_header_id|>\n",
);
const MISTRAL: &str = concat!(
    "{% for message in messages %}",
    "{% if message.role == 'user' %}",
    "[INST] {{ message.content }} [/INST]",
    "{% elif message.role == 'assistant' %}",
    " {{ message.content }}</s>",
    "{% endif %}",
    "{% endfor %}",
);
const GEMMA: &str = concat!(
    "{% for message in messages %}",
    "{% if message.role == 'user' %}",
    "<start_of_turn>user\n{{ message.content }}<end_of_turn>\n",
    "{% elif message.role == 'assistant' %}",
    "<start_of_turn>model\n{{ message.content }}<end_of_turn>\n",
    "{% endif %}",
    "{% endfor %}",
    "<start_of_turn>model\n",
);

/// Trust decision for the only template a runtime may render.
#[derive(Clone, Debug, Eq, PartialEq)]
pub enum TemplateVerdict {
    /// Model-embedded bytes exactly match one curated SHA-256.
    TrustedEmbedded {
        template: String,
        family: &'static str,
        sha256: String,
    },
    /// The model embeds nothing, so a local immutable family fallback is used.
    CuratedFallback {
        template: &'static str,
        family: &'static str,
    },
    /// Embedded executable text is unknown and must never be rendered.
    Untrusted { sha256: String },
}

impl TemplateVerdict {
    /// Whether warm-up and rendering may use this verdict.
    pub fn warmup_allowed(&self) -> bool {
        matches!(
            self,
            Self::TrustedEmbedded { .. } | Self::CuratedFallback { .. }
        )
    }

    /// Returns the curated family selected by the trust authority.
    pub fn family(&self) -> Option<&'static str> {
        match self {
            Self::TrustedEmbedded { family, .. } | Self::CuratedFallback { family, .. } => {
                Some(*family)
            }
            Self::Untrusted { .. } => None,
        }
    }

    /// Returns the SHA-256 identity of the exact trusted template bytes.
    ///
    /// Untrusted embedded templates have no executable identity because the
    /// runtime refuses to render them.
    pub fn trusted_sha256(&self) -> Option<[u8; 32]> {
        let template = match self {
            Self::TrustedEmbedded { template, .. } => template.as_str(),
            Self::CuratedFallback { template, .. } => *template,
            Self::Untrusted { .. } => return None,
        };
        Some(Sha256::digest(template.as_bytes()).into())
    }

    /// Renders only a trusted embedded template or local curated fallback.
    #[cfg(any(feature = "cpu", feature = "cuda"))]
    pub fn render_chat(
        &self,
        model: &crate::ffi::Model,
        messages: &[crate::ffi::ChatMessage<'_>],
        add_assistant: bool,
    ) -> Result<Vec<u8>, TemplateRenderError> {
        match self {
            Self::TrustedEmbedded { template, .. } => model
                .apply_embedded_chat_template(template, messages, add_assistant)
                .map_err(TemplateRenderError::Native),
            Self::CuratedFallback { template, .. } => model
                .apply_curated_chat_template(template, messages, add_assistant)
                .map_err(TemplateRenderError::Native),
            Self::Untrusted { .. } => Err(TemplateRenderError::TemplateUntrusted),
        }
    }
}

/// Typed rendering refusal kept distinct from native template failures.
#[derive(Debug, Error, Eq, PartialEq)]
pub enum TemplateRenderError {
    #[error("model chat template is not present in the curated trust registry")]
    TemplateUntrusted,
    #[cfg(any(feature = "cpu", feature = "cuda"))]
    #[error("native chat template rendering failed: {0}")]
    Native(crate::ffi::FfiError),
}

/// Stateless curated policy; callers cannot add arbitrary trusted templates.
#[derive(Clone, Copy, Debug, Default)]
pub struct TemplatePolicy;

impl TemplatePolicy {
    /// Resolves embedded bytes by SHA-256 or selects a local fallback only when absent.
    pub fn evaluate(&self, model_id: &str, embedded: Option<&str>) -> TemplateVerdict {
        let family = detect_family(model_id);
        let Some(embedded) = embedded else {
            return TemplateVerdict::CuratedFallback {
                template: fallback(family),
                family,
            };
        };
        let sha256 = hash_template(embedded);
        match curated_family_for_hash(&sha256) {
            Some(matched_family) => TemplateVerdict::TrustedEmbedded {
                template: embedded.to_owned(),
                family: matched_family,
                sha256,
            },
            None => TemplateVerdict::Untrusted { sha256 },
        }
    }
}

fn curated_family_for_hash(hash: &str) -> Option<&'static str> {
    [
        ("chatml", CHATML),
        ("llama", LLAMA),
        ("mistral", MISTRAL),
        ("phi", CHATML),
        ("gemma", GEMMA),
    ]
    .into_iter()
    .find_map(|(family, template)| (hash_template(template) == hash).then_some(family))
}

fn hash_template(template: &str) -> String {
    format!("{:x}", Sha256::digest(template.as_bytes()))
}

fn detect_family(model_id: &str) -> &'static str {
    let lower = model_id.to_ascii_lowercase();
    if ["qwen", "dolphin", "hermes", "openhermes", "chatml", "nous"]
        .iter()
        .any(|tag| lower.contains(tag))
    {
        "chatml"
    } else if ["llama", "meta-llama", "codellama"]
        .iter()
        .any(|tag| lower.contains(tag))
    {
        "llama"
    } else if ["mistral", "mixtral", "zephyr"]
        .iter()
        .any(|tag| lower.contains(tag))
    {
        "mistral"
    } else if ["phi-", "phi2", "phi3", "phi4"]
        .iter()
        .any(|tag| lower.contains(tag))
    {
        "phi"
    } else if lower.contains("gemma") {
        "gemma"
    } else {
        "default"
    }
}

fn fallback(family: &'static str) -> &'static str {
    match family {
        "chatml" | "phi" | "default" => CHATML,
        "llama" => LLAMA,
        "mistral" => MISTRAL,
        "gemma" => GEMMA,
        _ => CHATML,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn curated_hashes_are_trusted_unknown_embedded_is_not() {
        let policy = TemplatePolicy;
        assert!(matches!(
            policy.evaluate("qwen", Some(CHATML)),
            TemplateVerdict::TrustedEmbedded {
                family: "chatml",
                ..
            }
        ));
        let untrusted = policy.evaluate("qwen", Some("{{ arbitrary }}"));
        assert!(matches!(untrusted, TemplateVerdict::Untrusted { .. }));
        assert!(!untrusted.warmup_allowed());
    }

    #[test]
    fn missing_template_uses_only_the_local_family_fallback() {
        let policy = TemplatePolicy;
        let fallback = policy.evaluate("tinyllama-15m", None);
        assert!(matches!(
            fallback,
            TemplateVerdict::CuratedFallback {
                family: "llama",
                template: LLAMA,
            }
        ));
        assert!(fallback.warmup_allowed());
        assert_eq!(
            fallback.trusted_sha256(),
            Some(Sha256::digest(LLAMA.as_bytes()).into())
        );
    }

    #[test]
    fn untrusted_template_has_no_executable_receipt_identity() {
        let verdict = TemplatePolicy.evaluate("qwen", Some("{{ attacker }}"));

        assert_eq!(verdict.trusted_sha256(), None);
    }
}
