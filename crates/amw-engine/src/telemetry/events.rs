use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

pub const EVENT_SCHEMA_VERSION: u32 = 1;
const PREFIX_IDENTIFIER_PREFIX: &str = "pfx-";
const PREFIX_IDENTIFIER_DIGEST_BYTES: usize = 12;
const PREFIX_IDENTIFIER_BYTES: usize =
    PREFIX_IDENTIFIER_PREFIX.len() + (PREFIX_IDENTIFIER_DIGEST_BYTES * 2);

#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
#[serde(tag = "event", rename_all = "snake_case")]
pub enum EngineEvent {
    RequestComplete {
        request_id: String,
        trace_id: String,
        model_id: String,
        queue_ms: f64,
        prefill_ms: f64,
        decode_ms: f64,
        input_tokens: u32,
        output_tokens: u32,
        tok_per_s: f64,
        prefix_hit_tokens: u32,
        speculation_proposed_tokens: u32,
        speculation_accepted_tokens: u32,
        #[serde(skip_serializing_if = "Option::is_none")]
        spec_accept_rate: Option<f64>,
        priority_class: String,
        eval_slot: usize,
    },
    RequestFailed {
        request_id: String,
        trace_id: String,
        model_id: String,
        code: String,
        priority_class: String,
    },
    ModelLoaded {
        model_id: String,
        vram_mb: u64,
    },
    ModelUnloaded {
        model_id: String,
        vram_mb: u64,
    },
    SlotState {
        slot_id: usize,
        state: String,
    },
    PrefixRegistered {
        name: String,
        tokens: u32,
    },
    PrefixHit {
        name: String,
        tokens: u32,
    },
    Gauges {
        slots_busy: usize,
        queue_depth: usize,
        vram_used_mb: Option<u64>,
        kv_occupancy_pct: u8,
    },
}

#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
pub struct EventEnvelope {
    pub ts: f64,
    pub schema_version: u32,
    #[serde(flatten)]
    pub event: EngineEvent,
}

impl EventEnvelope {
    pub fn new(ts: f64, event: EngineEvent) -> Self {
        Self {
            ts,
            schema_version: EVENT_SCHEMA_VERSION,
            event: redact_prefix_identifier(event),
        }
    }
    pub fn to_ndjson(&self) -> Result<String, serde_json::Error> {
        serde_json::to_string(self).map(|mut value| {
            value.push('\n');
            value
        })
    }

    pub fn contains_content(&self) -> bool {
        match &self.event {
            EngineEvent::PrefixRegistered { name, .. } | EngineEvent::PrefixHit { name, .. } => {
                !is_opaque_prefix_identifier(name)
            }
            EngineEvent::RequestComplete { .. }
            | EngineEvent::RequestFailed { .. }
            | EngineEvent::ModelLoaded { .. }
            | EngineEvent::ModelUnloaded { .. }
            | EngineEvent::SlotState { .. }
            | EngineEvent::Gauges { .. } => false,
        }
    }

    pub fn validate(&self) -> Result<(), &'static str> {
        if self.schema_version != EVENT_SCHEMA_VERSION {
            return Err("unsupported event schema version");
        }
        if !self.ts.is_finite() || self.ts < 0.0 {
            return Err("event timestamp must be finite and non-negative");
        }
        match &self.event {
            EngineEvent::RequestComplete {
                queue_ms,
                prefill_ms,
                decode_ms,
                tok_per_s,
                spec_accept_rate,
                speculation_proposed_tokens,
                speculation_accepted_tokens,
                ..
            } => {
                if [*queue_ms, *prefill_ms, *decode_ms, *tok_per_s]
                    .iter()
                    .any(|value| !value.is_finite() || *value < 0.0)
                {
                    return Err("request metrics must be finite and non-negative");
                }
                if spec_accept_rate
                    .is_some_and(|value| !value.is_finite() || !(0.0..=1.0).contains(&value))
                {
                    return Err("speculation acceptance rate must be between zero and one");
                }
                if speculation_accepted_tokens > speculation_proposed_tokens {
                    return Err("accepted speculation tokens must not exceed proposed tokens");
                }
                match (*speculation_proposed_tokens, *spec_accept_rate) {
                    (0, None) => {}
                    (0, Some(_)) => {
                        return Err("speculation acceptance rate requires committed proposals")
                    }
                    (proposed, Some(rate))
                        if (rate
                            - f64::from(*speculation_accepted_tokens) / f64::from(proposed))
                        .abs()
                            <= f64::EPSILON => {}
                    _ => return Err("speculation acceptance rate must match exact token counts"),
                }
            }
            EngineEvent::Gauges {
                kv_occupancy_pct, ..
            } if *kv_occupancy_pct > 100 => {
                return Err("KV occupancy percentage must not exceed 100");
            }
            EngineEvent::PrefixRegistered { name, .. } | EngineEvent::PrefixHit { name, .. }
                if !is_opaque_prefix_identifier(name) =>
            {
                return Err("prefix telemetry identifier must be a bounded opaque reference");
            }
            _ => {}
        }
        Ok(())
    }
}

fn redact_prefix_identifier(event: EngineEvent) -> EngineEvent {
    match event {
        EngineEvent::PrefixRegistered { name, tokens } => EngineEvent::PrefixRegistered {
            name: opaque_prefix_identifier(&name),
            tokens,
        },
        EngineEvent::PrefixHit { name, tokens } => EngineEvent::PrefixHit {
            name: opaque_prefix_identifier(&name),
            tokens,
        },
        other => other,
    }
}

fn opaque_prefix_identifier(name: &str) -> String {
    let digest = Sha256::digest(name.as_bytes());
    let mut identifier = String::with_capacity(PREFIX_IDENTIFIER_BYTES);
    identifier.push_str(PREFIX_IDENTIFIER_PREFIX);
    for byte in &digest[..PREFIX_IDENTIFIER_DIGEST_BYTES] {
        use std::fmt::Write as _;

        write!(identifier, "{byte:02x}").expect("writing to a String cannot fail");
    }
    identifier
}

fn is_opaque_prefix_identifier(identifier: &str) -> bool {
    identifier.len() == PREFIX_IDENTIFIER_BYTES
        && identifier
            .strip_prefix(PREFIX_IDENTIFIER_PREFIX)
            .is_some_and(|digest| digest.bytes().all(|byte| byte.is_ascii_hexdigit()))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn serialized_event_vocabulary_matches_python_v1() {
        let event = EventEnvelope::new(
            1.0,
            EngineEvent::RequestComplete {
                request_id: "request-1".to_owned(),
                trace_id: "trace-1".to_owned(),
                model_id: "model-1".to_owned(),
                queue_ms: 1.0,
                prefill_ms: 2.0,
                decode_ms: 3.0,
                input_tokens: 4,
                output_tokens: 5,
                tok_per_s: 6.0,
                prefix_hit_tokens: 0,
                speculation_proposed_tokens: 0,
                speculation_accepted_tokens: 0,
                spec_accept_rate: None,
                priority_class: "interactive".to_owned(),
                eval_slot: 0,
            },
        );
        let value = serde_json::to_value(event).unwrap();

        assert_eq!(value["event"], "request_complete");
        assert!(value.get("spec_accept_rate").is_none());
        assert!(value.get("cursor").is_none());
        assert!(value.get("watchdog").is_none());
    }

    #[test]
    fn every_public_variant_stays_inside_frozen_python_v1_vocabulary() {
        let events = [
            EngineEvent::RequestFailed {
                request_id: "r".to_owned(),
                trace_id: "t".to_owned(),
                model_id: "m".to_owned(),
                code: "cancelled".to_owned(),
                priority_class: "batch".to_owned(),
            },
            EngineEvent::ModelLoaded {
                model_id: "m".to_owned(),
                vram_mb: 1,
            },
            EngineEvent::ModelUnloaded {
                model_id: "m".to_owned(),
                vram_mb: 1,
            },
            EngineEvent::SlotState {
                slot_id: 0,
                state: "idle".to_owned(),
            },
            EngineEvent::PrefixRegistered {
                name: "system".to_owned(),
                tokens: 1,
            },
            EngineEvent::PrefixHit {
                name: "system".to_owned(),
                tokens: 1,
            },
            EngineEvent::Gauges {
                slots_busy: 0,
                queue_depth: 0,
                vram_used_mb: None,
                kv_occupancy_pct: 0,
            },
        ];
        let names: Vec<String> = events
            .into_iter()
            .map(|event| {
                serde_json::to_value(EventEnvelope::new(1.0, event)).unwrap()["event"]
                    .as_str()
                    .unwrap()
                    .to_owned()
            })
            .collect();

        assert_eq!(
            names.iter().map(String::as_str).collect::<Vec<_>>(),
            [
                "request_failed",
                "model_loaded",
                "model_unloaded",
                "slot_state",
                "prefix_registered",
                "prefix_hit",
                "gauges",
            ]
        );
    }

    #[test]
    fn prefix_telemetry_serializes_only_bounded_opaque_identifiers() {
        let raw_name = "customer-secret-prefix-name";
        let registered = EventEnvelope::new(
            1.0,
            EngineEvent::PrefixRegistered {
                name: raw_name.to_owned(),
                tokens: 20,
            },
        );
        let hit = EventEnvelope::new(
            2.0,
            EngineEvent::PrefixHit {
                name: raw_name.to_owned(),
                tokens: 10,
            },
        );
        let registered_json = serde_json::to_value(&registered).unwrap();
        let hit_json = serde_json::to_value(&hit).unwrap();

        assert_eq!(registered_json["name"], hit_json["name"]);
        let identifier = registered_json["name"].as_str().unwrap();
        assert!(is_opaque_prefix_identifier(identifier));
        assert!(!registered.to_ndjson().unwrap().contains(raw_name));
        assert!(!registered.contains_content());
        assert!(registered.validate().is_ok());
    }

    #[test]
    fn unredacted_deserialized_prefix_is_classified_as_content_and_rejected() {
        let event: EventEnvelope = serde_json::from_value(serde_json::json!({
            "ts": 1.0,
            "schema_version": EVENT_SCHEMA_VERSION,
            "event": "prefix_hit",
            "name": "raw-user-prefix",
            "tokens": 2
        }))
        .unwrap();

        assert!(event.contains_content());
        assert_eq!(
            event.validate(),
            Err("prefix telemetry identifier must be a bounded opaque reference")
        );
    }

    #[test]
    fn unknown_schema_version_is_rejected_before_replay() {
        let event = EventEnvelope {
            ts: 1.0,
            schema_version: EVENT_SCHEMA_VERSION + 1,
            event: EngineEvent::Gauges {
                slots_busy: 0,
                queue_depth: 0,
                vram_used_mb: None,
                kv_occupancy_pct: 0,
            },
        };

        assert_eq!(event.validate(), Err("unsupported event schema version"));
    }
}
