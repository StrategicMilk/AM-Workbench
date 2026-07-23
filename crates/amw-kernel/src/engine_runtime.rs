//! Read-only bridge to the Python engine supervisor's published owner record.
//!
//! The Python supervisor remains the sole lifecycle authority. The kernel reads
//! its atomically published record and credential file; it never starts,
//! stops, or otherwise mutates the engine process.

use std::{env, fs, path::PathBuf};

use serde::{Deserialize, Serialize};

const OWNER_SCHEMA: &str = "vetinari-engine-owner.v2";
const RUNTIME_SUBDIR: &str = "engine/runtime";

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum EngineLifecycleState {
    Running,
    Stopped,
    Missing,
    VersionMismatch,
    Degraded,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct EngineConnection {
    pub base_url: String,
    pub auth_token: String,
    pub lifecycle_state: EngineLifecycleState,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct OwnerRecord {
    schema_version: String,
    pid: u32,
    host: String,
    port: u16,
    endpoint: String,
    generation: u64,
    token_path: PathBuf,
    auth_policy_path: PathBuf,
    runtime_mode: String,
    expected_version: String,
    verified_version: String,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct EngineRuntimeError {
    pub state: EngineLifecycleState,
    pub reason: String,
}

pub fn read_engine_connection() -> Result<EngineConnection, EngineRuntimeError> {
    read_engine_connection_from(runtime_dir())
}

fn runtime_dir() -> PathBuf {
    if let Some(path) = env::var_os("VETINARI_USER_DIR") {
        return PathBuf::from(path).join(RUNTIME_SUBDIR);
    }
    let home = env::var_os("USERPROFILE")
        .or_else(|| env::var_os("HOME"))
        .map(PathBuf::from)
        .unwrap_or_else(env::temp_dir);
    home.join(".vetinari").join(RUNTIME_SUBDIR)
}

fn read_engine_connection_from(
    runtime_dir: PathBuf,
) -> Result<EngineConnection, EngineRuntimeError> {
    if !runtime_dir.is_dir() {
        return Err(runtime_error(
            EngineLifecycleState::Missing,
            "engine runtime directory is absent",
        ));
    }
    let owner_path = runtime_dir.join("supervisor.json");
    let raw = fs::read_to_string(&owner_path).map_err(|error| {
        runtime_error(
            EngineLifecycleState::Stopped,
            format!("engine owner record is unavailable: {error}"),
        )
    })?;
    let owner: OwnerRecord = serde_json::from_str(&raw).map_err(|error| {
        runtime_error(
            EngineLifecycleState::Degraded,
            format!("engine owner record is invalid: {error}"),
        )
    })?;
    validate_owner(&owner, &runtime_dir)?;
    let token = fs::read_to_string(&owner.token_path).map_err(|error| {
        runtime_error(
            EngineLifecycleState::Degraded,
            format!("engine credential is unavailable: {error}"),
        )
    })?;
    let token = token.trim().to_owned();
    if token.is_empty() || token.len() > 512 {
        return Err(runtime_error(
            EngineLifecycleState::Degraded,
            "engine credential is invalid",
        ));
    }
    Ok(EngineConnection {
        base_url: owner.endpoint.trim_end_matches('/').to_owned(),
        auth_token: token,
        lifecycle_state: EngineLifecycleState::Running,
    })
}

fn validate_owner(
    owner: &OwnerRecord,
    runtime_dir: &std::path::Path,
) -> Result<(), EngineRuntimeError> {
    if owner.schema_version != OWNER_SCHEMA
        || owner.pid == 0
        || owner.port == 0
        || owner.generation == 0
        || owner.runtime_mode != "owned"
        || !matches!(owner.host.as_str(), "127.0.0.1" | "localhost" | "::1")
    {
        return Err(runtime_error(
            EngineLifecycleState::Degraded,
            "engine owner record violates the published supervisor contract",
        ));
    }
    if owner.expected_version != owner.verified_version {
        return Err(runtime_error(
            EngineLifecycleState::VersionMismatch,
            "engine binary version does not match the supervisor pin",
        ));
    }
    let expected_token = runtime_dir.join("auth.token");
    let expected_policy = runtime_dir.join("auth-policy.json");
    if owner.token_path != expected_token || owner.auth_policy_path != expected_policy {
        return Err(runtime_error(
            EngineLifecycleState::Degraded,
            "engine owner record references credentials outside the runtime directory",
        ));
    }
    let expected_endpoint = format!("http://{}:{}", owner.host, owner.port);
    if owner.endpoint != expected_endpoint {
        return Err(runtime_error(
            EngineLifecycleState::Degraded,
            "engine owner endpoint is inconsistent",
        ));
    }
    Ok(())
}

fn runtime_error(state: EngineLifecycleState, reason: impl Into<String>) -> EngineRuntimeError {
    EngineRuntimeError {
        state,
        reason: reason.into(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::{SystemTime, UNIX_EPOCH};

    fn fixture_dir() -> PathBuf {
        let unique = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("clock")
            .as_nanos();
        env::temp_dir().join(format!("amw-kernel-engine-runtime-{unique}"))
    }

    #[test]
    fn owner_record_is_fail_closed_and_reads_private_token() {
        let dir = fixture_dir();
        fs::create_dir_all(&dir).expect("fixture directory");
        let token_path = dir.join("auth.token");
        let policy_path = dir.join("auth-policy.json");
        fs::write(&token_path, "secret-token\n").expect("token");
        fs::write(&policy_path, "{}").expect("policy");
        let owner = serde_json::json!({
            "schema_version": OWNER_SCHEMA,
            "pid": 7,
            "host": "127.0.0.1",
            "port": 8765,
            "endpoint": "http://127.0.0.1:8765",
            "generation": 1,
            "token_path": token_path,
            "auth_policy_path": policy_path,
            "runtime_mode": "owned",
            "expected_version": "1.0",
            "verified_version": "1.0"
        });
        fs::write(dir.join("supervisor.json"), owner.to_string()).expect("owner record");

        let connection = read_engine_connection_from(dir.clone()).expect("valid owner record");
        assert_eq!(connection.base_url, "http://127.0.0.1:8765");
        assert_eq!(connection.auth_token, "secret-token");
        fs::remove_dir_all(dir).expect("remove fixture");
    }

    #[test]
    fn version_mismatch_is_caller_visible() {
        let dir = fixture_dir();
        fs::create_dir_all(&dir).expect("fixture directory");
        let owner: OwnerRecord = serde_json::from_value(serde_json::json!({
            "schema_version": OWNER_SCHEMA,
            "pid": 7,
            "host": "127.0.0.1",
            "port": 8765,
            "endpoint": "http://127.0.0.1:8765",
            "generation": 1,
            "token_path": dir.join("auth.token"),
            "auth_policy_path": dir.join("auth-policy.json"),
            "runtime_mode": "owned",
            "expected_version": "1.0",
            "verified_version": "0.9"
        }))
        .expect("record");
        let error = validate_owner(&owner, &dir).expect_err("mismatch must fail");
        assert_eq!(error.state, EngineLifecycleState::VersionMismatch);
        fs::remove_dir_all(dir).expect("remove fixture");
    }
}
