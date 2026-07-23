use std::collections::{BTreeMap, BTreeSet};
use std::path::PathBuf;

use crate::storage::{AppendLogStore, StorageError};
use serde_json::Value;
use std::str::FromStr;

#[derive(Debug, Clone, Eq, PartialEq)]
pub enum SpineError {
    Storage(StorageError),
    Corrupt(String),
    MissingAuthority(PathBuf),
    DuplicateRecord { kind: String, record_id: String },
    MissingRecord { kind: String, record_id: String },
}

impl From<StorageError> for SpineError {
    fn from(value: StorageError) -> Self {
        Self::Storage(value)
    }
}

#[derive(Debug, Clone, Eq, PartialEq)]
pub struct SpineRecord {
    pub kind: SpineRecordKind,
    pub record_id: String,
    pub payload: Value,
}

#[derive(Debug, Clone, Copy, Eq, PartialEq, Ord, PartialOrd)]
pub enum SpineRecordKind {
    Asset,
    Decision,
    Receipt,
}

impl SpineRecordKind {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Asset => "asset",
            Self::Decision => "decision",
            Self::Receipt => "receipt",
        }
    }
}

impl FromStr for SpineRecordKind {
    type Err = SpineError;

    fn from_str(value: &str) -> Result<Self, Self::Err> {
        match value {
            "asset" => Ok(Self::Asset),
            "decision" => Ok(Self::Decision),
            "receipt" => Ok(Self::Receipt),
            other => Err(SpineError::Corrupt(format!("unknown spine kind {other}"))),
        }
    }
}

#[derive(Debug, Clone, Eq, PartialEq)]
pub struct SpineState {
    active: BTreeMap<(String, String), String>,
    deleted: BTreeSet<(String, String)>,
}

impl SpineState {
    pub fn active_len(&self) -> usize {
        self.active.len()
    }

    pub fn contains(&self, kind: &str, record_id: &str) -> bool {
        self.active
            .contains_key(&(kind.to_string(), record_id.to_string()))
    }

    pub fn was_deleted(&self, kind: &str, record_id: &str) -> bool {
        self.deleted
            .contains(&(kind.to_string(), record_id.to_string()))
    }
}

pub struct SpineAuthority {
    store: AppendLogStore,
}

impl SpineAuthority {
    pub fn new(log_path: impl Into<PathBuf>) -> Self {
        Self {
            store: AppendLogStore::new(log_path),
        }
    }

    pub fn append_record(&self, record: SpineRecord) -> Result<(), SpineError> {
        require_payload_envelope(record.kind, &record.payload)?;
        let kind = record.kind.as_str();
        let record_id = record.record_id.clone();
        let row = serde_json::json!({
            "schema_version": 1,
            "kind": kind,
            "record_id": record.record_id,
            "payload": record.payload,
        });
        let row_line = serde_json::to_string(&row).map_err(json_error)? + "\n";
        self.store.append_line_transaction(&row_line, |lines| {
            if record_is_active_from_lines(lines, kind, &record_id)? {
                return Err(SpineError::DuplicateRecord {
                    kind: kind.to_string(),
                    record_id: record_id.clone(),
                });
            }
            Ok(())
        })?;
        Ok(())
    }

    pub fn delete_record(
        &self,
        kind: &str,
        record_id: &str,
        reason: &str,
    ) -> Result<(), SpineError> {
        let state = self.rebuild()?;
        if !state.contains(kind, record_id) {
            return Err(SpineError::MissingRecord {
                kind: kind.to_string(),
                record_id: record_id.to_string(),
            });
        }
        let row = serde_json::json!({
            "schema_version": 1,
            "kind": "delete",
            "record_id": format!("{kind}:{record_id}"),
            "payload": {
                "target_kind": kind,
                "target_record_id": record_id,
                "reason": reason,
            },
        });
        self.store
            .append_line(&(serde_json::to_string(&row).map_err(json_error)? + "\n"))?;
        Ok(())
    }

    pub fn rebuild(&self) -> Result<SpineState, SpineError> {
        let mut state = SpineState {
            active: BTreeMap::new(),
            deleted: BTreeSet::new(),
        };
        for line in self.store.read_lines()? {
            let row: Value = serde_json::from_str(&line)
                .map_err(|err| SpineError::Corrupt(format!("invalid JSONL record: {err}")))?;
            let kind = json_string(&row, "kind")?;
            let record_id = json_string(&row, "record_id")?;
            let payload = row
                .get("payload")
                .ok_or_else(|| SpineError::Corrupt("missing JSON key payload".to_string()))?;
            if !payload.is_object() {
                return Err(SpineError::Corrupt("payload must be an object".to_string()));
            }
            if kind == "delete" {
                let target_kind = json_string(payload, "target_kind")?;
                let target_record_id = json_string(payload, "target_record_id")?;
                state
                    .active
                    .remove(&(target_kind.clone(), target_record_id.clone()));
                state.deleted.insert((target_kind, target_record_id));
                continue;
            }
            let parsed_kind = kind.parse::<SpineRecordKind>()?;
            require_payload_envelope(parsed_kind, payload)?;
            let key = (kind.clone(), record_id.clone());
            if state.active.contains_key(&key) {
                return Err(SpineError::DuplicateRecord { kind, record_id });
            }
            state.active.insert(key, payload.to_string());
        }
        Ok(state)
    }
}

fn json_string(row: &Value, key: &str) -> Result<String, SpineError> {
    row.get(key)
        .and_then(Value::as_str)
        .map(ToString::to_string)
        .ok_or_else(|| SpineError::Corrupt(format!("missing JSON string key {key}")))
}

fn json_error(err: serde_json::Error) -> SpineError {
    SpineError::Corrupt(format!("failed to encode spine record: {err}"))
}

fn record_is_active_from_lines(
    lines: Vec<String>,
    kind: &str,
    record_id: &str,
) -> Result<bool, SpineError> {
    let mut active = false;
    for line in lines {
        let row: Value = serde_json::from_str(&line)
            .map_err(|err| SpineError::Corrupt(format!("invalid JSONL record: {err}")))?;
        let row_kind = json_string(&row, "kind")?;
        let row_record_id = json_string(&row, "record_id")?;
        let payload = row
            .get("payload")
            .ok_or_else(|| SpineError::Corrupt("missing JSON key payload".to_string()))?;
        if !payload.is_object() {
            return Err(SpineError::Corrupt("payload must be an object".to_string()));
        }
        if row_kind == "delete" {
            let target_kind = json_string(payload, "target_kind")?;
            let target_record_id = json_string(payload, "target_record_id")?;
            if target_kind == kind && target_record_id == record_id {
                active = false;
            }
            continue;
        }
        let parsed_kind = row_kind.parse::<SpineRecordKind>()?;
        require_payload_envelope(parsed_kind, payload)?;
        if row_kind == kind && row_record_id == record_id {
            if active {
                return Err(SpineError::DuplicateRecord {
                    kind: kind.to_string(),
                    record_id: record_id.to_string(),
                });
            }
            active = true;
        }
    }
    Ok(active)
}

fn require_payload_envelope(kind: SpineRecordKind, payload: &Value) -> Result<(), SpineError> {
    if !payload.is_object() {
        return Err(SpineError::Corrupt("payload must be an object".to_string()));
    }
    let required_key = match kind {
        SpineRecordKind::Asset => "revision",
        SpineRecordKind::Decision => "decision_id",
        SpineRecordKind::Receipt => "receipt_id",
    };
    if !payload
        .get(required_key)
        .is_some_and(|value| value.is_string())
    {
        return Err(SpineError::Corrupt(format!(
            "{kind:?} payload missing string key {required_key}"
        )));
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;
    use std::sync::{Arc, Barrier};
    use std::time::{SystemTime, UNIX_EPOCH};

    fn temp_path(name: &str) -> PathBuf {
        let stamp = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("clock available")
            .as_nanos();
        std::env::temp_dir().join(format!("amw-spine-{name}-{stamp}.jsonl"))
    }

    #[test]
    fn rust_spine_rebuilds_valid_records() {
        let path = temp_path("valid");
        let authority = SpineAuthority::new(&path);

        authority
            .append_record(SpineRecord {
                kind: SpineRecordKind::Asset,
                record_id: "asset-1".to_string(),
                payload: serde_json::json!({"revision": "v1"}),
            })
            .expect("append succeeds");

        let state = authority.rebuild().expect("rebuild succeeds");
        assert_eq!(state.active_len(), 1);
        assert!(state.contains("asset", "asset-1"));
        let _ = fs::remove_file(path);
    }

    #[test]
    fn rust_spine_rejects_corrupt_log() {
        let path = temp_path("corrupt");
        fs::write(
            &path,
            b"{\"kind\":\"asset\",\"record_id\":\"asset-1\",\"payload\":{}",
        )
        .expect("seed corrupt log");
        let authority = SpineAuthority::new(&path);

        assert!(matches!(
            authority.rebuild(),
            Err(SpineError::Storage(StorageError::Corrupt(_)))
        ));
        let _ = fs::remove_file(path);
    }

    #[test]
    fn rust_spine_delete_tombstone_survives_rebuild() {
        let path = temp_path("delete");
        let authority = SpineAuthority::new(&path);
        authority
            .append_record(SpineRecord {
                kind: SpineRecordKind::Asset,
                record_id: "asset-1".to_string(),
                payload: serde_json::json!({"revision": "v1"}),
            })
            .expect("append succeeds");
        authority
            .delete_record("asset", "asset-1", "retention expired")
            .expect("delete succeeds");

        let state = authority.rebuild().expect("rebuild succeeds");
        assert_eq!(state.active_len(), 0);
        assert!(state.was_deleted("asset", "asset-1"));
        let _ = fs::remove_file(path);
    }

    #[test]
    fn rust_spine_duplicate_active_record_fails_closed() {
        let path = temp_path("duplicate");
        fs::write(
            &path,
            concat!(
                "{\"schema_version\":1,\"kind\":\"asset\",\"record_id\":\"asset-1\",\"payload\":{\"revision\":\"v1\"}}\n",
                "{\"schema_version\":1,\"kind\":\"asset\",\"record_id\":\"asset-1\",\"payload\":{\"revision\":\"v2\"}}\n"
            ),
        )
        .expect("seed duplicate log");
        let authority = SpineAuthority::new(&path);

        assert!(matches!(
            authority.rebuild(),
            Err(SpineError::DuplicateRecord { kind, record_id })
                if kind == "asset" && record_id == "asset-1"
        ));
        let _ = fs::remove_file(path);
    }

    #[test]
    fn rust_spine_concurrent_duplicate_append_keeps_log_replayable() {
        let path = temp_path("concurrent-duplicate");
        let authority = Arc::new(SpineAuthority::new(&path));
        let barrier = Arc::new(Barrier::new(2));
        let mut handles = Vec::new();

        for revision in ["v1", "v2"] {
            let authority = Arc::clone(&authority);
            let barrier = Arc::clone(&barrier);
            handles.push(std::thread::spawn(move || {
                barrier.wait();
                authority.append_record(SpineRecord {
                    kind: SpineRecordKind::Asset,
                    record_id: "asset-1".to_string(),
                    payload: serde_json::json!({"revision": revision}),
                })
            }));
        }

        let results = handles
            .into_iter()
            .map(|handle| handle.join().expect("thread joins"))
            .collect::<Vec<_>>();
        assert_eq!(results.iter().filter(|result| result.is_ok()).count(), 1);
        assert_eq!(
            results
                .iter()
                .filter(|result| matches!(
                    result,
                    Err(SpineError::DuplicateRecord { kind, record_id })
                        if kind == "asset" && record_id == "asset-1"
                ))
                .count(),
            1
        );

        let state = authority.rebuild().expect("log stays replayable");
        assert_eq!(state.active_len(), 1);
        assert!(state.contains("asset", "asset-1"));
        let _ = fs::remove_file(path);
    }

    #[test]
    fn rust_spine_serializes_payloads_with_braces_and_quotes() {
        let path = temp_path("payload-json");
        let authority = SpineAuthority::new(&path);
        authority
            .append_record(SpineRecord {
                kind: SpineRecordKind::Asset,
                record_id: "asset:\"1\"".to_string(),
                payload: serde_json::json!({"note": "brace } and quote \" remain data"}),
            })
            .expect_err("invalid asset payload fails closed");
        authority
            .append_record(SpineRecord {
                kind: SpineRecordKind::Asset,
                record_id: "asset:\"1\"".to_string(),
                payload: serde_json::json!({"revision": "brace } and quote \" remain data"}),
            })
            .expect("valid envelope append succeeds");

        let state = authority.rebuild().expect("rebuild succeeds");

        assert_eq!(state.active_len(), 1);
        assert!(state.contains("asset", "asset:\"1\""));
        let _ = fs::remove_file(path);
    }

    #[test]
    fn rust_spine_kernel_contracts_reject_unknown_kind_on_replay() {
        let path = temp_path("unknown-kind");
        fs::write(
            &path,
            "{\"schema_version\":1,\"kind\":\"freeform\",\"record_id\":\"asset-1\",\"payload\":{\"revision\":\"v1\"}}\n",
        )
        .expect("seed unknown kind log");
        let authority = SpineAuthority::new(&path);

        assert!(matches!(
            authority.rebuild(),
            Err(SpineError::Corrupt(message)) if message.contains("unknown spine kind freeform")
        ));
        let _ = fs::remove_file(path);
    }
}
